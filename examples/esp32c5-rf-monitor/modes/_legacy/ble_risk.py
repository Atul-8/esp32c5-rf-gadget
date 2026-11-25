"""功能 3：BLE 广播信道风险评估（2402 / 2426 / 2480 MHz 三个广播信道）。

只用 ``core`` 暴露的接口：
  * ``Snapshot.bins_db`` / ``band_indices()`` 做分带与判决
  * ``ScanRequest`` 声明驻留计划
  * ``Output`` 交付表格

判据（可自测，见 tests/test_ble_risk.py）：
  * ``quiet``              : 无窄带凸起、宽带占用低 → 「无明显风险」，评分接近 0
  * ``ble_crowded``        : 三个广播信道密集窄脉冲 + WiFi 空闲 → 「蓝牙设备密度型」，评分显著高于 quiet
  * ``wifi_suppress_ble``  : 2.4G 被宽带 WiFi 占满、BLE 脉冲被埋在底下 → 「WiFi 压制型」

为什么这样判（实板噪声底自身抖动 ±10~18 dB，绝对门限会把纯噪声判成忙，任务 1 已经踩过）：
  1. **有信号** = 本通道 **p80** 电平比**本帧 p10** 高 ≥ ``PRESENCE_DB``（同帧自比，跨增益也能用）
  2. **像 BLE** = 本通道 p80 电平比**肩部**（±4~8 MHz，BLE 通道之外）高 ≥ ``PROMINENCE_DB``，
     且这段凸起要占本次抬升的 ≥ ``NARROW_RATIO``（能量集中在窄通道里才叫窄带源）
     —— BLE 广播是 2 MHz 栅格上的窄脉冲；WiFi 是 18~22 MHz 宽带，抬的是整片而不是一个 2 MHz 尖。
  3. 「有信号但不像 BLE」= 窄带凸起被同频宽带埋掉 → 记 ``masked``，这是「压制」的判据来源。

边界（写进输出注释，不许美化）：
  * **只评概率，不做设备级归因**：看得见的是"这里在动、动得像不像 BLE"，不是"几台什么设备"。
  * 功率码是相对 dB 码，不是 dBm；单射频分时扫描，帧与帧之间有接收空档。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from core.registry import ModePlugin, Output, ScanRequest
from core.snapshot import Snapshot
from output.format import ascii_table, bar, fmt_hz, fmt_pct

# ---------------------------------------------------------------------------
# BLE 信道表（40 个通道：37 / 38 / 39 是广播信道，其余是数据信道）
# ---------------------------------------------------------------------------

#: 广播信道 → 中心频率
ADV_CHANNELS = {37: 2_402_000_000, 38: 2_426_000_000, 39: 2_480_000_000}
ADV_ORDER = (37, 38, 39)
#: 数据信道 0..36 的中心频率栅格（2 MHz 步进）
BLE_STEP_HZ = 2_000_000
BLE_DATA_FIRST_HZ = 2_404_000_000
BLE_DATA_LAST_HZ = 2_478_000_000

#: 2.4G ISM 频段（判读范围；2483.5 以上不算 BLE 活动）
BAND_HZ = (2_402_000_000, 2_483_500_000)

#: 扫描窗：20 MHz 窗（20 MS/s 实测满带宽）+ 15 MHz 步进（相邻窗重叠 5 MHz）
SCAN_BW_HZ = 20_000_000
USABLE_HALF_HZ = SCAN_BW_HZ // 2
SCAN_STEP_HZ = 15_000_000
DWELL_MS = 100
BINS = 1024

#: BLE 1 Msym/s 的占用约 ±1 MHz
ADV_HALF_HZ = 1_000_000
#: 肩部取值区间（相对通道中心）：避开 BLE 自身，又离得够近能代表"同一片背景"
SHOULDER_HZ = (4_000_000, 8_000_000)
#: 数据信道栅格 2 MHz → 半宽 1 MHz 恰好一格
SLOT_HALF_HZ = 1_000_000

#: 通道被实测覆盖到多少才算「有覆盖」（否则样本不足，不给结论）
MIN_COVERAGE = 0.60
#: 整窗最大码仍是 0 ⇒ 这一帧没有有效测量（增益未稳/没收到），丢弃
DEGENERATE_MAX_CODE = 0
#: 本通道中位电平 − 本帧 p10 ≥ 它 ⇒ 这次「有信号」
PRESENCE_DB = 12.0
#: 「浅抬升」下界：高于 p10 这么多但又没到 ``PRESENCE_DB``，算「有东西、但还不足以判定有信号」。
#: 这一档存在的原因：完全安静的底噪和「很浅的干扰」必须分得开（前者 rise 基本 ≤4 dB）。
PRESENCE_LIGHT_DB = 4.0
#: 浅抬升占 ≥ 这个比例 ⇒ 算一类证据（足以与完全安静区分）
LIGHT_RATIO = 0.10
#: 浅抬升证据的最小样本量：太少帧时几十次抖动就能凑出 20%「浅抬升」，
#: 拿小样本下判词等于把噪声当结论（实测 `--slices 12` 的 quiet 会翻成「轻微干扰」）。
MIN_ADV_FRAMES = 60
#: 每帧 p10 参考码高于它 ⇒ 前端底噪已经被增益抬起来了，
#: 宽带占用率会随增益变化（实板实测：增益 20/40/60 → 27%/61%/85%），必须低增益复测。
HEADROOM_CODES = 12.0
#: 本通道中位电平 − 肩部中位电平 ≥ 它 ⇒ 这次是「窄带凸起」（像 BLE）
PROMINENCE_DB = 8.0
#: 窄带凸起必须占这次抬升的多少比例才算「窄带源」
#: （宽带源把能量摊到肩部，通道只比肩部高一点 → 占比低 ⇒ 判成被压制而不是 BLE）
NARROW_RATIO = 0.50
#: 单帧里「高于 p10 + 10 dB」的 bin 占比均值 ≥ 它 ⇒ 2.4G 被宽带源占住
WIDEBAND_EXCESS_DB = 10.0
#: 宽带占用率**在时间均值频谱上算**，不在单帧上算：实板噪声底跨 bin 抖动很大
#: （实测单帧 p10≈20 / p50≈28 / p90≈41），固定门限 ``p10+10`` 会让 **37%** 的纯噪声 bin
#: 越线，安静环境直接被判成「2.4G 全被占」。多帧取均值后抖动按 1/√N 收敛，
#: 剩下的抬升就是真占用（宽带源稳定在均值里，BLE 脉冲只占 2 MHz / 20 MHz，摊不动整窗）。
WIDEBAND_FRAC = 0.50
#: 「宽带占比高的频点」在**时间上**的开关幅度（功率码）要 ≥ 它，才算「随时间开关的宽带源」。
#: 只比静态幅度会漏一条：前端滤波器的固定起伏（形状）也能让 85% 的频点高于底噪，
#: 但那批频点在时间上**一动不动**（幅度 ≈ 噪声抖动）。把「随时间开关」单列成判据，
#: 静态形状就不会被误判成「WiFi 压制型」—— 输出逻辑才算闭合（仿真见 tests 的 StaticShapeTest）。
CONTRAST_DB = 8.0
#: 评分分档（0..100）
LEVELS = ((70.0, "重度"), (40.0, "拥挤"), (15.0, "轻度"), (0.0, "空闲"))
#: WiFi 2.4G 信道（用于共存提示，不参与 BLE 判决）
WIFI_CHANNELS_24 = {ch: 2412 + 5 * (ch - 1) for ch in range(1, 14)}
WIFI_MEASURE_HALF_HZ = 9_000_000   # 测电平：±9 MHz，避开邻道能量泄漏
WIFI_SPAN_HALF_HZ = 10_000_000     # 重叠几何：20 MHz 信道真实占用 ±10 MHz
#: WiFi 信道里高于「本帧 p10 + 8 dB」的 bin 占比 ≥ 它 ⇒ 这个 WiFi 信道在用。
#: 用**占用比例**而不是电平：窄带 BLE 脉冲也能把 18 MHz 窗口的 p80 顶起来，
#: 但只占 2 MHz / 18 MHz ≈ 11%，宽带 WiFi 能占 80% 以上（2026-11 实测踩到误判）。
WIFI_BUSY_DB = 8.0
WIFI_BUSY_FILL = 0.50

RISK_NONE = "无明显风险"
RISK_WIFI = "WiFi 压制型"
RISK_BLE = "蓝牙设备密度型"
RISK_MIXED = "混合型"
RISK_NO_DATA = "无数据"
#: 宽带占用高、但随时间不开关：可能是前端形状 / 常开宽带源，**不下 WiFi 结论**
RISK_FLAT = "宽带常开/形状疑似"
#: 只有浅抬升、没有任何达标的窄带命中 ⇒ 有东西在动，但判不了是不是 BLE
RISK_LIGHT = "轻微干扰"
#: 多类证据同时成立（宽带 + 栅格占用 + 浅抬升…）⇒ 复杂噪音，不给单一归因
RISK_COMPLEX = "复杂噪音（多源混合）"

#: 判词 → 一句话结论的**档位**：risk = 有风险 / watch = 有抬升但不够判 / clear = 没证据 / nodata
#: 档位同时是前端文案的键后缀（``ble.verdict.<code>.head/why/next``），
#: 这样「结论」跟「判词」不可能互相矛盾——两者都出自同一次 ``risk_type()``。
VERDICT_CODE = {
    RISK_NONE: "clear",
    RISK_LIGHT: "light",
    RISK_COMPLEX: "complex",
    RISK_FLAT: "flat",
    RISK_BLE: "ble",
    RISK_WIFI: "wifi",
    RISK_MIXED: "mixed",
    RISK_NO_DATA: "nodata",
}
#: 哪些判词算「有风险」：给「不确定」的两档留出位置，别把分不清的说成安全
VERDICT_LEVEL = {
    "clear": "clear",
    "light": "watch",
    "complex": "risk",
    "flat": "watch",
    "ble": "risk",
    "wifi": "risk",
    "mixed": "risk",
    "nodata": "nodata",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def sliding_windows(lo_hz: int, hi_hz: int, step_hz: int = SCAN_STEP_HZ,
                    half_hz: int = USABLE_HALF_HZ) -> list[int]:
    """重叠滑扫的中心频率序列（每窗覆盖 ``±half_hz``，步进小于窗宽即重叠）。"""
    first = lo_hz + half_hz
    last = hi_hz - half_hz
    if last <= first:
        return [(lo_hz + hi_hz) // 2]
    centers: list[int] = []
    center = first
    while center <= last:
        centers.append(center)
        center += step_hz
    if centers[-1] < last:  # 步进除不尽时把上沿补齐
        centers.append(last)
    return centers


def scan_plan() -> list[int]:
    """扫描计划：3 个广播信道**对准**各开一窗（要有肩部才判得窄带）+ 数据信道区滑扫。

    广播信道的窗必须**中心对准**它：窄带判据要拿 ±4~8 MHz 的肩部做对比，
    通道偏到窗边上就没有肩部可比了。
    """
    centers = [ADV_CHANNELS[ch] for ch in ADV_ORDER]
    for center in sliding_windows(BLE_DATA_FIRST_HZ, BLE_DATA_LAST_HZ):
        if center not in centers:
            centers.append(center)
    return sorted(centers)


def data_slots() -> list[int]:
    """数据信道（0..36）的中心频率；广播信道那三个频点不算数据信道。"""
    adv = set(ADV_CHANNELS.values())
    out: list[int] = []
    center = BLE_DATA_FIRST_HZ
    while center <= BLE_DATA_LAST_HZ:
        if center not in adv:
            out.append(center)
        center += BLE_STEP_HZ
    return out


def level_of(score: float) -> str:
    for threshold, label in LEVELS:
        if score >= threshold:
            return label
    return "空闲"


def _span(snap: Snapshot, lo_hz: float, hi_hz: float) -> list[float]:
    """取 ``[lo, hi]`` 覆盖的 bin 值（越界自动裁到快照可见范围）。"""
    first, last = snap.band_indices(lo_hz, hi_hz)
    if last < first:
        return []
    return list(snap.bins_db[first : last + 1])


def _median(values: Iterable[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return float(ordered[len(ordered) // 2])


def _p80(values: Iterable[float]) -> float:
    """通道电平取 p80 而不是中位数。

    BLE 脉冲在一个 2 MHz 通道里不是铺满的：中位数会被"没脉冲的那一半 bin"拉回底噪，
    真实信号直接消失。p80 落在"有脉冲"的那一侧，同时仍然抗单 bin 毛刺（比取 max 稳）。
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(len(ordered) * 0.8))
    return float(ordered[index])


