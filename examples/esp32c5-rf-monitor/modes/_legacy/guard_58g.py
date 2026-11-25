"""任务 4：5.8G 频段守卫（持续发射源检测 + 告警日志）。

干什么（只做三件事，不碰 ``core/``）：

1. **轮扫** 5700–5895 MHz（20 MHz 窗 / 15 MHz 步进，与瀑布图同一套重叠滑扫规矩）；
2. **检测持续性发射源**：图传 / 模拟摄像头 / 无线影音那类「窄带一直亮」或「规律跳频」的源；
3. **出告警日志**：每次告警带出现时间、频点、带宽、持续时长、占空比、峰值抬升。

判据（全部是**相对**口径，不换算 dBm）：

* 单帧参考电平取该帧 bins 的 p25，某 bin 高出参考 ``OCC_EXCESS_DB``（6 码）算「这一帧亮着」；
* 候选段 = 时间均值频谱上「抬升 ≥ ``ALERT_DB``（10 码）」且「占空比 ≥ ``HOP_FILL``（0.20）」
  的**连续 bin 段**，宽度限制在 ``MAX_ALERT_HZ``（16 MHz）以内；
* 占空比 ≥ ``STEADY_FILL``（0.75）→「持续窄带/持续宽带」；0.20~0.75 →「规律跳频」；
* 同窗内两个候选段占空比在 0.20~0.75 且**互斥出现**（相关系数 ≤ ``ALT_CORR`` = -0.4）
  → 判为「规律跳频（交替）」，这是模拟图传跳频最像的形态；
* 一个候选段要连续 ``MIN_FRAMES``（8）帧都在，才写进告警日志（否则就是一次噪声尖峰）。

边界（写进产出物，不许美化）：

* **周期快照，非连续实时**：一轮 13 窗 ≈ 1.3 s（真板约 4 窗/秒 → 3 s），「持续」是按
  我们采样到的帧算的，不是真的连续盯了那么久；
* 功率码是**相对 dB 码**，不是 dBm；「抬升 10 码」不等于「比噪声高 10 dB 的绝对功率」；
* **跨窗跳频不判**：只会判「同一个 20 MHz 窗内两个频点交替」。跳频源一步跳 >15 MHz
  就会跨窗，本模块不追（追它需要固件侧连续 IQ，那是任务 5 的抓档口径）；
* 覆盖不到 5895–5925 MHz（C5 调谐上限 5895，见 ``docs/rx-controls.md``）；
* 增益会改占用率读数（``docs/03-ble-risk.md`` §6.1 实测 27%/61%/85%），
  **跨会话比较前先固定档位**。
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

from core.registry import ModePlugin, Output, ScanRequest
from core.snapshot import Snapshot

#: 守卫频段：5.8G 视频/图传常用段（5700–5850）+ C5 的调谐余量到 5895
#: （任务书写 5700–5900，但固件/芯片上限是 5895，见 docs/rx-controls.md —— 越界就是假数据）
BAND_LO_HZ = 5_700_000_000
BAND_HI_HZ = 5_895_000_000
#: 单窗带宽与步进（与 modes/wifi_check.py、modes/waterfall.py 同一套：20 MHz 窗 / 15 MHz 步进）
SCAN_BW_HZ = 20_000_000
SCAN_STEP_HZ = 15_000_000
#: 单窗驻留（固件下限 100 ms）
DWELL_MS = 100
#: 每帧 bin 数（固件上限 512）
BINS = 512
#: 窗内有效半宽：80 MS/s 下有效通带 30–35 MHz > 20 MHz，整窗可用
USABLE_HALF_HZ = SCAN_BW_HZ // 2
#: 覆盖率下限：低于它就记「没覆盖」，不参与判决
MIN_COVERAGE = 0.60
#: 整窗全 0 码的帧（增益没稳 / 没真收到）直接丢弃
DEGENERATE_MAX_CODE = 0
#: 每个窗保留多少帧做统计（40 帧 ≈ 真板 10 s / 假固件 1 s 量级）
RING_FRAMES = 40
#: 候选段至少出现这么多帧才写日志
MIN_FRAMES = 8
#: 时间均值频谱上「算抬升」的门槛（相对 dB 码）
ALERT_DB = 10.0
#: 单帧内「这一格亮着」的门槛（相对该帧 p25 参考）
OCC_EXCESS_DB = 6.0
#: 占空比 ≥ 这个 → 持续发射；低于 → 跳频/间歇
STEADY_FILL = 0.75
#: 占空比下限：低于它就是偶发（不是「持续性发射源」，不告警）
HOP_FILL = 0.20
#: 比这还窄的段当单 bin 抖动，不当发射源（20 MHz/512 bin ≈ 39 kHz/格，100 kHz ≈ 2.5 格）
MIN_NARROW_HZ = 100_000
#: 比这还窄的段判「单频点」而不是「窄带」：实板实测环境里 0.1 MHz 级的持续凸起是
#: 本振/时钟杂散（5708 MHz、5776 MHz 各一根 3 格尖峰，占空比 92~97%），
#: 不是 5.8G 图传（最窄也 ≥1 MHz）。照实报，但类别必须分开，别让人误认成摄像头。
SPUR_MAX_HZ = 500_000
#: 窗中心 ±这么多以内的格一律不判 —— 实板实测：每个窗的正中心都有一根 3 格宽、
#: 高出底噪 33~43 码的「持续窄带」（5740/5768/5450 MHz 都一样），是接收机直流/LO 泄漏，
#: 不是环境里的发射源。真实 5.8G 图传最窄也有 1 MHz，挖掉 ±0.25 MHz 不影响抓源。
CENTER_NOTCH_HZ = 250_000
#: 窗两侧各留这么多不判：20 MHz 窗靠边的滚降本身就会高出均值 10~27 码
EDGE_GUARD_HZ = 500_000
#: 比这还宽的段不归本模块（那是 WiFi/宽带干扰，交给 wifi_check）
MAX_ALERT_HZ = 16_000_000
#: 两条告警中心相差在这么多以内视为「同一个源」：续时长、不新开日志
#: （重叠窗会把同一个发射源在相邻 2~3 个窗里各看到一次，不聚类就是一堆重复告警）
MATCH_HZ = 6_000_000
#: 超过这么久没再命中 → 标 inactive（告警保留，不删）
STALE_S = 60.0
#: 告警日志上限（环形，保最新）
MAX_ALERTS = 300
#: 两次自动检测的最小间隔（``on_snapshot`` 里 1 Hz 跑一次足够）
ANALYZE_MIN_INTERVAL_S = 0.9
#: 交替判据：两段占空比序列相关系数 ≤ 这个值 → 互斥出现（跳频交替）
ALT_CORR = -0.4

#: 发射源类型（六种，「单频点」= 疑似本振/时钟杂散，与「窄带」区分开）
KIND_SPUR_STEADY = "持续单频点"
KIND_SPUR_BURST = "间歇单频点"
KIND_STEADY_NARROW = "持续窄带"
KIND_STEADY_WIDE = "持续宽带"
KIND_HOP = "规律跳频"
KIND_SPUR_KINDS = (KIND_SPUR_STEADY, KIND_SPUR_BURST)


def window_centers(
    lo_hz: int = BAND_LO_HZ,
    hi_hz: int = BAND_HI_HZ,
    bw_hz: int = SCAN_BW_HZ,
    step_hz: int = SCAN_STEP_HZ,
) -> list[int]:
    """覆盖 ``[lo_hz, hi_hz]`` 的中心频点：首尾都压边，中间均分（不留 >step 的空档）。"""
    half = bw_hz // 2
    lo_center = lo_hz + half
    hi_center = hi_hz - half
    if hi_center <= lo_center:
        return [lo_center]
    span = hi_center - lo_center
    steps = max(1, math.ceil(span / step_hz))
    return [int(round(lo_center + span * i / steps)) for i in range(steps + 1)]


def percentile(values: Sequence[float], pct: float) -> float:
    """线性插值分位数（与 ``host/gain.py`` 同一口径，空序列返回 0）。"""
    data = sorted(float(v) for v in values if v == v)  # 顺手滤掉 NaN
    if not data:
        return 0.0
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * max(0.0, min(100.0, pct)) / 100.0
    low = math.floor(pos)
    high = min(len(data) - 1, low + 1)
    frac = pos - low
    return data[low] * (1.0 - frac) + data[high] * frac


def runs_of(flags: Sequence[bool]) -> list[tuple[int, int]]:
    """连续 True 段的 ``[first, last]`` 下标（闭区间）；相邻段之间至少隔一个 False。"""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(flags) - 1))
    return runs


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """皮尔逊相关系数；任一侧方差为 0 时返回 0（无法判断，不硬判互斥）。"""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    mean_a = sum(a[:n]) / n
    mean_b = sum(b[:n]) / n
    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((a[i] - mean_a) ** 2 for i in range(n))
    var_b = sum((b[i] - mean_b) ** 2 for i in range(n))
    if var_a <= 0 or var_b <= 0:
        return 0.0
    return num / math.sqrt(var_a * var_b)


def masked_bins(bins: int, bw_hz: float = SCAN_BW_HZ) -> list[bool]:
    """哪些格**可以**参与判决（True = 可判）。

    挖掉两段：窗中心 ±``CENTER_NOTCH_HZ``（直流/LO 泄漏）、窗两侧各 ``EDGE_GUARD_HZ``（滚降）。
    """
    half = bw_hz / 2.0
    bin_hz = bw_hz / bins
    keep: list[bool] = []
    for index in range(bins):
        offset = -half + (index + 0.5) * bin_hz
        keep.append(abs(offset) > CENTER_NOTCH_HZ and abs(offset) < half - EDGE_GUARD_HZ)
    return keep


def cluster_by_freq(cands: Sequence[dict[str, Any]], match_hz: float = MATCH_HZ) -> list[list[dict[str, Any]]]:
    """按中心频点把候选贪心聚类（链式：相邻间距 ≤ ``match_hz`` 就并进同一簇）。

    为什么必须做：窗是 20 MHz / 步进 14.58 MHz 的重叠滑扫，一个 5.8G 发射源会被
    相邻 2~3 个窗各看到一次，不聚类的话日志里同一条源会重复出现 3 遍、频点还都不一样。
    链式聚类的代价是「相距 6 MHz 的两个独立源会被并成一条」——这里接受这个代价，
    理由是 5.8G 守卫关心的是「有没有持续发射源」，不是「精确数出几个源」。
    """
    if not cands:
        return []
    ordered = sorted(cands, key=lambda c: c["center_hz"])
    groups: list[list[dict[str, Any]]] = [[ordered[0]]]
    for cand in ordered[1:]:
        if cand["center_hz"] - groups[-1][-1]["center_hz"] <= match_hz:
            groups[-1].append(cand)
        else:
            groups.append([cand])
    return groups


def kind_of(width_hz: float, fill: float) -> str:
    """按「带宽 + 占空比」判类型：先看是不是单频点杂散，再按持续性分窄/宽/跳频。"""
    if width_hz <= SPUR_MAX_HZ:
        return KIND_SPUR_STEADY if fill >= STEADY_FILL else KIND_SPUR_BURST
    if fill >= STEADY_FILL:
        return KIND_STEADY_NARROW if width_hz <= 8_000_000 else KIND_STEADY_WIDE
    return KIND_HOP


@dataclass
class Alert:
    """一条告警（既是日志行，也是 ``produce()`` 的表格行来源）。"""

    alert_id: str
    kind: str
    center_hz: int
    bw_hz: float
    peak_excess_db: float
    fill: float
    frames: int
    first_seen: float
    last_seen: float
    hits: int = 1
    windows: int = 1  # 这一轮有多少个重叠窗同时看到了它（聚类支撑数）
    window_hz: int = 0
    active: bool = True

    @property
    def duration_s(self) -> float:
        return max(0.0, self.last_seen - self.first_seen)

    def row(self) -> list[Any]:
        return [
            self.alert_id,
            "在发" if self.active else "已停",
            self.kind,
            f"{self.center_hz / 1e6:.1f}",
            f"{self.bw_hz / 1e6:.1f}",
            f"{self.peak_excess_db:.1f}",
            f"{self.fill:.0%}",
            f"{self.duration_s:.1f}",
            time.strftime("%H:%M:%S", time.localtime(self.first_seen)),
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.alert_id,
            "kind": self.kind,
            "center_hz": self.center_hz,
            "center_mhz": round(self.center_hz / 1e6, 3),
            "bw_mhz": round(self.bw_hz / 1e6, 3),
            "peak_excess_db": round(self.peak_excess_db, 1),
            "fill": round(self.fill, 3),
            "frames": self.frames,
            "first_seen": round(self.first_seen, 3),
            "last_seen": round(self.last_seen, 3),
            "duration_s": round(self.duration_s, 1),
            "hits": self.hits,
            "windows": self.windows,
            "window_mhz": round(self.window_hz / 1e6, 1),
            "active": self.active,
        }


@dataclass
class WindowStat:
    """一个扫描窗的累计统计（环形缓冲 + 派生量）。

    缓冲里存 ``(ts, bins)`` 而不是只存 ``bins``：判据只认**最近 ``STALE_S`` 秒**的帧，
    否则源停发以后旧帧还躺在缓冲里，会把告警一直「续命」到永远。
    """

    center_hz: int
    frames: deque = field(default_factory=lambda: deque(maxlen=RING_FRAMES))
    total_frames: int = 0
    dropped_frames: int = 0
    last_ts: float = 0.0

    def add(self, bins: Sequence[int], ts: float) -> bool:
        """收一帧；整窗全 0 码的帧丢弃并返回 False。"""
        self.total_frames += 1
        self.last_ts = max(self.last_ts, ts)
        if not bins or max(bins) <= DEGENERATE_MAX_CODE:
            self.dropped_frames += 1
            return False
        if self.frames and ts < self.frames[-1][0]:
            # 时间戳倒退（换了时钟源 / 重连）→ 清缓冲，别拿两段不同时间的数据混算
            self.frames.clear()
        self.frames.append((float(ts), tuple(int(v) for v in bins)))
        return True

    def recent(self, fresh_s: float = STALE_S) -> list[tuple[float, tuple[int, ...]]]:
        """最近 ``fresh_s`` 秒内的帧。"""
        if not self.frames:
            return []
        edge = self.last_ts - fresh_s
        return [item for item in self.frames if item[0] >= edge]

    def series(self, fresh_s: float = STALE_S) -> list[list[float]]:
        return [[float(v) for v in bins] for _ts, bins in self.recent(fresh_s)]

    def mean_spectrum(self, fresh_s: float = STALE_S) -> list[float]:
        rows = self.series(fresh_s)
        if not rows:
            return []
        count = len(rows)
        return [sum(row[i] for row in rows) / count for i in range(len(rows[0]))]

    def coverage(self) -> float:
        """有效帧占比：覆盖不足说明这一段根本没被真正扫到。"""
        return len(self.frames) / self.total_frames if self.total_frames else 0.0


@dataclass
class Guard58G(ModePlugin):
    """5.8G 频段守卫。"""

    id: str = "guard_58g"
    name: str = "5.8G 频段守卫"
    # 单射频分时：与瀑布图/WiFi/BLE 同权（META-0087：权重悬殊会把低权重模块饿死）
    slice_weight: float = 1.0

    dwell_ms: int = DWELL_MS
    bins: int = BINS
    store: Any = None  # 由 app 层注入（有 add_event 就能落库），模块自己不 import store

    windows: dict[int, WindowStat] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    started_at: float = 0.0
    last_ts: float = 0.0
    frames: int = 0
    dropped_frames: int = 0
    analyses: int = 0
    _seq: int = 0
    _last_analyze: float = 0.0
    _pending_events: list[dict[str, Any]] = field(default_factory=list, repr=False)

    # ---- 契约 -----------------------------------------------------------
    def scan_requests(self) -> list[ScanRequest]:
        return [
            ScanRequest(hz, SCAN_BW_HZ, self.dwell_ms, self.bins)
            for hz in window_centers()
        ]

    def on_snapshot(self, snap: Snapshot) -> None:
        if not self.started_at:
            self.started_at = snap.ts
        self.last_ts = max(self.last_ts, snap.ts)
        self.frames += 1
        stat = self.windows.get(snap.center_hz)
        if stat is None:
            stat = WindowStat(center_hz=snap.center_hz)
            self.windows[snap.center_hz] = stat
        if not stat.add(snap.bins_db, snap.ts):
            self.dropped_frames += 1
        # 1 Hz 量级的自动检测：只在新告警产生时落库，避免每秒刷一遍 events 表
        if snap.ts - self._last_analyze >= ANALYZE_MIN_INTERVAL_S:
            self._last_analyze = snap.ts
            self.analyze(now=snap.ts)

    def produce(self) -> Output:
        self.analyze()
        rows = [alert.row() for alert in self.alerts[-MAX_ALERTS:]]
        return Output(
            kind="alert",
            title=f"5.8G 频段守卫（{BAND_LO_HZ/1e6:.0f}–{BAND_HI_HZ/1e6:.0f} MHz，告警 {len(rows)} 条）",
            columns=["编号", "状态", "类型", "频点 MHz", "带宽 MHz", "峰值抬升", "占空比", "持续 s", "首次出现"],
            rows=rows,
            notes=self.notes(),
            payload=self.web_payload(),
            created_at=self.last_ts or time.time(),
        )

    def reset(self) -> None:
        self.windows.clear()
        self.alerts.clear()
        self.started_at = 0.0
        self.last_ts = 0.0
        self.frames = 0
        self.dropped_frames = 0
        self.analyses = 0
        self._seq = 0
        self._last_analyze = 0.0
        self._pending_events.clear()

    # ---- 检测 -----------------------------------------------------------
    def analyze(self, now: float | None = None) -> list[Alert]:
        """对每个窗做一次检测，更新/新建告警，返回**本次命中**的告警。"""
        stamp = time.time() if now is None else now
        self.analyses += 1
        cands: list[dict[str, Any]] = []
        for center_hz in sorted(self.windows):
            stat = self.windows[center_hz]
            bins = len(stat.frames[0][1]) if stat.frames else 0
            for cand in self.candidates(stat):
                # 贴着窗沿的段是「被窗切了一半」的，带宽和中心都不可信；
                # 只在没有完整段可比时才拿它凑数（见下面的代表选取）
                cand["clipped"] = cand["first_bin"] == 0 or cand["last_bin"] == bins - 1
                cand["window_hz"] = center_hz
                cands.append(cand)
        hits: list[Alert] = []
        for group in cluster_by_freq(cands):
            rep = max(group, key=lambda c: (0 if c["clipped"] else 1, c["peak_excess_db"]))
            hits.append(self._upsert(rep, group, stamp))
        self._expire(stamp)
        self._flush_events()
        return hits

    def candidates(self, stat: WindowStat) -> list[dict[str, Any]]:
        """一个窗里的发射源候选（纯计算、无副作用，方便单测）。"""
        frames = stat.series()
        if len(frames) < MIN_FRAMES or stat.coverage() < MIN_COVERAGE:
            return []
        count = len(frames)
        bins = len(frames[0])
        if bins < 8:
            return []
        # 逐帧参考电平取 p25：抗「半窗空、半窗有信号」的台阶（p10 会被空窗拉低）
        refs = [percentile(frame, 25.0) for frame in frames]
        mean_spec = [sum(frame[i] for frame in frames) / count for i in range(bins)]
        floor = percentile(mean_spec, 25.0)
        excess = [value - floor for value in mean_spec]
        series: list[list[float]] = []
        fills: list[float] = []
        for i in range(bins):
            row = [1.0 if frames[k][i] >= refs[k] + OCC_EXCESS_DB else 0.0 for k in range(count)]
            series.append(row)
            fills.append(sum(row) / count)
        keep = masked_bins(bins)
        flags = [
            keep[i] and excess[i] >= ALERT_DB and fills[i] >= HOP_FILL
            for i in range(bins)
        ]
        bin_hz = SCAN_BW_HZ / bins
        found: list[dict[str, Any]] = []
        for first, last in runs_of(flags):
            width_hz = (last - first + 1) * bin_hz
            if not all(keep[i] for i in range(first, last + 1)):
                continue  # 段里夹着被挖掉的格 → 不连续，不当一个源
            if width_hz < MIN_NARROW_HZ or width_hz > MAX_ALERT_HZ:
                continue
            span = range(first, last + 1)
            peak_bin = max(span, key=lambda i: excess[i])
            found.append(
                {
                    "center_hz": int(
                        round(stat.center_hz - SCAN_BW_HZ / 2 + (first + last + 1) * bin_hz / 2)
                    ),
                    "bw_hz": width_hz,
                    "first_bin": first,
                    "last_bin": last,
                    "fill": sum(fills[i] for i in span) / len(span),
                    "peak_excess_db": excess[peak_bin],
                    "peak_bin": peak_bin,
                    "frames": count,
                    "frame_ts": stat.last_ts,
                    "series": series[peak_bin],
                }
            )
        # 同窗内两段互斥出现（相关系数 ≤ ALT_CORR）→ 判「规律跳频（交替）」：
        # 这是模拟图传跳频最像的形态，也是「两段各自都像间歇」唯一能解释成单一发射源的证据
        for i in range(len(found)):
            for j in range(i + 1, len(found)):
                a, b = found[i], found[j]
                if not (HOP_FILL <= a["fill"] < STEADY_FILL and HOP_FILL <= b["fill"] < STEADY_FILL):
                    continue
                if correlation(a["series"], b["series"]) <= ALT_CORR:
                    a["alternate"] = b["center_hz"]
                    b["alternate"] = a["center_hz"]
        out: list[dict[str, Any]] = []
        for cand in found:
            cand["kind"] = KIND_HOP if "alternate" in cand else kind_of(cand["bw_hz"], cand["fill"])
            cand["window_hz"] = stat.center_hz
            cand.setdefault("frame_ts", stat.last_ts)
            cand.pop("series", None)
            out.append(cand)
        return out

    # ---- 告警日志 -------------------------------------------------------
    def _upsert(self, cand: dict[str, Any], group: Sequence[dict[str, Any]], stamp: float) -> Alert:
        frame_ts = float(cand.get("frame_ts") or stamp)
        for alert in reversed(self.alerts):
            if abs(alert.center_hz - cand["center_hz"]) <= MATCH_HZ:
                if frame_ts > alert.last_seen:  # 有新帧才算「又看到一次」
                    alert.last_seen = frame_ts
                    alert.hits += 1
                alert.windows = len(group)
                alert.frames = max(alert.frames, int(cand["frames"]))
                alert.kind = cand["kind"]
                alert.bw_hz = cand["bw_hz"]
                alert.center_hz = cand["center_hz"]
                alert.fill = cand["fill"]
                alert.peak_excess_db = max(alert.peak_excess_db, cand["peak_excess_db"])
                alert.active = True
                alert.window_hz = cand["window_hz"]
                return alert
        self._seq += 1
        alert = Alert(
            alert_id=f"G58-{self._seq:03d}",
            kind=cand["kind"],
            center_hz=cand["center_hz"],
            bw_hz=cand["bw_hz"],
            peak_excess_db=cand["peak_excess_db"],
            fill=cand["fill"],
            frames=int(cand["frames"]),
            first_seen=frame_ts,
            last_seen=frame_ts,
            windows=len(group),
            window_hz=cand["window_hz"],
        )
        self.alerts.append(alert)
        del self.alerts[:-MAX_ALERTS]
        self._pending_events.append(alert.as_dict())
        return alert

    def _expire(self, stamp: float) -> None:
        """按**最新帧时间**判存活：环形缓冲里留着旧帧不等于「源还在发」。"""
        edge = self.last_ts or stamp
        for alert in self.alerts:
            if alert.active and edge - alert.last_seen > STALE_S:
                alert.active = False

    def _flush_events(self) -> None:
        """把新告警交给注入的 store（``app`` 层设 ``plugin.store = store``）。"""
        if not self._pending_events:
            return
        pending, self._pending_events = self._pending_events, []
        store = self.store
        if store is None or not hasattr(store, "add_event"):
            return
        for event in pending:
            try:
                store.add_event(self.id, "alert", event)
            except Exception:  # noqa: BLE001 - 落库失败不能把采集带崩
                pass

    # ---- 汇总 -----------------------------------------------------------
    def active_alerts(self) -> list[Alert]:
        return [alert for alert in self.alerts if alert.active]

    def span_s(self) -> float:
        return max(0.0, self.last_ts - self.started_at) if self.started_at else 0.0

    def thresholds(self) -> dict[str, Any]:
        """判据全量下发（前端不许再写死数字）。"""
        return {
            "alert_db": ALERT_DB,
            "occ_excess_db": OCC_EXCESS_DB,
            "steady_fill": STEADY_FILL,
            "hop_fill": HOP_FILL,
            "min_frames": MIN_FRAMES,
            "min_narrow_hz": MIN_NARROW_HZ,
            "max_alert_hz": MAX_ALERT_HZ,
            "min_coverage": MIN_COVERAGE,
            "alt_corr": ALT_CORR,
            "match_hz": MATCH_HZ,
            "stale_s": STALE_S,
        }

    def web_payload(self) -> dict[str, Any]:
        rows = [alert.as_dict() for alert in self.alerts[-MAX_ALERTS:]]
        return {
            "kind": "guard_58g",
            "band": {"lo_hz": BAND_LO_HZ, "hi_hz": BAND_HI_HZ},
            "scan": {"bw_hz": SCAN_BW_HZ, "step_hz": SCAN_STEP_HZ, "windows": len(window_centers())},
            "alerts": rows,
            "active": sum(1 for alert in self.alerts if alert.active),
            "spurs": sum(1 for alert in self.alerts if alert.kind in KIND_SPUR_KINDS),
            "analyses": self.analyses,
            "frames": self.frames,
            "dropped_frames": self.dropped_frames,
            "span_s": round(self.span_s(), 1),
            "windows": [
                {
                    "center_hz": stat.center_hz,
                    "frames": len(stat.frames),
                    "total_frames": stat.total_frames,
                    "dropped_frames": stat.dropped_frames,
                    "coverage": round(stat.coverage(), 3),
                    "floor_code": round(percentile(stat.mean_spectrum(), 25.0), 1) if stat.frames else 0.0,
                }
                for stat in (self.windows[hz] for hz in sorted(self.windows))
            ],
            "thresholds": self.thresholds(),
            "notes": self.notes(),
        }

    def notes(self) -> list[str]:
        active = len(self.active_alerts())
        notes = [
            f"周期快照，非连续实时：一轮 {len(window_centers())} 窗，"
            f"本次统计跨度 {self.span_s():.1f} s / {self.frames} 帧。",
            "功率码是相对 dB 码（0..255），不是 dBm；「抬升 10 码」不等于绝对功率 10 dB。",
            f"比 {SPUR_MAX_HZ/1e3:.0f} kHz 还窄的段记「单频点」（实板环境里就是本振/时钟杂散，"
            f"不是摄像头；照实报但类别分开，别误读成图传）。",
            f"不判两段：窗中心 ±{CENTER_NOTCH_HZ/1e3:.0f} kHz（接收机直流/LO 泄漏，实板每个窗正中都有一根 3 格尖峰）"
            f"与窗两侧各 {EDGE_GUARD_HZ/1e6:.1f} MHz（滚降）。",
            f"判据：抬升 ≥{ALERT_DB:.0f} 码、占空比 ≥{HOP_FILL:.0%}、连续 ≥{MIN_FRAMES} 帧、"
            f"段宽 {MIN_NARROW_HZ/1e3:.0f} kHz–{MAX_ALERT_HZ/1e6:.0f} MHz；"
            f"占空比 ≥{STEADY_FILL:.0%} 记「持续」，否则记「规律跳频」。",
            f"跨窗跳频不追：只判同一 20 MHz 窗内两个频点交替（相关系数 ≤{ALT_CORR:.1f}）。",
            f"重叠窗会把同一个源在多窗里各看到一次 → 按频点 {MATCH_HZ/1e6:.0f} MHz 内聚类合并"
            f"（代价：相距 6 MHz 内的两个独立源会并成一条）。",
            f"频段上限 {BAND_HI_HZ/1e6:.0f} MHz 就是芯片调谐上限，"
            f"5895–5925 MHz 这一段本机测不到（不是没扫，是硬件到不了）。",
            f"当前在发 {active} 条 / 累计 {len(self.alerts)} 条；"
            f"丢弃全 0 码帧 {self.dropped_frames} 帧（不参与判决）。",
            "增益会改占用率读数：跨会话比较前先固定档位（见 docs/03-ble-risk.md §6.1）。",
        ]
        return notes


__all__ = [
    "BAND_LO_HZ",
    "BAND_HI_HZ",
    "SCAN_BW_HZ",
    "SCAN_STEP_HZ",
    "DWELL_MS",
    "BINS",
    "USABLE_HALF_HZ",
    "MIN_COVERAGE",
    "DEGENERATE_MAX_CODE",
    "RING_FRAMES",
    "MIN_FRAMES",
    "ALERT_DB",
    "OCC_EXCESS_DB",
    "STEADY_FILL",
    "HOP_FILL",
    "MIN_NARROW_HZ",
    "CENTER_NOTCH_HZ",
    "EDGE_GUARD_HZ",
    "MAX_ALERT_HZ",
    "MATCH_HZ",
    "STALE_S",
    "MAX_ALERTS",
    "ANALYZE_MIN_INTERVAL_S",
    "ALT_CORR",
    "SPUR_MAX_HZ",
    "KIND_SPUR_STEADY",
    "KIND_SPUR_BURST",
    "KIND_SPUR_KINDS",
    "KIND_STEADY_NARROW",
    "KIND_STEADY_WIDE",
    "KIND_HOP",
    "Alert",
    "WindowStat",
    "Guard58G",
    "window_centers",
    "percentile",
    "runs_of",
    "correlation",
    "kind_of",
    "masked_bins",
]