"""假快照源：无硬件也能跑通全链路、验证每个功能的判据。

这是后面所有功能"无硬件自测"的基础设施，必须先于功能实现。
合成器按"物理上说得通"的方式堆信号：WiFi 突发块、BLE 窄脉冲、
微波炉宽带抬升、5.8G 窄带持续源、未知窄带 CW。功率码与真实固件同域
（0..255 的相对 dB 码），所以各功能的阈值不需要为 mock 特调。
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

from core.registry import ScanRequest
from core.snapshot import Snapshot

#: 2.4G 信道 → 中心频率
WIFI_24 = {ch: 2412 + 5 * (ch - 1) for ch in range(1, 14)}
#: 5G 常用信道 → 中心频率（20 MHz 栅格，跳过 DFS 细节）
WIFI_5 = {}
for _ch in list(range(36, 65, 4)) + list(range(100, 145, 4)) + list(range(149, 166, 4)):
    WIFI_5[_ch] = 5000 + 5 * _ch


def _sigmoid(x: float) -> float:
    if x >= 50.0:
        return 1.0
    if x <= -50.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _block(freq: float, f0: float, f1: float, edge_mhz: float = 1.0) -> float:
    """带软边的矩形窗（0..1），模拟信道滤波后的边沿。"""
    edge = max(1e-9, edge_mhz * 1e6) / 4.0
    return _sigmoid((freq - f0) / edge) * _sigmoid((f1 - freq) / edge)


def _gauss(freq: float, center: float, sigma_hz: float) -> float:
    return math.exp(-0.5 * ((freq - center) / sigma_hz) ** 2)


@dataclass
class Signal:
    """合成器里的一条"发射源"。"""

    kind: str  # wifi / ble / microwave / cw / wide
    f0: float  # 起始频率
    f1: float  # 结束频率（窄带时 = f0）
    level_db: float  # 相对噪声底的抬升量（功率码域）
    duty: float = 1.0  # 出现概率（模拟突发/跳频）
    sigma_mhz: float = 0.0  # 窄带高斯 sigma；0 表示用 f0..f1 软边矩形


@dataclass
class Scenario:
    name: str
    description: str
    signals: list[Signal] = field(default_factory=list)
    floor: float = 22.0
    spread: float = 3.5


def _wifi(center_mhz: float, level: float, width_mhz: float = 18.0, duty: float = 0.85) -> Signal:
    half = width_mhz / 2
    return Signal(
        "wifi",
        (center_mhz - half) * 1e6,
        (center_mhz + half) * 1e6,
        level,
        duty,
        sigma_mhz=0.0,
    )


def _ble(channel_mhz: float, level: float = 62.0, duty: float = 0.30) -> Signal:
    return Signal("ble", channel_mhz * 1e6, channel_mhz * 1e6, level, duty, sigma_mhz=1.2)


def _cw(freq_mhz: float, level: float = 80.0, sigma_mhz: float = 0.35) -> Signal:
    return Signal("cw", freq_mhz * 1e6, freq_mhz * 1e6, level, 1.0, sigma_mhz=sigma_mhz)


def _microwave(level: float = 42.0) -> Signal:
    return Signal("microwave", 2430e6, 2490e6, level, 0.6, sigma_mhz=0.0)


SCENARIOS: dict[str, Scenario] = {
    "quiet": Scenario("quiet", "只有噪声底，用于验证零误报", []),
    "wifi_busy": Scenario(
        "wifi_busy",
        "2.4G ch1/ch6 强 WiFi + 5G ch149 热点（任务 1/3 判据）",
        [
            _wifi(2412, 96.0, 20.0, 0.95),
            _wifi(2437, 74.0, 20.0, 0.9),
            _wifi(2462, 40.0, 20.0, 0.8),
            _wifi(5745, 88.0, 20.0, 0.9),
            _wifi(5180, 30.0, 20.0, 0.7),
        ],
    ),
    "wifi_suppress_ble": Scenario(
        "wifi_suppress_ble",
        "宽带 WiFi 占满 2.4G（BLE 风险评估的『压制型』场景）",
        [
            _wifi(2412, 92.0, 22.0, 0.98),
            _wifi(2437, 90.0, 22.0, 0.98),
            _wifi(2462, 88.0, 22.0, 0.98),
            _ble(2402, 30.0, 0.2),
            _ble(2426, 30.0, 0.2),
            _ble(2480, 28.0, 0.2),
        ],
    ),
    "ble_crowded": Scenario(
        "ble_crowded",
        "WiFi 空闲 + BLE 三个广播信道密集脉冲（『设备密度型』场景）",
        [
            _ble(2402, 68.0, 0.55),
            _ble(2426, 70.0, 0.60),
            _ble(2480, 66.0, 0.50),
            _ble(2418, 40.0, 0.35),
            _ble(2450, 38.0, 0.30),
        ],
    ),
    "microwave": Scenario(
        "microwave",
        "微波炉宽带噪声抬升 + 偶发 WiFi（任务 2 瀑布图判据）",
        [
            _microwave(46.0),
            _wifi(2437, 62.0, 20.0, 0.55),
            _ble(2402, 45.0, 0.2),
        ],
    ),
    "video_58g": Scenario(
        "video_58g",
        "5.8G 5740MHz 图传/模拟摄像头持续窄带（任务 4 告警判据）",
        [
            Signal("wide", 5728e6, 5752e6, 30.0, 1.0, sigma_mhz=0.0),
            _cw(5740.0, 84.0, 0.5),
            _wifi(5745, 40.0, 20.0, 0.4),
        ],
    ),
    "unknown_cw": Scenario(
        "unknown_cw",
        "未知窄带持续源（任务 5 抓拍判据）+ 已知 WiFi/BLE 背景",
        [
            _wifi(2412, 78.0, 20.0, 0.7),
            _ble(2426, 58.0, 0.4),
            _cw(2413.0, 82.0, 0.25),
            _cw(5765.0, 86.0, 0.25),
        ],
    ),
    "unknown_cw_clean": Scenario(
        "unknown_cw_clean",
        "干净频点上的未知窄带源（任务 5 抓拍判据）：WiFi ch1 + BLE 当背景，2454 MHz 一片没人的地方有一根 CW",
        [
            _wifi(2412, 80.0, 20.0, 0.7),
            _ble(2426, 58.0, 0.4),
            _cw(2454.0, 82.0),
        ],
    ),
    "mixed": Scenario(
        "mixed",
        "全场景混合：WiFi/BLE/微波炉/5.8G 图传/未知源（默认演示）",
        [
            _wifi(2412, 88.0, 20.0, 0.9),
            _wifi(2437, 66.0, 20.0, 0.75),
            _ble(2402, 55.0, 0.45),
            _ble(2426, 58.0, 0.45),
            _ble(2480, 50.0, 0.40),
            _microwave(34.0),
            _cw(5740.0, 82.0, 0.5),
            Signal("wide", 5728e6, 5752e6, 22.0, 0.8, sigma_mhz=0.0),
            _cw(2413.0, 44.0, 0.25),
        ],
    ),
    # ── 酒店反偷拍「整场」仿真（任务 4 + 任务 5 的合并正样本）──────────────
    # 手上一台真的 5.8G 图传、一台真的偷拍摄像头都没有，所以「能发现」这件事
    # 只能靠仿真自证：背景是普通 WiFi/BLE（= 已知类型不该被冤枉），
    # 5.8G 放一台模拟图传（宽平顶 + 载波），2.4G 干净处放一根窄带未知源。
    # 期望：5.8G 守卫报警（任务 4）+ 2.4G 未知源被抓档（任务 5），两个都中才算「能发现」。
    "hotel_scan": Scenario(
        "hotel_scan",
        "酒店反偷拍整场仿真：5.8G 模拟图传 + 2.4G 隐藏窄带源，背景普通 WiFi/BLE",
        [
            _wifi(2412, 80.0, 20.0, 0.7),
            _ble(2426, 56.0, 0.35),
            _cw(2454.0, 82.0),
            Signal("wide", 5728e6, 5752e6, 30.0, 1.0, sigma_mhz=0.0),
            _cw(5740.0, 84.0, 0.5),
        ],
    ),
}


class SpectrumSynthesizer:
    """把 Scenario 渲染成某个频点/带宽下的功率码数组。"""

    def __init__(
        self,
        scenario: str = "mixed",
        seed: int = 1234,
        floor: float | None = None,
        spread: float | None = None,
    ) -> None:
        if scenario not in SCENARIOS:
            raise KeyError(f"未知场景：{scenario}（可选 {sorted(SCENARIOS)}）")
        self.scenario = SCENARIOS[scenario]
        self._rng = random.Random(seed)
        self.floor = self.scenario.floor if floor is None else floor
        self.spread = self.scenario.spread if spread is None else spread
        self._active: dict[int, bool] = {}

    def _is_on(self, index: int, signal: Signal) -> bool:
        """按 duty 决定这一次快照里该信号是否出现（同一信号在相邻快照间相关）。"""
        if signal.duty >= 1.0:
            return True
        if signal.duty <= 0.0:
            return False
        key = id(signal)
        if key not in self._active or self._rng.random() > 0.65:
            self._active[key] = self._rng.random() < signal.duty
        return self._active[key]

    def codes(
        self,
        center_hz: int,
        bw_hz: int,
        bins: int,
        timestamp: float | None = None,
        jitter: bool = True,
    ) -> tuple[int, ...]:
        step = bw_hz / bins
        start = center_hz - bw_hz / 2
        out: list[int] = []
        for index in range(bins):
            freq = start + (index + 0.5) * step
            value = self.floor + (self._rng.gauss(0.0, self.spread) if jitter else 0.0)
            for signal in self.scenario.signals:
                if not self._is_on(index, signal):
                    continue
                if signal.sigma_mhz:
                    weight = _gauss(freq, signal.f0, signal.sigma_mhz * 1e6)
                    # 窄带源在"命中 bin"时整体抬高，避免少于一个 bin 时消失
                    if weight < 1e-3 and abs(freq - signal.f0) > step * 0.75:
                        continue
                    weight = max(weight, 0.6 if abs(freq - signal.f0) <= step else 0.0)
                else:
                    weight = _block(freq, signal.f0, signal.f1)
                if weight <= 0.01:
                    continue
                value += signal.level_db * weight
            out.append(max(0, min(255, int(round(value)))))
        return tuple(out)


class MockSource:
    """``core.scheduler.Source`` 的假实现：按请求生成假快照。"""

    def __init__(
        self,
        scenario: str = "mixed",
        seed: int = 1234,
        frames_per_scan: int = 2,
        frame_interval_s: float = 0.02,
        realtime: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        synthesizer: SpectrumSynthesizer | None = None,
        virtual_step_s: float = 0.0,
    ) -> None:
        self.synth = synthesizer or SpectrumSynthesizer(scenario, seed)
        self.scenario = scenario
        self.frames_per_scan = max(1, frames_per_scan)
        self.frame_interval_s = frame_interval_s
        self.realtime = realtime
        self._sleep = sleep
        self._sleep_total = 0.0
        #: 每帧额外推进的**假墙钟**（不真睡）。给「非实时但要在几秒内攒够跨度」的
        #: 场景用：抓档的 ``MIN_SPAN_S`` 是按快照 ts 的跨度算的，非实时源默认
        #: 所有帧挤在一起 → 永远攒不够 3 s → 矩阵里一条都抓不到（假红）。
        self.virtual_step_s = max(0.0, float(virtual_step_s))
        self._virtual_total = 0.0
        self._seq = 0
        self.scans = 0

    def scan(self, request: ScanRequest) -> Iterator[Snapshot]:
        self.scans += 1
        for frame in range(self.frames_per_scan):
            self._seq += 1
            codes = self.synth.codes(request.freq_hz, request.bw_hz, request.bins)
            now = time.time() + self._sleep_total + self._virtual_total
            yield Snapshot(
                ts=now,
                center_hz=request.freq_hz,
                bw_hz=request.bw_hz,
                bins_db=codes,
                meta={
                    "seq": self._seq,
                    "frame": frame,
                    "source": "mock",
                    "scenario": self.scenario,
                    "dwell_ms": request.dwell_ms,
                    "detector": "max" if request.detector else "mean",
                    "snapshot_gaps": True,
                },
            )
            if self.realtime and self.frame_interval_s:
                self._sleep(self.frame_interval_s)
                self._sleep_total += self.frame_interval_s
            self._virtual_total += self.virtual_step_s


def make_source(scenario: str = "mixed", realtime: bool = False, **kwargs: Any) -> MockSource:
    return MockSource(scenario=scenario, realtime=realtime, **kwargs)


__all__ = [
    "MockSource",
    "SCENARIOS",
    "Scenario",
    "Signal",
    "SpectrumSynthesizer",
    "WIFI_24",
    "WIFI_5",
    "make_source",
]