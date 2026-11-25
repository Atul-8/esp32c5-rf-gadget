"""任务 5：可疑信号抓拍（``feat/5-capture``）。

单文件插件 + 一个由 app 层注入的抓取函数（模块自己**不碰串口**，也不 import
``core.serial_link`` —— 和 ``guard_58g`` 的 store 注入同一套做法，单测里塞假函数即可）。

流程：

    SPEC 快照 ──► on_snapshot() 逐帧判候选（窄带持续 / 跳频样 / 分不清的宽带）
                 └─► classify() 判成 unknown 的才排队
                        └─► 队列 ──► app 层的 CaptureAwareSource 在**两次扫描之间**取走
                                    └─► CAP20 抓 I/Q ──► 存盘 + 落事件 + 出一行日志

边界（产出物里也写，不许美化）：

* 抓的是**一次突发**（固件 ``CAP20`` 上限 = ``IQ_WORDS`` 个样本，C5 是 16380），
  不是连续录制；抓的时候射频是停扫的（单射频分时）。
* 功率码/IQ 都是**相对量**，没做过绝对校准，不是 dBm。
* 分类是启发式的（只见带宽/持续/占空比），看不出「哪一个牌子的设备」。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from core.classifier import AMBIGUOUS_MIN_BW_HZ, Classification, SignalFeatures, classify
from core.registry import ModePlugin, Output, ScanRequest
from core.snapshot import Snapshot
from output.format import fmt_duration, fmt_hz_short

#: 2.4G ISM：抓拍的主战场（mock 的 unknown_cw / microwave 都在这里）
BAND_LO_HZ = 2_400_000_000
BAND_HI_HZ = 2_483_500_000
SCAN_BW_HZ = 20_000_000
SCAN_STEP_HZ = 15_000_000
DWELL_MS = 100
BINS = 512

#: 判定门槛（与 guard_58g 同源口径：单帧 p25 当参考，均值谱上判持续性）
FLOOR_PCT = 10.0  #: 底噪参考分位：比 p25 低，才能看见「占窗 90% 的宽带」
ALERT_DB = 20.0  #: 底噪自身抖动实测 ±10~18 码（AGENTS.md），10 码压不住噪声
OCC_EXCESS_DB = 10.0
HOP_FILL = 0.20
STEADY_FILL = 0.75
MIN_FRAMES = 8
MIN_WIDTH_HZ = 100_000
#: 窄段（<1 MHz）要求更高的抬升：真板噪声纹波就能造出 300 kHz 的「+20 码凸起」，
#: 而真正值得抓的窄带源（本振/杂散/摄像头时钟）实测都是 +30 码以上
NARROW_ALERT_DB = 30.0
NARROW_MAX_HZ = 1_000_000
#: ≤这个宽度且持续在发 = 本机杂散（本振/时钟），不是摄像头：报出来但不抓
#: （真板 60 s 实测 4 根：2455/2465/2427/2468 MHz，宽 0.16–0.27 MHz，抬升 +30~35 码，持续 37–52 s）
SPUR_MAX_HZ = 500_000
MAX_WIDTH_HZ = 19_500_000  # 窗内可判宽度上限（挖掉两侧 0.5 MHz 滚降后最多 ~19 MHz）
SMOOTH_BINS = 5  #: 均值谱滑动中位平滑格数（单格噪声能骗出「10 码抬升」）
CENTER_NOTCH_HZ = 250_000
EDGE_GUARD_HZ = 500_000

#: 环形缓冲
RING_FRAMES = 40
#: 帧子采样：真板一次扫描能吐 60+ 帧（0.2 s 内），不抽帧的话「40 帧」只覆盖 0.1 s，
#: 判出来的「持续 40 帧」跟「持续 0.1 秒」是一回事 —— 持续性根本没被验证。
FRAME_MIN_GAP_S = 0.2
#: 判定前环形缓冲至少覆盖这么久（秒）：不够就不判，宁可这一轮不出结论
MIN_SPAN_S = 3.0
#: 窗内「抬起的格」占比 ≥ 这个值 = 环境饱和（满负荷 WiFi），这时候按段切出来的
#: 每一块都是凹陷造成的假源，不做逐段判定（记 skipped_busy 并在 notes 里说明）
BUSY_FILL = 0.35
#: 同一个频点（±MATCH_HZ）抓过一次后，这么久内不重复抓
RETRIGGER_S = 120.0
MATCH_HZ = 3_000_000
#: 一个会话最多抓多少次（防止「一直抓」把时间片全吃掉）
MAX_CAPTURES = 20
#: 抓档参数：CAP20 = 10 位 I/Q，20 MS/s → 与 20 MHz 窗同宽
CAPTURE_BITS = 20
CAPTURE_RATE_CODE = 2
CAPTURE_SAMPLES = 16_384
#: 固件 CAP 上限（``CAP <samples>`` 要求 samples ≤ IQ_WORDS；C5 实测 16380）
FIRMWARE_MAX_SAMPLES = 16_380
#: 分析节流：一次扫描 ~0.1 s，1 s 分析一次够用
ANALYZE_MIN_INTERVAL_S = 0.9
#: 抓档元数据必须带「前后各 30 s 频谱上下文」（任务书要求）
CONTEXT_S = 30.0
#: 上下文里存的均值谱格数（512 格太重，够看形状即可）
CONTEXT_BINS = 64

KIND_CW = "窄带持续"
KIND_STEADY = "持续凸起"
KIND_AMBIGUOUS = "分不清的宽带"
KIND_BURST = "间歇出现"
KIND_SPUR = "持续单频点"


def window_centers(lo_hz: float = BAND_LO_HZ, hi_hz: float = BAND_HI_HZ) -> list[float]:
    """2.4G ISM 的窗表：首尾压边、均分步进（不许超过 ``SCAN_STEP_HZ``）。"""
    span = hi_hz - lo_hz
    half = SCAN_BW_HZ / 2.0
    count = max(1, int(-(-(span - SCAN_BW_HZ) // SCAN_STEP_HZ)) + 1) if span > SCAN_BW_HZ else 1
    if count == 1:
        return [(lo_hz + hi_hz) / 2.0]
    step = (span - SCAN_BW_HZ) / (count - 1)
    return [lo_hz + half + step * index for index in range(count)]


def percentile(values: Sequence[float], pct: float) -> float:
    """线性插值分位数（标准库实现，无 numpy 依赖）。"""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * max(0.0, min(100.0, float(pct))) / 100.0
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    frac = position - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def runs_of(flags: Sequence[bool]) -> list[tuple[int, int]]:
    """连续 True 的 ``[first, last]`` 区间。"""
    spans: list[tuple[int, int]] = []
    start = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            spans.append((start, index - 1))
            start = None
    if start is not None:
        spans.append((start, len(flags) - 1))
    return spans


def notch_bins(bins: int, bw_hz: float = SCAN_BW_HZ) -> list[bool]:
    """窗中心 ±``CENTER_NOTCH_HZ`` 的那几格（接收机直流/LO 泄漏，已知会抬升）。"""
    half = bw_hz / 2.0
    bin_hz = bw_hz / bins
    return [abs(-half + (index + 0.5) * bin_hz) <= CENTER_NOTCH_HZ for index in range(bins)]


def masked_bins(bins: int, bw_hz: float = SCAN_BW_HZ) -> list[bool]:
    """可判格：窗中心 ±``CENTER_NOTCH_HZ``（直流/LO 泄漏）与两侧滚降之外。"""
    half = bw_hz / 2.0
    bin_hz = bw_hz / bins
    return [
        abs(-half + (index + 0.5) * bin_hz) > CENTER_NOTCH_HZ
        and abs(-half + (index + 0.5) * bin_hz) < half - EDGE_GUARD_HZ
        for index in range(bins)
    ]


def smooth(values: Sequence[float], width: int = SMOOTH_BINS) -> list[float]:
    """滑动中位平滑（边界用真实存在的邻居），压掉单格噪声尖刺。"""
    half = max(0, int(width) // 2)
    if half == 0:
        return [float(v) for v in values]
    out: list[float] = []
    for index in range(len(values)):
        lo = max(0, index - half)
        hi = min(len(values), index + half + 1)
        out.append(percentile(values[lo:hi], 50.0))
    return out


def runs_of_state(state: Sequence[bool | None]) -> list[tuple[int, int]]:
    """``True/None`` 连成一段、``False`` 断开；``None``（挖掉的格）**不断段**。

    为什么不断段：真板一个 19 MHz 的平顶一定横跨窗中心，若把挖掉的 3 格当断点，
    就会把一个宽带信号劈成左右两个 ~9 MHz 的「WiFi」，判词直接错。挖掉的格
    「不知道是不是信号」，所以让它做粘合剂、不计入判决、只计入宽度。
    """
    spans: list[tuple[int, int]] = []
    start = None
    hot = False
    for index, value in enumerate(state):
        if value is False:
            if start is not None and hot:
                spans.append((start, index - 1))
            start, hot = None, False
            continue
        if start is None:
            start = index
        if value is True:
            hot = True
    if start is not None and hot:
        spans.append((start, len(state) - 1))
    return spans


def kind_of(width_hz: float, fill: float) -> str:
    """按带宽 + 占空比给候选一个粗分类（只是提示，真正的分类交 :func:`classify`）。"""
    if width_hz >= 15_000_000 and fill >= 0.70:
        return KIND_AMBIGUOUS
    if width_hz <= 500_000:
        return KIND_CW if fill >= 0.50 else KIND_BURST
    if fill >= STEADY_FILL:
        return KIND_STEADY
    return KIND_BURST


@dataclass
class Candidate:
    """一个待抓拍的候选源（判据 + 特征，都是给 :func:`classify` 的输入）。"""

    center_hz: float
    width_hz: float
    fill: float
    frames: int
    peak_excess_db: float
    window_hz: float
    kind: str
    classification: Classification
    first_seen: float = 0.0
    last_seen: float = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.last_seen - self.first_seen)

    def features(self) -> SignalFeatures:
        return SignalFeatures(
            bw_hz=self.width_hz,
            duration_s=self.duration_s,
            duty_cycle=self.fill,
            peak_over_noise_db=self.peak_excess_db,
            centre_hz=self.center_hz,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "center_hz": round(self.center_hz),
            "center_mhz": round(self.center_hz / 1e6, 3),
            "width_hz": round(self.width_hz),
            "width_mhz": round(self.width_hz / 1e6, 3),
            "fill": round(self.fill, 3),
            "frames": self.frames,
            "peak_excess_db": round(self.peak_excess_db, 1),
            "kind": self.kind,
            "label": self.classification.label,
            "confidence": round(self.classification.confidence, 3),
            "reason": self.classification.reason,
            "duration_s": round(self.duration_s, 2),
        }


@dataclass
class CaptureRequest:
    """排给 app 层去执行的一次抓档（模块本身不发串口命令）。"""

    freq_hz: int
    samples: int
    rate_code: int
    bits: int
    candidate: Candidate


@dataclass
class CaptureRecord:
    """一次已经落盘的抓档。"""

    index: int
    ts: float
    freq_hz: int
    samples: int
    bits: int
    rate_code: int
    sample_rate_hz: int
    duration_us: int
    path: str
    sidecar: str
    sha256: str
    label: str
    confidence: float
    reason: str
    peak: int
    rms: float
    note: str = ""

    def row(self) -> list[Any]:
        return [
            f"CAP-{self.index:03d}",
            fmt_hz_short(self.freq_hz),
            f"{self.samples}",
            f"{self.sample_rate_hz/1e6:.0f}MS/s",
            f"{self.bits}b",
            self.label,
            f"{self.confidence:.2f}",
            f"{self.rms:.0f}",
            time.strftime("%H:%M:%S", time.localtime(self.ts)),
            os.path.basename(self.path),
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": f"CAP-{self.index:03d}",
            "ts": round(self.ts, 3),
            "freq_hz": self.freq_hz,
            "freq_mhz": round(self.freq_hz / 1e6, 3),
            "samples": self.samples,
            "bits": self.bits,
            "rate_code": self.rate_code,
            "sample_rate_hz": self.sample_rate_hz,
            "duration_us": self.duration_us,
            "path": self.path,
            "sidecar": self.sidecar,
            "sha256": self.sha256,
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "peak": self.peak,
            "rms": round(self.rms, 1),
            "note": self.note,
        }


@dataclass
class WindowStat:
    """一个窗的帧缓冲（判持续性用）。"""

    center_hz: float
    frames: deque = field(default_factory=lambda: deque(maxlen=RING_FRAMES))
    total: int = 0
    dropped: int = 0
    last_ts: float = 0.0

    def add(self, snap: Snapshot) -> bool:
        values = list(snap.bins_db)
        if not values or max(values) <= 0:
            self.dropped += 1
            return False
        stamp = float(snap.ts or time.time())
        if self.last_ts and snap.ts and snap.ts < self.last_ts - 1.0:
            self.frames.clear()  # 时间戳倒退（重放/时钟跳变）：旧帧不能拿来续命
        elif self.last_ts and stamp - self.last_ts < FRAME_MIN_GAP_S:
            # 抽帧：同一次扫描里的连发帧留着只会让「40 帧」覆盖 0.1 s
            return False
        self.frames.append((stamp, values))
        self.last_ts = stamp
        self.total += 1
        return True

    def recent(self, fresh_s: float, now: float) -> list[tuple[float, list[int]]]:
        return [(ts, bins) for ts, bins in self.frames if now - ts <= fresh_s]

    def coverage(self) -> float:
        return 1.0 if self.total + self.dropped == 0 else 1.0 - self.dropped / (self.total + self.dropped)

    def span_s(self) -> float:
        """环形缓冲实际覆盖的墙钟时长（不是「帧数 × 间隔」）。"""
        if len(self.frames) < 2:
            return 0.0
        return max(0.0, self.frames[-1][0] - self.frames[0][0])


@dataclass
class CaptureMode(ModePlugin):
    """``modes/capture.py`` 的插件本体。"""

    id: str = "capture"
    name: str = "可疑信号抓拍"
    slice_weight: float = 1.0

    #: 由 ``app.bind_capture()`` 注入：``fn(freq_hz, samples, rate_code, bits) -> 抓档对象``
    capture_fn: Callable[..., Any] | None = None
    #: 落盘目录（None = 只统计不落盘，单测用）
    out_dir: str | None = None
    #: 由 ``app.bind_store()`` 注入
    store: Any | None = None

    windows: dict[float, WindowStat] = field(default_factory=dict)
    pending: list[CaptureRequest] = field(default_factory=list)
    records: list[CaptureRecord] = field(default_factory=list)
    seen: dict[float, float] = field(default_factory=dict)  # 频点 -> 上次抓的时间
    spurs: list[Candidate] = field(default_factory=list)  # 能确认的窄带持续源 → 见 notes
    started_at: float = 0.0
    last_ts: float = 0.0
    last_analyze: float = 0.0
    analyses: int = 0
    frames: int = 0
    dropped: int = 0
    triggers: int = 0  # 累计判成 unknown 的次数（每轮分析各算一次，含被预算/重复挡掉的）
    skipped_budget: int = 0
    skipped_repeat: int = 0
    skipped_edge: int = 0  # 被窗边界切开、宽度不可信而丢弃的段数
    skipped_weak: int = 0  # 窄段但抬升不够（多半是噪声纹波）
    skipped_short: int = 0  # 缓冲覆盖时长不足 MIN_SPAN_S，这轮不判
    skipped_busy: int = 0  # 窗内环境饱和（占比 ≥BUSY_FILL），不做逐段判定
    skipped_spur: int = 0  # 窄带持续源但归为本机杂散（不抓）
    errors: list[str] = field(default_factory=list)

    # ---- 契约 ------------------------------------------------------------
    def scan_requests(self) -> list[ScanRequest]:
        return [
            ScanRequest(int(center), SCAN_BW_HZ, DWELL_MS, BINS)
            for center in window_centers()
        ]

    def on_snapshot(self, snap: Snapshot) -> None:
        center = round(float(snap.center_hz or 0.0))
        if not center:
            return
        stat = self.windows.get(center)
        if stat is None:
            stat = WindowStat(center_hz=center)
            self.windows[center] = stat
        ts = float(snap.ts or time.time())
        self.started_at = self.started_at or ts
        self.last_ts = max(self.last_ts, ts)
        if stat.add(snap):
            self.frames += 1
        else:
            self.dropped += 1
        if ts - self.last_analyze >= ANALYZE_MIN_INTERVAL_S:
            self.analyze()

    def produce(self) -> Output:
        rows = [record.row() for record in self.records]
        notes = self.notes()
        payload = self.web_payload()
        if not rows:
            return Output(
                kind="report",
                title=f"可疑信号抓拍（{fmt_duration(self.span_s())} 内未抓到可疑信号）",
                notes=notes,
                payload=payload,
            )
        return Output(
            kind="archive",
            title=f"可疑信号抓拍（{len(rows)} 次）",
            columns=["编号", "频点", "样本", "采样率", "位宽", "分类", "置信", "RMS", "时间", "文件"],
            rows=rows,
            files=tuple(record.path for record in self.records),
            notes=notes,
            payload=payload,
        )

    def reset(self) -> None:
        self.windows.clear()
        self.pending.clear()
        self.records.clear()
        self.seen.clear()
        self.started_at = 0.0
        self.last_ts = 0.0
        self.last_analyze = 0.0
        self.analyses = 0
        self.frames = 0
        self.dropped = 0
        self.triggers = 0
        self.skipped_budget = 0
        self.skipped_repeat = 0
        self.skipped_edge = 0
        self.skipped_weak = 0
        self.skipped_short = 0
        self.skipped_busy = 0
        self.skipped_spur = 0
        self.spurs.clear()
        self.errors.clear()

    # ---- 判定 ------------------------------------------------------------
    def span_s(self) -> float:
        if not self.started_at or not self.last_ts:
            return 0.0
        return max(0.0, self.last_ts - self.started_at)

    def analyze(self) -> list[Candidate]:
        """把所有窗过一遍，返回这一轮判出的候选（同时把该抓的排进队列）。"""
        self.analyses += 1
        self.last_analyze = self.last_ts or time.time()
        found: list[Candidate] = []
        for center in sorted(self.windows):
            stat = self.windows[center]
            for cand in self.candidates(stat):
                found.append(cand)
                self._maybe_queue(cand)
        return found

    def candidates(self, stat: WindowStat) -> list[Candidate]:
        """单窗候选：均值谱抬升 + 单帧占空比 + 连续段宽度。"""
        frames = [bins for _ts, bins in stat.recent(RETRIGGER_S, self.last_ts or time.time())]
        if len(frames) < MIN_FRAMES:
            return []
        if stat.span_s() < MIN_SPAN_S:
            # 缓冲还没覆盖够时间，这时候说「持续」没有依据
            self.skipped_short += 1
            return []
        bins = len(frames[0])
        bin_hz = SCAN_BW_HZ / bins
        refs = [percentile(frame, FLOOR_PCT) for frame in frames]
        peak_holds = [max(frame) for frame in frames]
        raw_mean = [
            sum(frame[i] for frame in frames) / len(frames)
            for i in range(bins)
        ]
        mean = smooth(raw_mean, SMOOTH_BINS)
        keep = masked_bins(bins)
        notch = notch_bins(bins)
        # 单帧占空比：这一格有多少帧比本帧 p25 高 ≥ 6 码
        fills = [
            sum(1 for frame, ref in zip(frames, refs) if keep[i] and frame[i] - ref >= OCC_EXCESS_DB)
            / len(frames)
            for i in range(bins)
        ]
        state: list[bool | None] = []
        for i in range(bins):
            if notch[i]:
                state.append(None)  # 挖掉但不断段
            elif not keep[i]:
                state.append(False)  # 窗沿滚降：直接断段
            else:
                raised = mean[i] - percentile(mean, FLOOR_PCT) >= ALERT_DB
                state.append(bool(raised and fills[i] >= HOP_FILL))
        spans = runs_of_state(state)
        if not spans:
            return []
        judged_bins = [i for i in range(bins) if keep[i] and not notch[i]]
        raised = sum(1 for i in judged_bins if state[i])
        # 「环境饱和」的判据是「大片抬起 + 还被凹陷切成好几段」，不是「面积大」：
        # 一个 17 MHz 的连续平顶是**一个**源（微波炉/图传），而满负荷 WiFi 那种
        # 抬起面积大又有多个凹陷的窗，切出来的每段都是凹陷造的假源。
        if len(spans) >= 2 and judged_bins and raised / len(judged_bins) >= BUSY_FILL:
            self.skipped_busy += 1
            return []
        first_kept = next(i for i in range(bins) if keep[i])
        last_kept = bins - 1 - next(i for i in range(bins) if keep[bins - 1 - i])
        rise = [raw_mean[i] - percentile(mean, FLOOR_PCT) for i in range(bins)]
        out: list[Candidate] = []
        for span_first, span_last in spans:
            # 平滑会把段往两侧各扩 SMOOTH_BINS//2 格 → 用未平滑的抬升把边界收紧，
            # 否则 3 格的窄带源会被算成「扣完只剩 0 格」而漏判，宽度也会虚报。
            hot_bins = [
                i for i in range(span_first, span_last + 1) if keep[i] and rise[i] >= ALERT_DB
            ]
            if not hot_bins:
                continue
            first, last = hot_bins[0], hot_bins[-1]
            width_hz = (last - first + 1) * bin_hz
            if width_hz < MIN_WIDTH_HZ or width_hz > MAX_WIDTH_HZ:
                continue
            # 贴着可判边界 = 被窗边界切开，真实宽度不知道 → 不判。
            # 不这么挡，强 WiFi 信道在邻窗会露出 5 MHz 的尾巴，被判成「分不清」反复抓拍。
            # 例外：≥AMBIGUOUS_MIN_BW_HZ 的宽带本来就会顶满整窗（微波炉一类），照判。
            if (first == first_kept or last == last_kept) and width_hz < AMBIGUOUS_MIN_BW_HZ:
                self.skipped_edge += 1
                continue
            # 逐帧都算一遍「这一帧这段在不在」，才算连续帧数
            hot = sum(
                1
                for frame, ref in zip(frames, refs)
                if percentile(
                    [frame[i] for i in range(first, last + 1) if keep[i]], 50.0
                )
                - ref
                >= OCC_EXCESS_DB
            )
            if hot < MIN_FRAMES:
                continue
            centre = stat.center_hz - SCAN_BW_HZ / 2 + (first + last + 1) / 2.0 * bin_hz
            judged = [i for i in range(first, last + 1) if keep[i]] or [first]
            peak = max(rise[i] for i in judged)
            if width_hz < NARROW_MAX_HZ and peak < NARROW_ALERT_DB:
                self.skipped_weak += 1
                continue
            fill = sum(fills[i] for i in judged) / len(judged)
            stamps = [ts for ts, _ in stat.recent(RETRIGGER_S, self.last_ts or time.time())]
            cand = Candidate(
                center_hz=centre,
                width_hz=width_hz,
                fill=fill,
                frames=hot,
                peak_excess_db=peak,
                window_hz=stat.center_hz,
                kind=kind_of(width_hz, fill),
                classification=Classification("unknown", 0.0, ""),
                first_seen=min(stamps),
                last_seen=max(stamps),
            )
            cand.classification = classify(cand.features())
            out.append(cand)
        return out

    def _maybe_queue(self, cand: Candidate) -> None:
        if cand.classification.label != "unknown":
            return
        if cand.width_hz <= SPUR_MAX_HZ and cand.fill >= STEADY_FILL:
            # 本机杂散：真板环境里每块板子都有几根固定窄带（本振/时钟），
            # 抓它只会把「可疑抓拍」刷满。报进 spurs 清单，不当可疑信号处理。
            self.spurs.append(cand)
            self.skipped_spur += 1
            return
        self.triggers += 1
        if len(self.records) + len(self.pending) >= MAX_CAPTURES:
            self.skipped_budget += 1
            return
        now = self.last_ts or time.time()
        for freq, last in self.seen.items():
            if abs(freq - cand.center_hz) <= MATCH_HZ and now - last < RETRIGGER_S:
                self.skipped_repeat += 1
                return
        freq_hz = int(round(cand.center_hz / 1e6)) * 1_000_000  # 固件 FREQ 只收整 MHz
        request = CaptureRequest(
            freq_hz=freq_hz,
            samples=min(CAPTURE_SAMPLES, FIRMWARE_MAX_SAMPLES),
            rate_code=CAPTURE_RATE_CODE,
            bits=CAPTURE_BITS,
            candidate=cand,
        )
        self.pending.append(request)
        self.seen[freq_hz] = now

    # ---- 抓档执行（由 app 层在两次扫描之间调用） ---------------------------
    def take_pending(self, limit: int | None = None) -> list[CaptureRequest]:
        """取走排队中的抓档请求（app 层拿去做真正的 ``CAP20``）。

        ``limit`` 给「一个时间片只抓一条」用：**只取前 limit 条，剩下的留队**。
        早先版本是「全取走再切片」，被切掉的请求就静默丢了 —— 一轮里同时冒出
        两三个可疑源时，第二条永远抓不到（且计数上看不出来）。
        """
        if limit is None or limit >= len(self.pending):
            taken, self.pending = self.pending, []
            return taken
        limit = max(0, int(limit))
        taken, self.pending = self.pending[:limit], self.pending[limit:]
        return taken

    def deliver(self, request: CaptureRequest, capture: Any) -> CaptureRecord | None:
        """把一次抓档结果落盘并记账；``capture`` 只要有 samples_i/samples_q 等属性即可。"""
        try:
            record = self._save(request, capture)
        except Exception as exc:  # noqa: BLE001 - 单次落盘失败不能拖垮采集
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return None
        self.records.append(record)
        if self.store is not None:
            try:
                self.store.add_event("capture", "capture", record.as_dict())
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"store: {type(exc).__name__}: {exc}")
        return record

    def context(self, request: CaptureRequest, ts: float) -> dict[str, Any] | None:
        """抓档前后各 ``CONTEXT_S`` 秒的频谱上下文（来自 store 里本模块的快照）。

        store 没注入 / 库里没有这一段 → 返回 ``None`` 并在 sidecar 里写明原因，
        绝不假装有上下文。
        """
        if self.store is None:
            return None
        try:
            frames = [
                snap
                for snap in self.store.iter_between(
                    ts - CONTEXT_S,
                    ts + CONTEXT_S,
                    center_hz=int(request.candidate.window_hz),
                    module=self.id,
                )
            ]
        except Exception as exc:  # noqa: BLE001 - 上下文取不到不影响抓档本身
            self.errors.append(f"context: {type(exc).__name__}: {exc}")
            return None
        if not frames:
            return None
        bins = len(frames[0].bins_db)
        mean = [sum(snap.bins_db[i] for snap in frames) / len(frames) for i in range(bins)]
        step = max(1, bins // CONTEXT_BINS)
        spectrum = [
            round(max(mean[i : i + step]), 1) for i in range(0, bins, step)
        ][:CONTEXT_BINS]
        return {
            "window_hz": int(request.candidate.window_hz),
            "before_s": CONTEXT_S,
            "after_s": CONTEXT_S,
            "frames": len(frames),
            "first_ts": round(frames[0].ts, 3),
            "last_ts": round(frames[-1].ts, 3),
            "noise_p10": round(percentile([b for snap in frames for b in snap.bins_db], FLOOR_PCT), 1),
            "peak": round(max(max(snap.bins_db) for snap in frames), 1),
            "mean_spectrum": spectrum,
            "spectrum_bins": len(spectrum),
            "spectrum_step_hz": round(SCAN_BW_HZ / bins * step),
            "note": "本模块该窗的快照均值谱（下采样取每桶最大），承载单位是相对码不是 dBm",
        }

    def _save(self, request: CaptureRequest, capture: Any) -> CaptureRecord:
        samples_i: Iterable[int] = getattr(capture, "samples_i", ())
        samples_q: Iterable[int] = getattr(capture, "samples_q", ())
        pairs = list(zip(samples_i, samples_q))
        bits = int(getattr(capture, "bits", request.bits) or request.bits)
        rate_code = int(getattr(capture, "rate_code", request.rate_code) or request.rate_code)
        from core.serial_link import RATE_TABLE

        rate_hz = int(getattr(capture, "sample_rate_hz", 0) or RATE_TABLE.get(rate_code, 0))
        duration_us = int(getattr(capture, "duration_us", 0) or 0)
        freq_hz = int(getattr(capture, "freq_hz", 0) or request.freq_hz)
        # int16 小端交错：CAP16/CAP20 的数值范围都能装下，读档不用管位宽
        blob = bytearray()
        for i, q in pairs:
            blob += int(i).to_bytes(2, "little", signed=True)
            blob += int(q).to_bytes(2, "little", signed=True)
        levels = [abs(i) for i, _q in pairs] or [0]
        peak = max(levels)
        rms = (sum(v * v for v in levels) / len(levels)) ** 0.5
        digest = hashlib.sha256(bytes(blob)).hexdigest()
        index = len(self.records) + 1
        now = time.time()
        context = self.context(request, now)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        base = f"cap{index:03d}-{freq_hz//1_000_000}MHz-{stamp}"
        path = ""
        sidecar = ""
        if self.out_dir:
            os.makedirs(self.out_dir, exist_ok=True)
            path = os.path.join(self.out_dir, base + ".iq16")
            sidecar = os.path.join(self.out_dir, base + ".json")
            with open(path, "wb") as handle:
                handle.write(bytes(blob))
            meta = {
                "kind": "capture",
                "id": f"CAP-{index:03d}",
                "freq_hz": freq_hz,
                "samples": len(pairs),
                "bits": bits,
                "rate_code": rate_code,
                "sample_rate_hz": rate_hz,
                "duration_us": duration_us,
                "format": "int16 LE interleaved I,Q",
                "bytes": len(blob),
                "wire": (
                    "CAP16 线上是 8 bit 有符号 I/Q 交错；CAP20 线上是 10 bit 紧密打包、"
                    "且固件发的是 (I, -Q)，解析时已把 Q 取反还原。两种位宽在这里统一解成 "
                    "int16 LE 交错存档（每样本对 4 字节），读档不用管位宽。"
                ),
                "sha256": digest,
                "classification": {
                    "label": request.candidate.classification.label,
                    "confidence": request.candidate.classification.confidence,
                    "reason": request.candidate.classification.reason,
                },
                "candidate": request.candidate.as_dict(),
                "context": context,
                "context_note": (
                    "已存抓档前后各 30 s 本模块快照均值谱"
                    if context
                    else "没有可用快照上下文（store 未注入，或该窗这段时间没有帧）"
                ),
                "boundary": [
                    "一次突发抓取（非连续录制），抓档期间停扫",
                    "I/Q 是相对量，未做绝对校准，不是 dBm",
                    "分类是启发式（带宽/持续/占空比），不做调制识别、不做设备归因",
                ],
            }
            with open(sidecar, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, ensure_ascii=False, indent=2)
        note = ""
        if not len(pairs):
            note = "空抓档（0 样本）"
        return CaptureRecord(
            index=index,
            ts=now,
            freq_hz=freq_hz,
            samples=len(pairs),
            bits=bits,
            rate_code=rate_code,
            sample_rate_hz=rate_hz,
            duration_us=duration_us,
            path=path,
            sidecar=sidecar,
            sha256=digest,
            label=request.candidate.classification.label,
            confidence=request.candidate.classification.confidence,
            reason=request.candidate.classification.reason,
            peak=peak,
            rms=float(rms),
            note=note,
        )

    # ---- 输出 ------------------------------------------------------------
    def thresholds(self) -> dict[str, float]:
        return {
            "alert_db": ALERT_DB,
            "narrow_alert_db": NARROW_ALERT_DB,
            "narrow_max_hz": float(NARROW_MAX_HZ),
            "busy_fill": BUSY_FILL,
            "min_span_s": MIN_SPAN_S,
            "frame_min_gap_s": FRAME_MIN_GAP_S,
            "occ_excess_db": OCC_EXCESS_DB,
            "hop_fill": HOP_FILL,
            "steady_fill": STEADY_FILL,
            "min_frames": float(MIN_FRAMES),
            "min_width_hz": float(MIN_WIDTH_HZ),
            "max_width_hz": float(MAX_WIDTH_HZ),
            "retrigger_s": RETRIGGER_S,
            "max_captures": float(MAX_CAPTURES),
            "capture_samples": float(min(CAPTURE_SAMPLES, FIRMWARE_MAX_SAMPLES)),
            "capture_rate_code": float(CAPTURE_RATE_CODE),
            "analyze_min_interval_s": ANALYZE_MIN_INTERVAL_S,
            "spur_max_hz": float(SPUR_MAX_HZ),
        }

    def web_payload(self) -> dict[str, Any]:
        return {
            "kind": "capture",
            "band": {"lo_hz": BAND_LO_HZ, "hi_hz": BAND_HI_HZ},
            "scan": {
                "bw_hz": SCAN_BW_HZ,
                "step_hz": SCAN_STEP_HZ,
                "windows": len(self.windows) or len(window_centers()),
            },
            "captures": [record.as_dict() for record in self.records],
            #: 存档目录（界面显示用；没注入 store/out_dir 时为空 = 只统计不落盘）
            "dir": self.out_dir or "",
            "pending": len(self.pending),
            "triggers": self.triggers,
            "skipped_budget": self.skipped_budget,
            "skipped_repeat": self.skipped_repeat,
            "skipped_edge": self.skipped_edge,
            "skipped_weak": self.skipped_weak,
            "skipped_short": self.skipped_short,
            "skipped_busy": self.skipped_busy,
            "skipped_spur": self.skipped_spur,
            "spurs": [
                {
                    "freq_mhz": round(c.center_hz / 1e6, 3),
                    "width_khz": round(c.width_hz / 1e3, 1),
                    "fill": round(c.fill, 2),
                    "peak_excess_db": round(c.peak_excess_db, 1),
                    "duration_s": round(c.duration_s, 1),
                }
                for c in self.spurs[:20]
            ],
            "analyses": self.analyses,
            "frames": self.frames,
            "dropped_frames": self.dropped,
            "span_s": round(self.span_s(), 2),
            "errors": list(self.errors),
            "thresholds": self.thresholds(),
            "notes": self.notes(),
        }

    def notes(self) -> list[str]:
        return [
            "周期快照，非连续实时：一轮 6 窗，抓档时射频停扫（单射频分时）。",
            "功率码/IQ 都是相对量（0..255 / 有符号整数），不是 dBm，没做过绝对校准。",
            f"抓档参数：CAP{CAPTURE_BITS}（{CAPTURE_BITS} 位 I/Q）、"
            f"码 {CAPTURE_RATE_CODE} = {20_000_000/1e6:.0f} MS/s、"
            f"{min(CAPTURE_SAMPLES, FIRMWARE_MAX_SAMPLES)} 样本 ≈ "
            f"{min(CAPTURE_SAMPLES, FIRMWARE_MAX_SAMPLES)/20e6*1e3:.2f} ms；"
            "固件一次突发上限就是 IQ_WORDS（C5 = 16380），不是连续录制。",
            "底噪参考取逐帧 p10（拿 p25 会把「占窗 90% 的宽带」当成整体底噪抬升而看不见）；"
            "若整窗一起抬升（连 p10 都抬）= 增益/底噪漂移，不判成信号。",
            f"触发条件：启发式分类落 unknown（触发时候选形如 {KIND_CW}/{KIND_STEADY}/"
            f"{KIND_AMBIGUOUS}/{KIND_BURST}），"
            f"且抬升 ≥{ALERT_DB:.0f} 码、占空 ≥{HOP_FILL:.0%}、连续 ≥{MIN_FRAMES} 帧。",
            f"同一个频点（±{MATCH_HZ/1e6:.0f} MHz）抓过一次后 {RETRIGGER_S:.0f} s 内不重复抓；"
            f"整轮最多 {MAX_CAPTURES} 次（防止抓档把时间片吃光）。",
            f"每次抓档的 sidecar JSON 带前后各 {CONTEXT_S:.0f} s 的本模块均值谱（"
            f"下采样到 {CONTEXT_BINS} 格）；store 没注入时如实写「没有可用上下文」。",
            "分类是启发式的（只看带宽/持续/占空比），不做调制识别、不做设备级归因；"
            "「分不清的宽带」是明确不给结论，不是漏判。",
            f"窄带持续源（≤{SPUR_MAX_HZ/1e3:.0f} kHz 且占空 ≥{STEADY_FILL:.0%}）当**本机杂散**处理，"
            f"报清单但不抓拍（本轮 {self.skipped_spur} 根：真板本振/时钟是真的会一直响）——"
            "想找窄带窃听器得靠人工靠近对比，不在自动判定范围。",
            f"窄段（<{NARROW_MAX_HZ/1e6:.0f} MHz）要求抬升 ≥{NARROW_ALERT_DB:.0f} 码：噪声纹波能造出 "
            f"+20 码的假凸起，本模块另有 {self.skipped_weak} 段因此被挡。",
            f"窗内抬起格占比 ≥{BUSY_FILL:.0%} 视为环境饱和（满负荷 WiFi/微波炉），"
            f"不做逐段判定（本轮 {self.skipped_busy} 窗）；帧按 ≥{FRAME_MIN_GAP_S} s 抽帧，"
            f"缓冲覆盖不足 {MIN_SPAN_S:.0f} s 时不判（本轮 {self.skipped_short} 次）——"
            "真板一次扫描能吐 60+ 帧，不抽帧的话「40 帧」其实只覆盖 0.1 s。",
            "贴着窗边的段不判（宽度被窗切掉、不可信；≥15 MHz 的宽带例外，因为它本来就顶满整窗），"
            f"本轮丢弃过 {self.skipped_edge} 段。",
            f"不判两段：窗中心 ±{CENTER_NOTCH_HZ/1e3:.0f} kHz（接收机直流/LO 泄漏）与窗两侧各 "
            f"{EDGE_GUARD_HZ/1e6:.1f} MHz（滚降）。",
        ]


__all__ = [
    "ALERT_DB",
    "ANALYZE_MIN_INTERVAL_S",
    "BAND_HI_HZ",
    "BAND_LO_HZ",
    "BINS",
    "CAPTURE_BITS",
    "CAPTURE_RATE_CODE",
    "CAPTURE_SAMPLES",
    "CENTER_NOTCH_HZ",
    "CONTEXT_BINS",
    "CONTEXT_S",
    "Candidate",
    "CaptureMode",
    "CaptureRecord",
    "CaptureRequest",
    "DWELL_MS",
    "EDGE_GUARD_HZ",
    "FIRMWARE_MAX_SAMPLES",
    "FLOOR_PCT",
    "HOP_FILL",
    "KIND_AMBIGUOUS",
    "KIND_BURST",
    "KIND_STEADY",
    "KIND_CW",
    "MATCH_HZ",
    "MAX_CAPTURES",
    "MAX_WIDTH_HZ",
    "MIN_FRAMES",
    "MIN_WIDTH_HZ",
    "MIN_SPAN_S",
    "BUSY_FILL",
    "FRAME_MIN_GAP_S",
    "NARROW_ALERT_DB",
    "NARROW_MAX_HZ",
    "SPUR_MAX_HZ",
    "KIND_SPUR",
    "OCC_EXCESS_DB",
    "RETRIGGER_S",
    "RING_FRAMES",
    "SCAN_BW_HZ",
    "SCAN_STEP_HZ",
    "SMOOTH_BINS",
    "STEADY_FILL",
    "WindowStat",
    "kind_of",
    "masked_bins",
    "notch_bins",
    "percentile",
    "runs_of",
    "runs_of_state",
    "smooth",
    "window_centers",
]