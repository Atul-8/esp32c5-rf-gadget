"""增益调控：手动档位 + **主机侧闭环自动**（`host/app.py` 的顶栏按钮打到这里）。

为什么非要这一层（以下都是实板量出来的，不是推理）：

* 宽带占用率**随增益走**：同房间同脚本，增益 20/40/60 → 宽带占用 27%/61%/85%
  （`docs/03-ble-risk.md` §6.1）。所以「占用 85%」这句话，不给增益档位就是废话。
* 档位 index 是 **PHY 表项**，既不等于 dB 也不线性。实板 0→81 逐档实测（2440 MHz，
  每档取一轮 35~40 帧的 p10 中位数）：

  ``0~30: 0 码 | 35~45: 6~8.5 码 | 50~65: 22~31 码 | 70~81: 30~63 码``

  跨度 63 码、整体单调但**局部不单调**（50→25.0 / 55→22.0 / 60→31.0 / 65→24.5），
  这就是为什么自动档必须带死区 + 连续确认，不能一有差就动。
* **单帧底噪不可当控制量**：同一档位、同一频点连扫 14 轮，单帧 p10 在 0~73 码之间跳。
  所以闭环看的是「一轮里所有帧 p10 的中位数」，一轮只决策一次。
* 固件另有 `GAIN HARDWARE`（硬件 AGC），它是为「看得见信号」调优的，不适合当
  「占用率测量」的基准 —— 三种模式各留一条路：

===  ==============  =========================================================
模式 固件命令        说明
===  ==============  =========================================================
AGC  `GAIN HARDWARE` 固件硬件 AGC（默认）。问「这个频段有没有信号」用它。
手动 `GAIN MANUAL n`  固定档位（PHY 表索引，不是 dB）。横向比较必须固定。
自动 主机闭环        每轮取 p10 中位数当底噪，按 `AUTO_STEP` 把底噪压到 `target`。
===  ==============  =========================================================

线程纪律（踩过一次就记住的）：`GAIN` 命令必须在**两次扫描之间**发。扫描中途插一条命令
会把串口读缓冲打乱（实测 `SPEC 启动回复异常`，而且报错里是二进制碎片）。
所以挂起变更只在 :class:`GainAwareSource` 的 ``scan()`` 开头落地，
GUI 线程只往队列里塞意图，绝不直接碰串口。
"""
from __future__ import annotations

import math
import threading
from typing import Any, Iterable, Iterator

#: 三种模式（'AGC' 是固件硬件 AGC，'AUTO' 是主机闭环，串口两边都用 MANUAL/HARDWARE 表达）
MODES = ("HARDWARE", "MANUAL", "AUTO")

#: 底噪参考码取每帧 bins 的 p10（上位机算底噪统一用这个口径，换模块也不改判据）
FLOOR_PCT = 10.0
#: AUTO 的目标底噪参考码：低到不压缩动态范围，又高到弱信号还能看见（实板经验值）
AUTO_TARGET = 4.0
#: 容差：落在 target ± tolerance 就算到位，避免来回抖一档
AUTO_TOLERANCE = 1.5
#: 每次调整的档位数（PHY 表索引步长）
AUTO_STEP = 2
#: 先观察这么多**轮**再开始闭环
AUTO_WARMUP = 2
#: 底噪指数平滑系数（作用在「每轮中位数」上）：实板单帧 p10 会在 0~73 码之间跳，
#: 不平滑就是在追噪声
AUTO_SMOOTH = 0.4
#: 连续多少轮同方向越界才动手：单轮尖峰不调档。
#: 这也是**唯一的调整节奏**——不用墙上时间做冷却：实测每轮约 1.1 s，
#: 设 1.2 s 冷却会在每一轮都被卡住，闭环直接停摆（真板上就这么停的）。
AUTO_CONFIRM = 2
#: 底噪参考码低于这个值视为「这一帧没扫到 / 全零」——实板出现过 0 码帧，
#: 拿它做控制量会把档位往错误方向推
DEGENERATE_FLOOR = 0.5
#: 档位范围的兜底值（真实范围由固件 `GAIN?` 报回来，实板是 0..81）
MIN_INDEX = 0
MAX_INDEX = 90


