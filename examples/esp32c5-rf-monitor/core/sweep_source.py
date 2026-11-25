"""设备端自扫源（方案 A）：把「谁掐表」交给设备后，主机侧的那一层适配。

为什么需要这一层：``Scheduler`` 只认 ``scan(request)`` —— 它按权重轮转挑一个模块、
从该模块的计划里取一个窗、请源扫这个窗。设备自扫之后，帧是**设备按自己节奏推上来
的**，与调度器的请求顺序并不一致。

所以这里做三件事，都不含判据：

1. **后台排空**：一个守护线程持续读串口、把帧按频点存进缓存。这一点是真板实测逼
   出来的：如果只在 ``scan()`` 里"读到我要的窗为止"，主机算表格的那一秒里设备吐出
   的帧就全丢了（实测设备 10.2 窗/秒、主机只消费到 3.1 窗/秒，两者差多少丢多少）。
2. **缓存兜底**：``scan(request)`` 先看缓存有没有这个频点；没有就等后台线程填，等到
   一轮扫完为止，绝不把「还没轮到」当成「扫不到」返回空。
3. **让路**：``GAIN`` / ``CAP20`` 与扫频共用射频，它们的应答还会跟帧流穿插。所以凡是
   走这些命令的操作，一律先 ``FSM STOP`` 停稳（顺带把读线程收掉）、做完、再接着扫。

计划里没有的频点（模块临时改了点位）直接退回逐窗驱动 ``link.scan()``：宁可慢，也不
许「扫不到就返回空」把模块饿死。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterator, Sequence

from .registry import ScanRequest
from .snapshot import Snapshot


def sweep_key(freq_hz: int) -> int:
    """频点归一键（MHz）。固件 ``FREQ`` 只收整 MHz，所以按 MHz 对齐。"""
    return int(round(int(freq_hz) / 1_000_000))


class SweepSource:
    """把设备自扫的帧流包成 ``Source``（``scan(request)``）。"""

    def __init__(
        self,
        link: Any,
        windows: Sequence[tuple[int, int, int]],
        bins: int,
        rate_code: int = 2,
        detector: int = 0,
        margin_s: float = 1.0,
        plan_dwell_ms: int = 20,
        pace: bool = True,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.link = link
        #: 设备计划表里每个窗的驻留（ms）。真板实测：固件一次 ``SPEC`` 是一个
        #: 16380 点突发（≈1 ms 采集），所谓「100 ms 驻留」在逐窗驱动下根本没花；
        #: 设备自扫是**真的按计划表 paced**，照抄模块声明的 100 ms 会把吞吐压到
        #: 10 窗/秒（实测）。这里给一个上限，既不空转也不把串口塞死。
        self.plan_dwell_ms = max(1, int(plan_dwell_ms))
        #: ``[(freq_mhz, dwell_ms, module_index), ...]`` —— 直接就是固件计划表的格式
        self.windows = [
            (int(mhz), max(1, min(int(dwell), self.plan_dwell_ms)), int(mod))
            for mhz, dwell, mod in windows
        ]
        self.bins = int(bins)
        self.rate_code = int(rate_code)
        self.detector = int(detector)
        self.margin_s = float(margin_s)
        #: 取到帧之后按该窗的驻留时长补一拍。理由是"占空"要诚实：设备自扫时驻留时间
        #: 已经花在设备上了，但调度器的权重是按**射频时间**分的 —— 主机若按串口速度
        #: 空转，权重就失真（实测 0.5 s 能跑 880 片），CPU 也白烧。
        self.pace = bool(pace)
        self._log = logger or (lambda _msg: None)
        self._cache: dict[int, tuple[float, Snapshot]] = {}
        self._keys = {key for key, _dwell, _mod in self.windows}
        #: 容许的帧陈旧度：设备重访同一窗最快就是一轮，所以「≤1 轮」已经是新鲜上限。
        #: 不设这个的话，刚被取走的那几个窗要等到设备下轮回到它，每轮卡一次一整轮。
        self.stale_s = 0.0
        self._ready = threading.Condition()
        self._reader: threading.Thread | None = None
        self._halt = threading.Event()
        #: 设备是否正在扫（``start()`` 之后为真，``stop()`` 或出错之后为假）
        self.running = False
        self.started = 0
        self.stopped = 0
        self.frames = 0
        self.misses = 0
        self.fallbacks = 0
        self.reopens = 0
        self.last_error = ""

    # ---- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        if not self.windows:
            raise ValueError("扫频计划不能为空")
        if self.running:
            return
        self.link.set_sweep_plan(self.windows)
        self.link.start_sweep(self.bins, self.rate_code, self.detector)
        self.running = True
        self.started += 1
        self.stale_s = max(1.0, 1.5 * self._round_s() + self.margin_s)
        self._halt.clear()
        self._reader = threading.Thread(target=self._drain, name="sweep-drain", daemon=True)
        self._reader.start()
        self._log(f"[sweep] 设备自扫启动：{len(self.windows)} 窗 / {self.bins} bin")

    def stop(self, drain_s: float = 5.0) -> None:
        if not self.running:
            return
        self.running = False
        self.stopped += 1
        # 先收读线程：它停了之后串口才回到「主机说了算」，否则 GAIN/CAP 的应答
        # 会跟帧流抢同一个读缓冲。
        self._halt.set()
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive():
            reader.join(timeout=max(1.0, drain_s))
        try:
            self.link.stop_sweep(drain_s)
        except Exception as exc:  # noqa: BLE001 - 停不下来也要把状态标成「没在扫」
            self.last_error = f"stop: {type(exc).__name__}: {exc}"
            self._log(f"[sweep] 停止异常：{self.last_error}")
        with self._ready:
            self._cache.clear()
            self._ready.notify_all()
        self._log("[sweep] 设备自扫停止")

    def close(self) -> None:
        self.stop()

    def pause_around(self, action: Callable[[], Any]) -> Any:
        """停扫 → 执行 → 恢复。给 GAIN / CAP 这类要独占射频的命令用。"""
        was = self.running
        if was:
            self.stop()
        try:
            return action()
        finally:
            if was:
                self.start()

    # ---- Source 协议 -----------------------------------------------------
    def scan(self, request: ScanRequest) -> Iterator[Snapshot]:
        if not self.running and not self.windows:
            yield from self.link.scan(request)
            return
        key = sweep_key(request.freq_hz)
        if key not in self._keys:
            self.fallbacks += 1
            if self.running:
                # 设备自扫期间串口只有一个读者（读线程）。这里直接 link.scan()
                # 会变成两个读者同时读，帧必被切歪 → 先停扫、扫完再恢复。
                yield from self.pause_around(lambda: list(self.link.scan(request)))
            else:
                yield from self.link.scan(request)
            return
        if not self.running:
            self.start()  # 懒启动：第一片才开始扫，避免开机就白吐帧
        snap = self._fetch(key)
        if self.pace:
            dwell_ms = next((d for k, d, _m in self.windows if k == key), 100)
            time.sleep(max(0.01, dwell_ms / 1000.0))
        if snap is not None:
            yield snap

    # ---- 内部 ------------------------------------------------------------
    def _drain(self) -> None:
        """后台读帧：读到什么就按频点存起来，**不判断谁要**。

        这个线程是性能关键：串口在设备自扫期间是一条连续帧流，主机不读就等于丢。
        """
        while not self._halt.is_set():
            try:
                snap = self.link.read_sweep_frame(timeout_s=0.2)
            except Exception as exc:  # noqa: BLE001 - 读失败不能把线程打死
                self.last_error = f"read: {type(exc).__name__}: {exc}"
                # 链路被判失效（帧错位）时自己重同步一次：core 的 failed 标记
                # 永不复位，不重开的话设备自扫就再也不出帧了。
                reopen = getattr(self.link, "reopen", None)
                if getattr(self.link, "failed", False) and callable(reopen):
                    try:
                        if reopen():
                            self.link.set_sweep_plan(self.windows)
                            self.link.start_sweep(self.bins, self.rate_code, self.detector)
                            self.last_error = ""
                            self.reopens += 1
                    except Exception as again:  # noqa: BLE001
                        self.last_error = f"reopen: {type(again).__name__}: {again}"
                time.sleep(0.05)
                continue
            if snap is None:
                continue
            key = sweep_key(snap.center_hz)
            with self._ready:
                self._cache[key] = (time.monotonic(), snap)
                self.frames += 1
                self._ready.notify_all()

    def _round_s(self) -> float:
        """一轮完整轮扫的时长（按设备驻留算），用作「等一个窗」的预算。"""
        return sum(d for _k, d, _m in self.windows) / 1000.0

    def _fetch(self, key: int) -> Snapshot | None:
        deadline = time.monotonic() + max(0.5, self._round_s()) + self.margin_s
        with self._ready:
            while True:
                item = self._cache.get(key)
                now = time.monotonic()
                # 「≤1 轮前扫到的」就算可用：设备本来就要一整轮才回到这个窗，
                # 再等只会把调度器卡成一跳一跳的。
                if item is not None and now - item[0] <= self.stale_s:
                    return item[1]
                left = deadline - now
                if left <= 0 or not self.running:
                    break
                self._ready.wait(min(0.2, left))
        self.misses += 1
        self._log(f"[sweep] {key} MHz 一轮都没轮到（预算 {max(0.5, self._round_s()):.1f}s）")
        return None

    # ---- 诊断 ------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "reader": bool(self._reader is not None and self._reader.is_alive()),
            "windows": len(self.windows),
            "bins": self.bins,
            "rate_code": self.rate_code,
            "started": self.started,
            "stopped": self.stopped,
            "frames": self.frames,
            "misses": self.misses,
            "fallbacks": self.fallbacks,
            "reopens": self.reopens,
            "cached": len(self._cache),
            "last_error": self.last_error,
        }


class SweepLinkFacade:
    """给 ``GainAwareSource`` / 抓档函数用的门面：``scan`` 走自扫源，其余命令让路。

    ``GainAwareSource`` 既要用 ``link.apply()`` 发 ``GAIN``，又要用 ``link.scan()`` 扫；
    这两件事对自扫的要求正好相反（一个要独占、一个是常态），所以必须由门面分流。
    """

    def __init__(self, link: Any, sweep: SweepSource) -> None:
        self._link = link
        self._sweep = sweep

    def scan(self, request: ScanRequest) -> Iterator[Snapshot]:
        yield from self._sweep.scan(request)

    def command(self, line: str, strict: bool = True) -> str:
        return self._sweep.pause_around(lambda: self._link.command(line, strict=strict))

    def set_gain(self, mode: str = "HARDWARE", index: int | None = None) -> None:
        return self._sweep.pause_around(lambda: self._link.set_gain(mode, index))

    def set_frequency_hz(self, freq_hz: int) -> int:
        return self._sweep.pause_around(lambda: self._link.set_frequency_hz(freq_hz))

    def capture_iq(self, *args: Any, **kwargs: Any) -> Any:
        return self._sweep.pause_around(lambda: self._link.capture_iq(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._link, name)


__all__ = ["SweepLinkFacade", "SweepSource", "sweep_key"]