# ---------------------------------------------------------------------------
# 统计结构
# ---------------------------------------------------------------------------
@dataclass
class AdvStat:
    """一个广播信道的累积统计（跨多帧）。"""

    channel: int
    center_hz: int
    frames: int = 0
    hits: int = 0
    masked: int = 0
    shallow: int = 0
    level_sum: float = 0.0
    prominence_sum: float = 0.0
    peak_db: float = 0.0
    covered_mhz: set[int] = field(default_factory=set)

    def feed(self, level: float, prominence: float, peak: float, ref: float,
             floor_rise: float = 0.0, covered: Iterable[int] = ()) -> None:
        self.frames += 1
        self.level_sum += level
        self.peak_db = max(self.peak_db, peak)
        self.covered_mhz.update(covered)
        # 「抬升」要跟**本机噪声自己的抬升**比：纯噪声下 p80−p10 本来就有 6~8 dB，
        # 直接用 4 dB 当门槛会把完全安静判成「轻微干扰」（首版就栽在这）。
        rise = (level - ref) - floor_rise
        if PRESENCE_LIGHT_DB < rise <= PRESENCE_DB:
            self.shallow += 1
        active = rise > PRESENCE_DB
        # 窄带占比：能量是集中在通道里（窄带源）还是摊到肩部（宽带源）
        narrow = active and prominence >= PROMINENCE_DB and prominence >= NARROW_RATIO * rise
        if narrow:
            self.hits += 1
            self.prominence_sum += prominence
        elif active:
            # 有能量但抬不出窄带凸起：同频有更宽的东西压着（WiFi / 微波炉）
            self.masked += 1

    @property
    def activity(self) -> float:
        """活动占空 = 命中帧 / 观测帧（每帧一次采样，不是时间的连续积分）。"""
        return self.hits / self.frames if self.frames else 0.0

    @property
    def level_db(self) -> float:
        return self.level_sum / self.frames if self.frames else 0.0

    @property
    def prominence_db(self) -> float:
        """命中帧的窄带突出度均值（比肩部高多少 dB）。"""
        return self.prominence_sum / self.hits if self.hits else 0.0

    @property
    def coverage(self) -> float:
        """通道 ±1 MHz 被实测覆盖的 MHz 比例（多次重叠窗按并集算）。"""
        return min(1.0, len(self.covered_mhz) / (2 * ADV_HALF_HZ / 1_000_000))

    def excess_db(self, ref: float) -> float:
        """比全频段参考电平（各帧 p10 的均值）高多少 dB。"""
        return self.level_db - ref

    def level(self, ref: float) -> str:
        if self.coverage < MIN_COVERAGE:
            return "无覆盖"
        if not self.frames:
            return "无数据"
        return level_of(_activity_score(self))