def percentile(values: Iterable[float], pct: float) -> float:
    """线性插值分位数（不引 numpy：本机没装，也不想为一行统计加依赖）。"""
    data = sorted(float(v) for v in values if v == v)  # NaN 自己不等于自己，正好过滤
    if not data:
        return 0.0
    k = (len(data) - 1) * max(0.0, min(100.0, float(pct))) / 100.0
    lo = int(math.floor(k))
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (k - lo)


def parse_gain_line(line: str) -> dict[str, Any]:
    """解析固件 `GAIN?` 的回复。

    固件格式：``GAIN MANUAL 72 0 72 1``（模式 当前档 最小 最大 步进），
    AGC 时是 ``GAIN HARDWARE -1 ...``。
    """
    text = (line or "").strip()
    if not text.upper().startswith("GAIN"):
        return {"ok": False, "raw": text}
    parts = text.split()
    mode = parts[1].upper() if len(parts) > 1 else ""
    numbers: list[int] = []
    for token in parts[2:]:
        try:
            numbers.append(int(float(token)))
        except ValueError:
            pass
    index = numbers[0] if numbers else -1
    out: dict[str, Any] = {"ok": mode in ("HARDWARE", "MANUAL"), "mode": mode, "index": index, "raw": text}
    if len(numbers) >= 4:
        out["min_index"] = numbers[1]
        out["max_index"] = numbers[2]
        out["step"] = numbers[3]
    return out


def clamp_index(index: int, lo: int = MIN_INDEX, hi: int = MAX_INDEX) -> int:
    return max(int(lo), min(int(hi), int(index)))


