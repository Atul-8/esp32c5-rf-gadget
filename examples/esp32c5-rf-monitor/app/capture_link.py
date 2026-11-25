"""任务 5 的 app 层胶水：把「模块排队」和「串口真抓」接起来。

为什么要这一层而不是让 ``modes/capture.py`` 自己发命令：

* ``modes/`` 不许 import 串口（单测要能塞假函数，也不会把 pyserial 拖进模块）；
* **抓档只能在两次扫描之间做**。同一根串口上一边扫一边抓会打乱读缓冲
  （增益那轮已经踩过一次，见 ``host/gain.py`` 的 ``GainAwareSource``）。
  做法一样：包一层 ``Source``，在一次 ``scan()`` 迭代结束之后才去取队列。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Sequence

from core.registry import ScanRequest
from core.snapshot import Snapshot


@dataclass
class FakeCapture:
    """无硬件时用的假抓档（真数字段与 ``core.serial_link.IqCapture`` 同名）。"""

    samples_i: tuple[int, ...]
    samples_q: tuple[int, ...]
    bits: int
    rate_code: int
    duration_us: int
    freq_hz: int = 0
    #: 假抓档独有：长相标签（``cw`` / ``wideband`` / ``burst``），真机没有这个字段
    profile: str = "cw"

    @property
    def sample_rate_hz(self) -> int:
        from core.serial_link import RATE_TABLE

        return RATE_TABLE.get(self.rate_code, 0)


#: 假抓档的几种「长相」。**不是信号仿真**，只保证不同场景抓出来的 IQ 彼此可区分，
#: 这样上位机/回放工具能验证「分类 → 抓档 → 存档」这条链真的带着场景特征走。
#: ``cw`` = 纯净窄带载波（对应可疑 CW）；``wideband`` = 占满频带的平坦噪声
#: （对应微波炉/图传）；``burst`` = 半段静默半段载波（对应间歇发射）。
CAPTURE_PROFILES = ("cw", "wideband", "burst")


def profile_for(scenario: str | None) -> str:
    """按 mock 场景挑假抓档长相（默认 ``cw``）。"""
    name = (scenario or "").strip().lower()
    if "video" in name or "microwave" in name or "wifi" in name:
        return "wideband"
    if "burst" in name or "hop" in name:
        return "burst"
    return "cw"


def synthetic_capture(
    freq_hz: int,
    samples: int = 16_384,
    rate_code: int = 2,
    bits: int = 20,
    amplitude: float = 260.0,
    noise: float = 12.0,
    seed: int = 20240101,
    profile: str = "cw",
) -> FakeCapture:
    """造一段假 I/Q，只为跑通链路（不是信号仿真）。

    ``profile`` 见 :data:`CAPTURE_PROFILES`：三种长相的判别特征分别是
    「单一强单音」「谱很平」「前半段有信号后半段近乎噪声」，
    回放/断言只依赖这些粗特征，不做任何数值级仿真。
    """
    from core.serial_link import RATE_TABLE

    if profile not in CAPTURE_PROFILES:
        raise ValueError(f"未知 profile：{profile}（可选 {CAPTURE_PROFILES}）")
    rate = RATE_TABLE.get(rate_code, 20_000_000)
    rng = random.Random(seed)
    omega = 2.0 * math.pi * 0.37  # 归一化频率（0.37 cycle/sample）
    limit = 2 ** (bits - 1) - 1 if bits == 20 else 127
    half = samples // 2
    out_i: list[int] = []
    out_q: list[int] = []
    for n in range(samples):
        off_i = rng.gauss(0.0, noise)
        off_q = rng.gauss(0.0, noise)
        if profile == "cw":
            value_i = amplitude * math.cos(omega * n) + off_i
            value_q = amplitude * math.sin(omega * n) + off_q
        elif profile == "wideband":
            # 平谱近似：每个样本独立同分布 → 频谱白、无单根尖峰
            value_i = amplitude * 0.55 * rng.gauss(0.0, 1.0)
            value_q = amplitude * 0.55 * rng.gauss(0.0, 1.0)
        else:  # burst：前一半有载波，后一半只有噪声
            if n < half:
                value_i = amplitude * math.cos(omega * n) + off_i
                value_q = amplitude * math.sin(omega * n) + off_q
            else:
                value_i = off_i
                value_q = off_q
        out_i.append(max(-limit, min(limit, int(round(value_i)))))
        out_q.append(max(-limit, min(limit, int(round(value_q)))))
    return FakeCapture(
        samples_i=tuple(out_i),
        samples_q=tuple(out_q),
        bits=bits,
        rate_code=rate_code,
        duration_us=int(samples / rate * 1e6),
        freq_hz=freq_hz,
        profile=profile,
    )


class CaptureAwareSource:
    """包一层 ``Source``：每次扫描结束后清空抓档队列（保证抓档不落在扫描中途）。"""

    def __init__(
        self,
        source: Any,
        plugins: Sequence[Any],
        capture_fn: Callable[..., Any],
        max_per_slice: int = 1,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.source = source
        self.plugins = list(plugins)
        self.capture_fn = capture_fn
        self.max_per_slice = max(1, int(max_per_slice))
        self._log = logger or (lambda _msg: None)
        self.done = 0
        self.failed = 0

    # ---- Source 协议 -----------------------------------------------------
    def scan(self, request: ScanRequest) -> Iterator[Snapshot]:
        yield from self.source.scan(request)
        self.flush()

    def flush(self) -> int:
        """把排队中的抓档做掉；返回本次成功落盘的条数。"""
        count = 0
        for plugin in self.plugins:
            take = getattr(plugin, "take_pending", None)
            if not callable(take):
                continue
            # 优先让模块自己按 limit 取（剩下的留队）；老插件不带参数就退回切片
            try:
                batch = take(self.max_per_slice)
            except TypeError:
                batch = take()[: self.max_per_slice]
            for request in batch:
                try:
                    capture = self.capture_fn(
                        request.freq_hz, request.samples, request.rate_code, request.bits
                    )
                except Exception as exc:  # noqa: BLE001 - 单次抓档失败不许拖垮采集
                    self.failed += 1
                    errors = getattr(plugin, "errors", None)
                    if isinstance(errors, list):
                        errors.append(f"capture: {type(exc).__name__}: {exc}")
                    self._log(f"[capture] 抓档失败 {request.freq_hz/1e6:.0f} MHz：{type(exc).__name__}: {exc}")
                    continue
                record = plugin.deliver(request, capture)
                if record is not None:
                    self.done += 1
                    count += 1
                    self._log(
                        f"[capture] CAP-{record.index:03d} {record.freq_hz/1e6:.1f} MHz "
                        f"{record.samples} 样本 / {record.label} → {record.path or '(未落盘)'}"
                    )
        return count

    # ---- 透传给上层诊断用 ------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)


def iter_capture_plugins(registry: Iterable[Any]) -> list[Any]:
    """挑出支持抓档的插件（有 ``take_pending`` 的就是）。"""
    return [plugin for plugin in registry if callable(getattr(plugin, "take_pending", None))]


__all__ = [
    "CAPTURE_PROFILES",
    "CaptureAwareSource",
    "FakeCapture",
    "iter_capture_plugins",
    "profile_for",
    "synthetic_capture",
]