def _activity_score(stat: AdvStat) -> float:
    """单通道 0..100 的活动强度（占空为主，电平与窄带程度为辅）。"""
    return 100.0 * (0.6 * stat.activity + 0.4 * min(1.0, stat.prominence_db / 20.0))


@dataclass
class BleRisk(ModePlugin):
    """BLE 广播信道风险评估。"""

    id: str = "ble_risk"
    name: str = "BLE 广播信道风险评估"
    # 单射频分时：与其它模块各占相等份额（悬殊配比会把某个模块饿死，见 META-0087）
    slice_weight: float = 1.0

    dwell_ms: int = DWELL_MS
    bins: int = BINS

    adv: dict[int, AdvStat] = field(default_factory=dict)
    started_at: float = 0.0
    last_ts: float = 0.0
    frames: int = 0
    dropped_frames: int = 0
    slices: int = 0
    # 参考电平（各帧 p10 的均值）
    _ref_sum: float = 0.0
    _ref_frames: int = 0
    # 每窗「时间均值频谱」（宽带占用率在这个上面算，见 WIDEBAND_EXCESS_DB 注释）
    _spec_sum: dict[int, list[float]] = field(default_factory=dict, repr=False)
    _spec_frames: dict[int, int] = field(default_factory=dict, repr=False)
    #: 各帧广播信道「原始抬升」（p80 − p10）样本，用来标定本机噪声自己的抬升水平
    _rises: list[float] = field(default_factory=list, repr=False)
    _rise_floor: float = 0.0
    _spec_lo: dict[int, list[float]] = field(default_factory=dict, repr=False)
    _spec_hi: dict[int, list[float]] = field(default_factory=dict, repr=False)
    # 宽带占用（单帧高于 p10+10dB 的 bin 占比）
    _wideband_sum: float = 0.0
    _wideband_frames: int = 0
    # 数据信道区密度（每帧多少个 2 MHz 栅格是「有信号」）
    _slot_frames: int = 0
    _slot_active_sum: float = 0.0
    _slot_peak: int = 0
    _slot_level_sum: float = 0.0
    _slot_covered: set[int] = field(default_factory=set, repr=False)
    #: 每个数据区栅格的「有信号帧数 / 总帧数」——常亮点（CW）和间歇流量靠这个分开
    _slot_hits: dict[int, int] = field(default_factory=dict, repr=False)
    _slot_seen: dict[int, int] = field(default_factory=dict, repr=False)
    # WiFi 信道占用（共存提示用）
    wifi_level_sum: dict[int, float] = field(default_factory=dict)
    wifi_frames: dict[int, int] = field(default_factory=dict)
    wifi_covered: dict[int, set[int]] = field(default_factory=dict, repr=False)
    wifi_fill: dict[int, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for channel, center_hz in ADV_CHANNELS.items():
            self.adv.setdefault(channel, AdvStat(channel=channel, center_hz=center_hz))

    # ---- 契约 -----------------------------------------------------------
    def scan_requests(self) -> list[ScanRequest]:
        return [ScanRequest(hz, SCAN_BW_HZ, self.dwell_ms, self.bins) for hz in scan_plan()]

    def on_snapshot(self, snap: Snapshot) -> None:
        if not self.started_at:
            self.started_at = snap.ts
        self.last_ts = max(self.last_ts, snap.ts)

        if max(snap.bins_db, default=0) <= DEGENERATE_MAX_CODE:
            self.dropped_frames += 1
            return
        self.frames += 1

        ordered = sorted(snap.bins_db)
        ref = ordered[int(len(ordered) * 0.10)]  # 本帧自比：抗 AGC 抖动与 0 码台阶
        self._ref_sum += ref
        self._ref_frames += 1

        visible_lo = max(snap.center_hz - USABLE_HALF_HZ, BAND_HZ[0])
        visible_hi = min(snap.center_hz + USABLE_HALF_HZ, BAND_HZ[1])

        values = snap.bins_db
        acc = self._spec_sum.get(snap.center_hz)
        if acc is None or len(acc) != len(values):
            acc = [0.0] * len(values)
            self._spec_sum[snap.center_hz] = acc
            self._spec_lo[snap.center_hz] = [float(value) for value in values]
            self._spec_hi[snap.center_hz] = [float(value) for value in values]
            self._spec_frames[snap.center_hz] = 0
        lo = self._spec_lo[snap.center_hz]
        hi = self._spec_hi[snap.center_hz]
        for index, value in enumerate(values):
            acc[index] += value
            if value < lo[index]:
                lo[index] = float(value)
            elif value > hi[index]:
                hi[index] = float(value)
        self._spec_frames[snap.center_hz] += 1

        self._feed_adv(snap, ref)
        self._feed_slots(snap, ref, visible_lo, visible_hi)
        self._feed_wifi(snap, ref)

    def produce(self) -> Output:
        rows = []
        ref = self.ref_db
        worst = max((stat.activity for stat in self.adv.values()), default=0.0) or 1.0
        for channel in ADV_ORDER:
            stat = self.adv[channel]
            rows.append(
                [
                    f"BLE {channel}",
                    fmt_hz(stat.center_hz),
                    fmt_pct(stat.coverage),
                    f"{stat.activity:.0%}",
                    bar(stat.activity, worst, 10),
                    f"{stat.excess_db(ref):+.0f} dB",
                    f"{stat.prominence_db:.0f} dB",
                    f"{stat.peak_db:.0f}",
                    stat.level(ref),
                ]
            )
        rows.append(
            [
                "数据区 0–36",
                "2404–2478 MHz",
                fmt_pct(self.slot_coverage),
                f"{self.slot_active:.1f}/帧",
                bar(self.slot_active, 12.0, 10),
                f"{self.slot_excess_db:+.0f} dB",
                "—",
                f"{self.slot_peak}",
                "窄带栅格占用",
            ]
        )
        return Output(
            kind="table",
            title=f"BLE 广播信道风险评估 — 风险：{self.risk_type()}（评分 {self.score():.1f}/100）",
            columns=["通道", "中心", "覆盖", "活动占空", "占空条", "比底噪", "窄带突出", "峰值码", "判定"],
            rows=rows,
            notes=self.notes(),
            payload=self.ble_report(),
        )

    # ---- 判读 -----------------------------------------------------------
    @property
    def ref_db(self) -> float:
        """全频段参考电平：各帧 p10 的均值（不依赖"最静信道"，窗口少时也稳）。"""
        return self._ref_sum / self._ref_frames if self._ref_frames else 0.0

    def mean_spectrum(self, center_hz: int) -> list[float]:
        """某个扫描窗的**时间均值频谱**（bins 升序频率）。没收到过帧则返回空表。"""
        acc = self._spec_sum.get(center_hz)
        frames = self._spec_frames.get(center_hz, 0)
        if not acc or frames <= 0:
            return []
        return [value / frames for value in acc]

    @property
    def wideband(self) -> float:
        """宽带占用率：ISM 内「高于全频段底噪 + :data:`WIDEBAND_EXCESS_DB`」的频点占比。

        在**时间均值频谱**上统计（不是单帧），否则实板噪声抖动会把 37% 的纯噪声 bin
        顶过门限。底噪取各窗均值频谱 p10 的最小值 —— 只要有一个窗落在干净处就有基准。
        """
        spectra = [self.mean_spectrum(center) for center in sorted(self._spec_sum)]
        spectra = [spec for spec in spectra if spec]
        if not spectra:
            return 0.0
        floors = []
        for spec in spectra:
            ordered = sorted(spec)
            floors.append(ordered[int(len(ordered) * 0.10)])
        floor = min(floors)
        threshold = floor + WIDEBAND_EXCESS_DB
        total = above = 0.0
        for center, spec in zip(sorted(self._spec_sum), spectra):
            step = SCAN_BW_HZ / len(spec)
            start = center - SCAN_BW_HZ / 2
            for index, value in enumerate(spec):
                freq = start + (index + 0.5) * step
                if not (BAND_HZ[0] <= freq <= BAND_HZ[1]):
                    continue
                total += 1
                if value > threshold:
                    above += 1
        return above / total if total else 0.0

    def contrast(self) -> float:
        """「高于底噪的频点」在时间上的开关幅度（功率码），减去安静频点的时间抖动。

        静止的形状 ⇒ 两组都只剩噪声抖动 ⇒ 差值 ≈ 0；
        随时间开关的宽带源（WiFi 包、微波炉）⇒ 抬升频点的 max−min 大得多 ⇒ 差值大。
        两侧都取 p80：整片 2.4G 一起上下动（增益/底噪在动，物理上不是信号）时，
        两组差值同样 ≈0 → 落到 ``RISK_FLAT``，宁可不判也不冒认。
        这是 :data:`RISK_FLAT` 与「WiFi 压制」唯一的分界线，靠它把静态形状挡在外面。
        """
        spectra = [self.mean_spectrum(center) for center in sorted(self._spec_sum)]
        spectra = [spec for spec in spectra if spec]
        if not spectra:
            return 0.0
        floor = min(sorted(spec)[int(len(spec) * 0.10)] for spec in spectra)
        threshold = floor + WIDEBAND_EXCESS_DB
        busy: list[float] = []
        quiet: list[float] = []
        for center in sorted(self._spec_sum):
            spec = self.mean_spectrum(center)
            lo = self._spec_lo.get(center) or []
            hi = self._spec_hi.get(center) or []
            if not spec or len(lo) != len(spec) or len(hi) != len(spec):
                continue
            step = SCAN_BW_HZ / len(spec)
            start = center - SCAN_BW_HZ / 2
            for index, value in enumerate(spec):
                freq = start + (index + 0.5) * step
                if not (BAND_HZ[0] <= freq <= BAND_HZ[1]):
                    continue
                span = hi[index] - lo[index]
                (busy if value > threshold else quiet).append(span)
        if not busy or not quiet:
            return 0.0
        # 两侧都取 p80 而不是均值：只要「抬升区里有一部分在开关」就算开关型宽带源，
        # 均值会被同一批频带里静止的那部分（形状/常开源）稀释掉。
        return round(_p80(busy) - _p80(quiet), 1)

    @property
    def rise_floor(self) -> float:
        """本机噪声自己的抬升水平（广播信道 p80−p10 的 p10）。

        这是判据的「零点」：``rise - rise_floor`` 才是信号真实的抬升。
        实板/不同增益下零点不一样，必须实测标定，不能写死。
        """
        return self._rise_floor

    def _update_rise_floor(self, every: int = 50) -> None:
        """每 ``every`` 个样本重算一次 p10（避免每帧排序）。"""
        total = len(self._rises)
        if total < 8 or (total % every and self._rise_floor):
            return
        ordered = sorted(self._rises[-2000:])
        self._rise_floor = ordered[int(len(ordered) * 0.10)]

    def persistent_slots(self) -> int:
        """「几乎一直亮」的数据区栅格数（≥80% 的帧都有信号）⇒ CW / 常开窄带源的指纹。

        间歇流量（BLE 连接、WiFi 包）每个栅格只在一部分帧里亮，达不到这一档。
        """
        count = 0
        for center, seen in self._slot_seen.items():
            if seen >= 10 and self._slot_hits.get(center, 0) / seen >= 0.8:
                count += 1
        return count

    @property
    def adv_frames(self) -> int:
        """三个广播信道的有效帧数合计（判浅抬升证据够不够用）。"""
        return sum(self.adv[ch].frames for ch in ADV_ORDER if ch in self.adv)

    def shallow_ratio(self) -> float:
        """三个广播信道上「浅抬升」帧的占比均值（有东西在动、但没到判定门限）。"""
        frames = [self.adv[ch].frames for ch in ADV_ORDER if ch in self.adv]
        total = sum(frames)
        if not total:
            return 0.0
        shallow = sum(self.adv[ch].shallow for ch in ADV_ORDER if ch in self.adv)
        return shallow / total

    @property
    def slot_active(self) -> float:
        """每帧「有信号」的 2 MHz 数据信道栅格数（均值）—— 设备密度的粗估。"""
        return self._slot_active_sum / self._slot_frames if self._slot_frames else 0.0

    @property
    def slot_peak(self) -> int:
        return self._slot_peak

    @property
    def slot_coverage(self) -> float:
        return min(1.0, len(self._slot_covered) / max(1, len(data_slots())))

    @property
    def slot_excess_db(self) -> float:
        return self._slot_level_sum / self._slot_frames - self.ref_db if self._slot_frames else 0.0

    @property
    def adv_activity(self) -> float:
        """三个广播信道的**加权**平均活动占空（按观测帧数加权）。"""
        total = sum(stat.frames for stat in self.adv.values())
        hits = sum(stat.hits for stat in self.adv.values())
        return hits / total if total else 0.0

    @property
    def masked_frames(self) -> int:
        return sum(stat.masked for stat in self.adv.values())

    def adv_score(self) -> float:
        """广播信道部分 0..100：占空为主，相对电平与窄带程度为辅。"""
        parts: list[float] = []
        ref = self.ref_db
        for channel in ADV_ORDER:
            stat = self.adv[channel]
            if not stat.frames:
                continue
            activity = stat.activity
            excess = min(1.0, max(0.0, stat.excess_db(ref) / 30.0))
            prominence = min(1.0, max(0.0, stat.prominence_db / 20.0))
            parts.append(0.6 * activity + 0.25 * excess + 0.15 * prominence)
        return 100.0 * (sum(parts) / len(parts)) if parts else 0.0

    def score(self) -> float:
        """综合评分 0..100（广播信道 80% + 数据信道密度 20%）。"""
        data_score = min(1.0, self.slot_active / 12.0)
        return round(0.8 * self.adv_score() + 0.2 * 100.0 * data_score, 1)

    def evidence(self) -> dict[str, float]:
        """判决用的全部证据（也是「为什么给这个判词」的原始数据，给前端/日志看）。"""
        return {
            "adv_activity": round(self.adv_activity, 3),
            "shallow": round(self.shallow_ratio(), 3),
            "wideband": round(self.wideband, 3),
            "contrast_db": self.contrast(),
            "rise_floor": round(self._rise_floor, 1),
            "slots": round(self.slot_active, 2),
            "slots_persistent": float(self.persistent_slots()),
            "adv_frames": float(self.adv_frames),
        }

    def risk_type(self) -> str:
        """风险类型判词：**先判能不能分清，再判是哪一类**。

        覆盖五档现实输入（仿真见 ``tests/test_ble_risk.py::DiscriminationTest``）：

        1. 完全安静          → ``RISK_NONE``
        2. 很浅的干扰/噪音   → ``RISK_LIGHT``（有抬升但没达标命中，不与安静混为一谈）
        3. 复杂噪音          → ``RISK_COMPLEX``（两类以上证据同时成立，不给单一归因）
        4. 单一来源          → ``RISK_BLE`` / ``RISK_WIFI`` / ``RISK_MIXED``
        5. 分不清的宽带      → ``RISK_FLAT``（宽带高但随时间不开关，不许冒认 WiFi）
        """
        if not self.frames:
            return RISK_NO_DATA
        ev = self.evidence()
        wide = ev["wideband"] >= WIDEBAND_FRAC
        light_wide = 0.25 <= ev["wideband"] < WIDEBAND_FRAC
        adv = ev["adv_activity"] >= 0.10
        light_adv = 0.02 <= ev["adv_activity"] < 0.10
        slots = ev["slots"] >= 6.0 or ev["slots_persistent"] >= 1.0
        shallow = ev["shallow"] >= LIGHT_RATIO and self.adv_frames >= MIN_ADV_FRAMES

        if wide:
            if ev["contrast_db"] < CONTRAST_DB:
                return RISK_FLAT
            # 宽带压制 + 自身还有明显广播活动 ⇒ 两类都在
            if adv and ev["adv_activity"] >= 0.15:
                return RISK_MIXED
            return RISK_WIFI
        if adv:
            return RISK_BLE
        # 没有达标命中：数「还剩下几类证据」——两类以上算复杂噪音，一类算浅干扰
        kinds = sum(1 for flag in (light_wide, light_adv, slots, shallow) if flag)
        if kinds >= 2:
            return RISK_COMPLEX
        if kinds == 1:
            return RISK_LIGHT
        return RISK_NONE

    def level(self) -> str:
        return level_of(self.score())

    def verdict(self) -> dict[str, Any]:
        """一句话结论：**先回答「到底有没有风险」，再说凭什么。**

        用户看到的第一个问题永远是「所以有没有风险？」——原来页面只给判词标签 + 十几列
        数字，得自己拼判据才能回答，实测被评价「完全没讲明白」。这里把三件事结构化下发给
        页面：

        * ``code``  结论档位（``clear/light/complex/flat/ble/wifi/mixed/nodata``），
          页面据此选模板（文案在 ``i18n.js``，中英各一套）；
        * ``level`` ``risk`` / ``watch`` / ``clear`` / ``nodata``，页面据此上色；
        * ``why``   模板里要填的数字**全部来自这里**：页面不自己算判据，也不写死门槛。

        ``code`` 与 ``risk_type()`` 由同一张映射表绑定 → 结论与判词不可能自相矛盾。
        """
        risk = self.risk_type()
        code = VERDICT_CODE.get(risk, "nodata")
        ev = self.evidence()
        prom = max((stat.prominence_db for stat in self.adv.values()), default=0.0)
        return {
            "code": code,
            "level": VERDICT_LEVEL.get(code, "nodata"),
            "risk_type": risk,
            "why": {
                **ev,
                "prominence_db": round(prom, 1),
                "ref_db": round(self.ref_db, 1),
                "presence_db": PRESENCE_DB,
                "prominence_thr": PROMINENCE_DB,
                "adv_thr": 0.10,
                "adv_heavy": 0.15,
                "wideband_frac": WIDEBAND_FRAC,
                "wideband_excess_db": WIDEBAND_EXCESS_DB,
                "contrast_db": round(self.contrast(), 1),
                "contrast_thr": CONTRAST_DB,
                "light_ratio": LIGHT_RATIO,
                "min_adv_frames": float(MIN_ADV_FRAMES),
                "headroom_codes": HEADROOM_CODES,
                "slots_total": len(data_slots()),
                "wifi_busy": self.wifi_busy(),
                "frames": self.frames,
                "span_s": round(self.span_s, 1),
            },
        }

    def wifi_busy(self) -> list[int]:
        """「在用」的 2.4G WiFi 信道：18 MHz 窗口里 ≥50% 的 bin 高于本帧 p10+8 dB。"""
        return [item["channel"] for item in self.wifi_channels() if item["busy"]]

    def wifi_channels(self) -> list[dict[str, Any]]:
        """逐个 2.4G WiFi 信道的占用情况（共存提示的原料，本身就是测量结果）。"""
        out: list[dict[str, Any]] = []
        ref = self.ref_db
        for channel in sorted(self.wifi_frames):
            frames = self.wifi_frames[channel]
            covered = len(self.wifi_covered.get(channel, ()))
            level = self.wifi_level_sum[channel] / frames if frames else 0.0
            fill = self.wifi_fill.get(channel, 0.0) / frames if frames else 0.0
            out.append(
                {
                    "channel": channel,
                    "center_hz": WIFI_CHANNELS_24[channel] * 1_000_000,
                    "level_db": round(level, 1),
                    "excess_db": round(level - ref, 1),
                    "fill": round(fill, 3),
                    "frames": frames,
                    "covered_mhz": covered,
                    "busy": frames >= 3 and covered >= 12 and fill >= WIFI_BUSY_FILL,
                }
            )
        return out

    def overlap(self) -> list[dict[str, Any]]:
        """WiFi 信道与 BLE 广播信道的重叠关系（共存提示的原料）。"""
        out: list[dict[str, Any]] = []
        busy = set(self.wifi_busy())
        for wifi_ch, wifi_mhz in WIFI_CHANNELS_24.items():
            lo = wifi_mhz * 1_000_000 - WIFI_SPAN_HALF_HZ
            hi = wifi_mhz * 1_000_000 + WIFI_SPAN_HALF_HZ
            for adv_ch in ADV_ORDER:
                center = ADV_CHANNELS[adv_ch]
                overlap_hz = min(hi, center + ADV_HALF_HZ) - max(lo, center - ADV_HALF_HZ)
                if overlap_hz <= 0:
                    continue
                out.append(
                    {
                        "wifi_channel": wifi_ch,
                        "ble_channel": adv_ch,
                        "overlap_mhz": round(overlap_hz / 1e6, 1),
                        "wifi_busy": wifi_ch in busy,
                        "full": overlap_hz >= 1.9 * ADV_HALF_HZ,
                        "fraction": round(overlap_hz / (2 * ADV_HALF_HZ), 2),
                    }
                )
        return out

    def headroom_warning(self) -> str:
        """前端余量告警：底噪参考码被增益抬太高时，宽带占用率不能当真值读。"""
        if self.wideband < 0.25 or self.ref_db < HEADROOM_CODES:
            # 宽带占用本身就很低时，「占用率会随增益变」这句话没有意义
            return ""
        return (
            f"前端底噪参考码 {self.ref_db:.0f}（≥{HEADROOM_CODES:.0f}）⇒ 增益偏高，"
            f"宽带占用率 {self.wideband:.0%} 会随增益变化（实板同环境实测：增益 20/40/60 → "
            f"宽带占用 27%/61%/85%，判词从「复杂噪音」一路滑到「WiFi 压制型」）。"
            "拿宽带占用下结论前，先降两档增益复测：占用率不随增益走才是环境，"
            "随增益走就是前端动态范围。"
        )

    def hints(self) -> list[str]:
        """共存/整改建议（按风险类型给，不承诺"一定是某个设备"）。"""
        risk = self.risk_type()
        lines: list[str] = []
        headroom = self.headroom_warning()
        if headroom:
            lines.append(headroom)
        if risk == RISK_WIFI:
            lines.append(
                "宽带占用率 "
                f"{self.wideband:.0%}（≥{WIDEBAND_FRAC:.0%}，按时间均值频谱算）"
                "→ BLE 广播信道整体被埋在宽带能量下，窄带凸起分辨不出来。"
                "宽带源不一定是 WiFi：微波炉、无线摄像头、5.8G 图传的镜像、邻近 LTE 也会铺满。"
                "建议：先逐个关掉可疑宽带设备复测（这台的判词只说明「宽带占主导」，不指名设备）；"
                "确认是路由器就把信道挪开或换到 5G；仅靠 BLE 侧调参没用（抬不起头的是 BLE 广播本身）。"
            )
        elif risk == RISK_BLE:
            lines.append(
                f"广播信道活动占空 {self.adv_activity:.0%}、宽带占用仅 {self.wideband:.0%} "
                "→ 是蓝牙设备的广播密度问题。建议：减少同时广播的 BLE 设备，或把广播间隔（adv interval）加长；"
                "注意数据信道区栅格占用 "
                f"{self.slot_active:.1f}/帧，说明还有连接态流量在动。"
            )
        elif risk == RISK_FLAT:
            lines.append(
                f"宽带占用 {self.wideband:.0%}（≥{WIDEBAND_FRAC:.0%}），但这些频点在时间上几乎不开关"
                f"（开关幅度 {self.contrast():.0f} 码，判据 ≥{CONTRAST_DB:.0f}）"
                "→ 分不清是「常开宽带源」还是「前端滤波器的固定起伏（形状）」，所以不下 WiFi 结论。"
                "想定性：换一档增益再测一次（形状随增益走、占用不随），"
                "或做一次对照空窗（把可疑设备全关掉）比两次的宽带占用率。"
            )
        elif risk == RISK_COMPLEX:
            lines.append(
                f"多类证据同时成立（浅抬升 {self.shallow_ratio():.0%}、宽带占用 {self.wideband:.0%}、"
                f"数据区 {self.slot_active:.1f} 栅格/帧）→ 这是**多源混合**的复杂噪音，"
                "不指名任何一类设备；要定位只能逐个关设备复测，或把分析带宽收窄逐段看。"
            )
        elif risk == RISK_LIGHT:
            lines.append(
                f"只有浅抬升（{self.shallow_ratio():.0%} 的帧高于底噪 {PRESENCE_LIGHT_DB:.0f} dB，"
                f"但没到判定的 {PRESENCE_DB:.0f} dB、也没有达标的窄带凸起）→ 有东西在动，"
                "但样本不足以判成 BLE 广播，也没到宽带压制。可以加长采集时间再看，"
                "或把增益往上挪一档（浅信号会被抬进门限）。"
            )
        elif risk == RISK_MIXED:
            lines.append(
                f"宽带占用 {self.wideband:.0%} 且广播信道活动 {self.adv_activity:.0%} → 两类风险同时存在："
                "先把 WiFi 挪开（宽带源），再评估蓝牙密度。"
            )
        else:
            lines.append("广播信道没有可分辨的窄带活动，宽带占用也低 → 本次样本里没有 BLE 密度风险。")

        busy = self.wifi_busy()
        if self.wideband >= 0.90:
            lines.append(
                f"宽带占用 {self.wideband:.0%}（≥90%）⇒ 「在用 WiFi 信道」列表与「共存冲突」已经没有分辨力"
                "（整段都被铺满，每个邻道都会亮），这种情况只看宽带占用率本身。"
            )
        conflicts = [item for item in self.overlap() if item["wifi_busy"] and item["full"]]
        if conflicts:
            pairs = "、".join(f"ch{item['wifi_channel']}↔BLE {item['ble_channel']}" for item in conflicts)
            lines.append(
                f"共存冲突（20 MHz WiFi 完全覆盖 BLE 广播信道）：{pairs}。"
                "BLE 广播会丢包重传、WiFi 也会被 BLE 脉冲打断，两边都掉性能。"
            )
        elif busy:
            lines.append(
                f"2.4G 在用的 WiFi 信道：{', '.join(f'ch{ch}' for ch in busy)}"
                "（按 20 MHz 栅格测到宽带占用；20 MHz 宽带源会连带点亮相邻信道，别当成好几个 AP）。"
            )
        else:
            lines.append("2.4G 没有测量到在用的 WiFi 信道（宽带占比都低于 50%，或样本不足）。")
        return lines

    def ble_report(self) -> dict[str, Any]:
        ref = self.ref_db
        return {
            "kind": "ble_risk",
            # 一句话结论（页面第一屏就显示它，别让人自己去拼判据）
            "verdict": self.verdict(),
            "risk_type": self.risk_type(),
            "score": self.score(),
            "level": self.level(),
            "adv_score": round(self.adv_score(), 1),
            "adv_activity": round(self.adv_activity, 3),
            "wideband": round(self.wideband, 3),
            "masked_frames": self.masked_frames,
            "ref_db": round(ref, 1),
            "wideband_method": {
                "basis": "时间均值频谱",
                "excess_db": WIDEBAND_EXCESS_DB,
                "windows": len(self._spec_sum),
            },
            # 判据门槛一起下发：前端只显示、不写死数字（改判据不用改网页）
            "thresholds": {
                "presence_db": PRESENCE_DB,
                "prominence_db": PROMINENCE_DB,
                "narrow_ratio": NARROW_RATIO,
                "wideband_excess_db": WIDEBAND_EXCESS_DB,
                "wideband_frac": WIDEBAND_FRAC,
                "contrast_db": CONTRAST_DB,
                "light_ratio": LIGHT_RATIO,
                "min_adv_frames": float(MIN_ADV_FRAMES),
                "headroom_codes": HEADROOM_CODES,
                "wifi_busy_fill": WIFI_BUSY_FILL,
            },
            "data_band": {
                "slots": len(data_slots()),
                "active_per_frame": round(self.slot_active, 2),
                "peak": self._slot_peak,
                "coverage": round(self.slot_coverage, 3),
                "excess_db": round(self.slot_excess_db, 1),
            },
            "adv": [
                {
                    "channel": channel,
                    "center_hz": stat.center_hz,
                    "activity": round(stat.activity, 3),
                    "level_db": round(stat.level_db, 1),
                    "excess_db": round(stat.excess_db(ref), 1),
                    "prominence_db": round(stat.prominence_db, 1),
                    "peak_db": round(stat.peak_db, 1),
                    "coverage": round(stat.coverage, 3),
                    "frames": stat.frames,
                    "hits": stat.hits,
                    "masked": stat.masked,
                    "level": stat.level(ref),
                }
                for channel in ADV_ORDER
                for stat in (self.adv[channel],)
            ],
            "contrast_db": self.contrast(),
            "shallow": round(self.shallow_ratio(), 3),
            "evidence": self.evidence(),
            "wifi_busy": self.wifi_busy(),
            "wifi_channels": self.wifi_channels(),
            "overlap": self.overlap(),
            "hints": self.hints(),
            # 判据原文一起下发：网页上「判据与边界」直接显示模块自己的话，不在 JS 里重编
            "notes": self.notes(),
            "headroom_warning": self.headroom_warning(),
            "frames": self.frames,
            "dropped_frames": self.dropped_frames,
            "span_s": round(self.span_s, 2),
            "scan_plan": {
                "bw_hz": SCAN_BW_HZ,
                "step_hz": SCAN_STEP_HZ,
                "dwell_ms": self.dwell_ms,
                "windows": len(scan_plan()),
            },
        }

    def web_payload(self) -> dict[str, Any]:
        """给上位机的结构化产出（页面自己画，这里只给数）。"""
        return self.ble_report()

    def notes(self) -> list[str]:
        lines = [
            "判据①：本通道 p80 电平 > 本帧 p10 + "
            f"{PRESENCE_DB:.0f} dB ⇒ 这次「有信号」；判据②：比肩部（±4~8MHz）高 ≥ "
            f"{PROMINENCE_DB:.0f} dB 且占这次抬升 ≥ {NARROW_RATIO:.0%} ⇒ 这次是「窄带凸起」（像 BLE）"
            "（宽带源把能量摊到肩部，通道只比肩部高一点，占比低）。两条都成立才算广播活动命中。",
            f"判据③：「抬升」先减掉本机噪声自己的抬升零点 rise_floor={self.rise_floor:.1f} 码"
            f"（纯噪声下 p80−p10 本来就有 6~8 dB，不减就会把「完全安静」判成「轻微干扰」）。"
            f"判据④：宽带占用高还要看**时间上开不开**——开关幅度 {self.contrast():.1f} 码 ≥ "
            f"{CONTRAST_DB:.0f} 才算信号，否则只是前端固有形状（判「{RISK_FLAT}」）。"
            f"判据⑤：没到门限但明确高于零点的帧算「浅抬升」，占 ≥ {LIGHT_RATIO:.0%} 就自成一档"
            f"（当前 {self.shallow_ratio():.0%}），不与「完全安静」混同。",
            f"风险：**{self.risk_type()}**（评分 {self.score():.1f}/100，宽带占用 {self.wideband:.0%}，"
            f"广播活动 {self.adv_activity:.0%}，数据区 {self.slot_active:.1f} 栅格/帧）。",
        ]
        lines.extend(self.hints())
        lines.append(
            f"扫描：3 个广播信道各对准一窗 + 数据区 {len(scan_plan()) - 3} 窗重叠滑扫"
            f"（{SCAN_BW_HZ/1e6:.0f}MHz 窗 / {SCAN_STEP_HZ/1e6:.0f}MHz 步进 / {self.dwell_ms}ms 驻留）；"
            f"样本 {self.frames} 帧、丢弃整窗 0 码 {self.dropped_frames} 帧。"
        )
        lines.append(
            "边界：**只评概率，不做设备级归因**（看出「这里在动、动得像不像 BLE」，"
            "看不出「几台什么设备」）；功率码是相对 dB 码不是 dBm；单射频分时扫描，帧间有接收空档。"
        )
        return lines

    @property
    def span_s(self) -> float:
        return max(0.0, self.last_ts - self.started_at) if self.started_at else 0.0

    # ---- 内部 -----------------------------------------------------------
    def _feed_adv(self, snap: Snapshot, ref: float) -> None:
        reach = SHOULDER_HZ[1] + ADV_HALF_HZ
        for channel, center in ADV_CHANNELS.items():
            if abs(center - snap.center_hz) > reach:
                continue  # 窗离这个广播信道太远，取不到肩部 → 不参与判决
            core = _span(snap, center - ADV_HALF_HZ, center + ADV_HALF_HZ)
            if not core:
                continue
            # 肩部左右分别取中位数再取**较大者**做「本地台面」：
            # 两边不对称时（WiFi 信道边缘正好扫过一个广播信道）合并取中位数会跳变，
            # 台面被低估 → 把 WiFi 斜坡误判成窄带凸起（2026-11 实测踩到）。
            left = _p80(_span(snap, center - SHOULDER_HZ[1], center - SHOULDER_HZ[0]))
            right = _p80(_span(snap, center + SHOULDER_HZ[0], center + SHOULDER_HZ[1]))
            level = _p80(core)
            pedestal = max(left, right)
            prominence = level - pedestal if pedestal else 0.0
            covered = {
                int(snap.bin_freq_hz(index) // 1_000_000)
                for index in range(*_index_range(snap, center - ADV_HALF_HZ, center + ADV_HALF_HZ))
                if snap.bins_db[index] > 0
            }
            self._rises.append(level - ref)
            self.adv[channel].feed(level, prominence, max(core), ref, self._rise_floor, covered)

    def _feed_slots(self, snap: Snapshot, ref: float, visible_lo: float, visible_hi: float) -> None:
        lo_edge = visible_lo + SLOT_HALF_HZ
        hi_edge = visible_hi - SLOT_HALF_HZ
        slots = [center for center in data_slots() if lo_edge <= center <= hi_edge]
        if not slots:
            return
        self._update_rise_floor()
        active = 0
        level_sum = 0.0
        for center in slots:
            core = _span(snap, center - SLOT_HALF_HZ, center + SLOT_HALF_HZ)
            if not core:
                continue
            level = _p80(core)
            level_sum += level
            self._slot_covered.add(center)
            # 数据区 37 个栅格也进「本机噪声抬升」样本池：广播信道全被占时
            # 靠数据区里那些安静栅格把噪声零点拉回来（否则零点会被占用的信道顶飞）。
            self._rises.append(level - ref)
            self._slot_seen[center] = self._slot_seen.get(center, 0) + 1
            if level > ref + self._rise_floor + PRESENCE_DB:
                active += 1
                self._slot_hits[center] = self._slot_hits.get(center, 0) + 1
        self._slot_frames += 1
        self._slot_active_sum += active
        self._slot_peak = max(self._slot_peak, active)
        self._slot_level_sum += level_sum / max(1, len(slots))

    def _feed_wifi(self, snap: Snapshot, ref: float) -> None:
        for channel, center_mhz in WIFI_CHANNELS_24.items():
            center = center_mhz * 1_000_000
            core = _span(snap, center - WIFI_MEASURE_HALF_HZ, center + WIFI_MEASURE_HALF_HZ)
            if not core:
                continue
            threshold = ref + WIFI_BUSY_DB
            fill = sum(1 for value in core if value > threshold) / len(core)
            self.wifi_fill[channel] = self.wifi_fill.get(channel, 0.0) + fill
            self.wifi_level_sum[channel] = self.wifi_level_sum.get(channel, 0.0) + _p80(core)
            self.wifi_frames[channel] = self.wifi_frames.get(channel, 0) + 1
            self.wifi_covered.setdefault(channel, set()).update(
                int(snap.bin_freq_hz(index) // 1_000_000)
                for index in range(*_index_range(snap, center - WIFI_MEASURE_HALF_HZ,
                                                 center + WIFI_MEASURE_HALF_HZ))
                if snap.bins_db[index] > 0
            )

    def reset(self) -> None:
        self.adv = {channel: AdvStat(channel=channel, center_hz=center)
                    for channel, center in ADV_CHANNELS.items()}
        self.started_at = 0.0
        self.last_ts = 0.0
        self.frames = 0
        self.dropped_frames = 0
        self.slices = 0
        self._ref_sum = 0.0
        self._ref_frames = 0
        self._spec_sum.clear()
        self._spec_frames.clear()
        self._spec_lo.clear()
        self._spec_hi.clear()
        self._rises.clear()
        self._rise_floor = 0.0
        self._slot_frames = 0
        self._slot_active_sum = 0.0
        self._slot_peak = 0
        self._slot_level_sum = 0.0
        self._slot_covered.clear()
        self._slot_hits.clear()
        self._slot_seen.clear()
        self.wifi_level_sum.clear()
        self.wifi_frames.clear()
        self.wifi_fill.clear()
        self.wifi_covered.clear()

    def summary(self) -> dict[str, Any]:
        data = super().summary()
        data.update(
            {
                "frames": self.frames,
                "risk_type": self.risk_type(),
                "score": self.score(),
                "adv_activity": round(self.adv_activity, 3),
                "wideband": round(self.wideband, 3),
            }
        )
        return data


def _index_range(snap: Snapshot, lo_hz: float, hi_hz: float) -> tuple[int, int]:
    """``range(*_index_range(...))`` 用的半开区间（越界已由 band_indices 裁剪）。"""
    first, last = snap.band_indices(lo_hz, hi_hz)
    return first, last + 1


# ---------------------------------------------------------------------------
# 自带自测入口：python -m modes._legacy.ble_risk --scenario ble_crowded
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="BLE 广播信道风险评估（mock 自测，无需硬件）")
    parser.add_argument("--scenario", default="ble_crowded")
    parser.add_argument("--slices", type=int, default=24)
    args = parser.parse_args(argv)

    from core.registry import Registry
    from core.scheduler import Scheduler
    from tests.mock_source import MockSource

    plugin = BleRisk()
    registry = Registry()
    registry.register(plugin)
    scheduler = Scheduler(MockSource(scenario=args.scenario, realtime=False), registry)
    scheduler.run(max_slices=args.slices)
    print(ascii_table(
        ["项目", "值"],
        [
            ["场景", args.scenario],
            ["时间片", scheduler.stats.slices],
            ["帧数", plugin.frames],
            ["风险类型", plugin.risk_type()],
            ["评分", f"{plugin.score():.1f}/100"],
            ["宽带占用", f"{plugin.wideband:.0%}"],
            ["广播活动", f"{plugin.adv_activity:.0%}"],
        ],
    ))
    print(plugin.produce().render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())