class GainControl:
    """一块共享状态：GUI 线程写意图，射频线程落地并读底噪做闭环。

    * GUI 线程只调 ``request_*``（加锁塞队列，绝不碰串口）；
    * 射频线程在两次扫描之间调 ``apply()``，每帧调 ``observe()``；
    * 任何线程都能调 ``state()`` 拿一份可 JSON 化的快照（前端显示用）。
    """

    def __init__(
        self,
        mode: str = "HARDWARE",
        index: int | None = None,
        target: float = AUTO_TARGET,
        step: int = AUTO_STEP,
        mock: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self.mode = mode.upper() if (mode or "").upper() in MODES else "HARDWARE"
        #: 档位范围：默认兜底值，`open()` 里会拿固件 `GAIN?` 的真实范围覆盖
        self.min_index = MIN_INDEX
        self.max_index = MAX_INDEX
        self.index = clamp_index(index if index is not None else 60, self.min_index, self.max_index)
        self.target = float(target)
        self.step = max(1, int(step))
        self.mock = bool(mock)
        #: 生效过的档位（来自每帧 meta 的 gain_code；AGC 下是固件自己挑的）
        self.effective: int | None = None
        #: 最近一帧的底噪参考码（p10）
        self.floor_code: float | None = None
        #: 平滑后的底噪（闭环真正用的控制量）
        self.floor_smooth: float | None = None
        self.samples = 0
        self.scans = 0
        self.adjusts = 0
        self.note = ""
        self.history: list[dict[str, Any]] = []
        self._pending: list[tuple[str, int | None]] = []
        self._out_of_band = 0  # 连续越界轮数（见 AUTO_CONFIRM）
        self._scan_floors: list[float] = []  # 本轮的逐帧底噪，轮末取中位数

    def set_limits(self, info: dict[str, Any] | None) -> None:
        """用固件 `GAIN?` 报的 [min, max] 覆盖兜底范围（实板是 0..81）。

        为什么必须问固件：不同模组/固件版本 PHY 表长度不同，写死 90 会给出
        「调了但固件不认」的档位——AUTO 闭环会一直在那个无效档位上打转。
        """
        if not isinstance(info, dict):
            return
        lo = info.get("min_index")
        hi = info.get("max_index")
        with self._lock:
            if isinstance(lo, int) and isinstance(hi, int) and 0 <= lo < hi:
                self.min_index, self.max_index = lo, hi
                self.index = clamp_index(self.index, lo, hi)

    # ---- GUI 线程：写意图 ------------------------------------------------
    def request_mode(self, mode: str) -> dict[str, Any]:
        want = (mode or "").upper()
        if want not in MODES:
            return self.state(message=f"未知增益模式：{mode}")
        with self._lock:
            self.mode = want
            self.adjusts = 0
            self.history = []
            self._out_of_band = 0
            if want == "HARDWARE":
                self._pending.append(("HARDWARE", None))
                self.note = "固件硬件 AGC：由固件自己挑档（测「有没有信号」用它）"
            else:
                self._pending.append(("MANUAL", self.index))
                self.note = (
                    f"自动闭环：目标底噪 {self.target:.0f} 码，步进 {self.step}"
                    if want == "AUTO"
                    else f"手动固定档位 {self.index}"
                )
        return self.state()

    def request_step(self, delta: int) -> dict[str, Any]:
        """±1 档。AGC / 自动模式下一按就切手动（否则闭环会立刻跟你对打）。"""
        with self._lock:
            new_index = clamp_index(self.index + int(delta), self.min_index, self.max_index)
            if new_index == self.index and self.mode != "AUTO":
                self.note = "已经到档位边界" if delta else self.note
            self.index = new_index
            # 按 ± 就是「我要自己定档」：AGC 和自动都退出，否则自动闭环会立刻跟用户对打
            self.mode = "MANUAL"
            self._pending.append(("MANUAL", new_index))
            self.note = f"手动固定档位 {new_index}（已退出手动之外的模式）"
        return self.state()

    def request_index(self, index: int) -> dict[str, Any]:
        with self._lock:
            self.index = clamp_index(index, self.min_index, self.max_index)
            self.mode = "MANUAL"
            self._pending.append(("MANUAL", self.index))
            self.note = f"手动固定档位 {self.index}"
        return self.state()

    def request_target(self, target: float) -> dict[str, Any]:
        with self._lock:
            self.target = max(0.0, float(target))
            self.mode = "AUTO"
            self._out_of_band = 0
            self._pending.append(("MANUAL", self.index))
            self.note = f"自动闭环：目标底噪 {self.target:.0f} 码，步进 {self.step}"
        return self.state()

    # ---- 射频线程：落地 + 闭环 -------------------------------------------
    def take_pending(self) -> list[tuple[str, int | None]]:
        with self._lock:
            ops, self._pending = self._pending, []
        return ops

    def apply(self, link: Any) -> None:
        """把挂起的增益变更落到固件上（**只在两次扫描之间调**）。"""
        for mode, index in self.take_pending():
            try:
                link.set_gain(mode if index is None else "MANUAL", index)
            except Exception as exc:  # noqa: BLE001 - 一次设不上不该停掉整个采集
                with self._lock:
                    self.note = f"设增益失败：{type(exc).__name__}: {exc}"
                continue
            with self._lock:
                if index is not None:
                    self.index = clamp_index(index, self.min_index, self.max_index)

    def observe(self, snap: Any) -> None:
        """每帧调用：只记录，不做决定（决定在轮末 `end_scan()` 做一次）。"""
        bins = getattr(snap, "bins_db", None) or []
        floor = percentile(bins, FLOOR_PCT) if bins else None
        meta = getattr(snap, "meta", None) or {}
        code = meta.get("gain_code")
        with self._lock:
            if isinstance(code, int):
                self.effective = code
            self.samples += 1
            if floor is None:
                return
            self.floor_code = round(floor, 1)
            if floor >= DEGENERATE_FLOOR:
                # 全零 / 没扫到的帧不进控制量（实板单帧 p10 会在 0 和 73 之间跳）
                self._scan_floors.append(floor)

    def end_scan(self) -> None:
        """一轮扫完调一次：拿本轮中位数做闭环决策。

        **为什么不在每帧决定**：实板实测单帧底噪 p10 在 0~73 码之间跳
        （同一档位、同一频点、连续 14 轮，最大值 73、最小值 0）。
        拿单帧当控制量等于拿噪声追噪声 —— 实测 14 轮里只在第 1、12 轮各调一次，
        而且方向全靠那一帧的运气。取一轮中位数才是「这一档位下真实的底噪」。
        """
        with self._lock:
            floors, self._scan_floors = self._scan_floors, []
            self.scans += 1
            if not floors:
                return
            med = percentile(floors, 50)
            self.floor_smooth = (
                med if self.floor_smooth is None
                else (1.0 - AUTO_SMOOTH) * self.floor_smooth + AUTO_SMOOTH * med
            )
            if self.mode != "AUTO" or self.mock:
                return
            if self.scans <= AUTO_WARMUP:
                return
            floor = self.floor_smooth
            delta = 0
            if floor > self.target + AUTO_TOLERANCE:
                delta = -self.step
            elif floor < self.target - AUTO_TOLERANCE:
                delta = self.step
            if not delta:
                self._out_of_band = 0
                self.note = f"自动闭环已到位：底噪 {floor:.1f} 码 ≈ 目标 {self.target:.0f}"
                return
            self._out_of_band += 1
            if self._out_of_band < AUTO_CONFIRM:
                return  # 先确认这不是一轮尖峰
            new_index = clamp_index(self.index + delta, self.min_index, self.max_index)
            if new_index == self.index:
                self.note = (
                    f"已到档位边界（{new_index}），底噪 {floor:.1f} 码 ≠ 目标 {self.target:.0f}"
                )
                self._out_of_band = 0
                return
            self.index = new_index
            self._pending.append(("MANUAL", new_index))
            self._out_of_band = 0
            self.adjusts += 1
            self.history.append(
                {"scan": self.scans, "from_floor": round(floor, 1), "index": new_index}
            )
            del self.history[:-20]
            self.note = f"自动闭环：底噪 {floor:.1f} → 目标 {self.target:.0f}，档位调到 {new_index}"

    # ---- 任何线程：读状态 ------------------------------------------------
    def state(self, message: str = "") -> dict[str, Any]:
        with self._lock:
            return {
                "kind": "gain",
                "mode": self.mode,
                "index": self.index,
                "effective": self.effective,
                "floor_code": self.floor_code,
                "floor_smooth": None if self.floor_smooth is None else round(self.floor_smooth, 1),
                "target": self.target,
                "tolerance": AUTO_TOLERANCE,
                "step": self.step,
                "min_index": self.min_index,
                "max_index": self.max_index,
                "samples": self.samples,
                "scans": self.scans,
                "adjusts": self.adjusts,
                "history": list(self.history),
                "note": message or self.note,
                "mock": self.mock,
                "ok": not message,
            }


class GainAwareSource:
    """包一层扫描源：**两次扫描之间**落地增益变更，每帧回读底噪。

    ``Scheduler`` 只要求源对象有 ``scan(request)``，包一层不用改 ``core/``。
    """

    def __init__(self, link: Any, gain: GainControl, round_ctl: Any = None) -> None:
        self.link = link
        self.gain = gain
        #: 圆屏轮播控制器（可选；同一个扫描间隙落地，见 round_ctl.py）
        self.round_ctl = round_ctl

    def scan(self, request: Any) -> Iterator[Any]:
        self.gain.apply(self.link)
        if self.round_ctl is not None:
            self.round_ctl.apply(self.link)
        try:
            for snap in self.link.scan(request):
                self.gain.observe(snap)
                yield snap
        finally:
            # 一轮扫完（无论是跑完、被 break 还是抛错）都要结算，否则这一轮的样本白攒，
            # 而且 `_scan_floors` 会跨轮累积、把窗口外的旧数据混进中位数
            self.gain.end_scan()

    def __getattr__(self, name: str) -> Any:  # 其余属性透传给真链路（identity/caps/...）
        return getattr(self.link, name)