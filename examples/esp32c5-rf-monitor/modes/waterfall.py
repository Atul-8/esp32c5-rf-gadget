"""任务 2：周期性频谱瀑布图。

职责（只做两件事，不碰 ``core/``）：

1. **轮扫**：把 2.4G ISM / 5G 低段 / 5G 高段切成 ``20 MHz 窗 + 15 MHz 步进``的
   重叠窗口，一轮扫完三个频段（快照由调度器顺路入库，本模块不额外占扫描时间）。
2. **出图**：``produce()`` 把环形缓冲里的最近若干帧交给 ``output.heatmap``
   渲染成「时频热力图（PNG + HTML）」；历史全量渲染走 CLI（读 SQLite），见 ``app/waterfall.py``。

判据（见 ``tests/test_waterfall.py``）：

* ``microwave`` 场景：2430–2490 MHz 必须是一条**连续的宽带亮带**（微波炉漏能特征）；
* ``mixed`` 场景：2412 MHz 必须是**窄带 + 明暗交替**（WiFi 突发块）
  且与微波炉宽带在同一张图上肉眼可分；
* 产出物上必须带「周期快照，非连续实时」声明（HTML 与 PNG 标题都有）。

边界（写进产出物，不许美化）：

* **周期快照，非连续实时** —— 单射频分时扫描，帧与帧之间有接收空档；
* 功率码是**相对 dB 码（0..255）**，不是 dBm，色标只能说"相对强弱"；
* 每轮 44 个窗口 × 100 ms ≈ 5 s 一轮，时频图的时间分辨率就是这个量级。
"""
from __future__ import annotations

import base64
import os
import time
from collections import deque
from typing import Any, Sequence

from core.registry import ModePlugin, Output, ScanRequest
from core.snapshot import Snapshot
from output.heatmap import (
    BANDS,
    BANNER,
    Band,
    blank_grid,
    code_range,
    fill_row,
    group_sweeps,
    render_waterfall,
)

#: 单个扫描窗带宽（用户裁决：5G 宽窗改成窄窗多次扫、允许重叠）
SWEEP_BW_HZ = 20_000_000
#: 中心频率步进（用户裁决：20 MHz 窗 / 15 MHz 步进）
SWEEP_STEP_HZ = 15_000_000
#: 单窗驻留时间（固件下限 100 ms）
DWELL_MS = 100
#: 每帧 bin 数（固件上限 512）
BINS = 512
#: 环形缓冲保留帧数（每帧 512 bin ≈ 4 KB，1200 帧 ≈ 5 MB ≈ 2 分钟数据）
RING_FRAMES = 1200
#: ``produce()`` 默认页时长（每页 5 分钟，一小时 12 页，可翻页看小时级历史）
PAGE_SECONDS = 300.0
#: 上位机「瀑布图」页最多画多少行（一行 = 一轮完整轮扫，不是固定时间桶）
WEB_ROWS = 48
#: 上位机每个频段的列数：三频段等宽并排，够看出窄带（BLE/CW）与宽带（微波炉），
#: 又不至于把 QWebChannel 灌满（3 段 × 48 行 × 180 列 = 25920 字节，base64 后约 35 KB）
WEB_COLS = 180
#: 上位机每秒都会 poll 一次 ``produce()``；PNG 全量渲染有成本，限制最小间隔。
#: CLI 只调一次，不受影响；要立刻出图用 ``produce(force=True)``。
PRODUCE_MIN_INTERVAL_S = 15.0

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data", "waterfall"
)


def window_centers(
    lo_hz: int,
    hi_hz: int,
    bw_hz: int = SWEEP_BW_HZ,
    step_hz: int = SWEEP_STEP_HZ,
) -> list[int]:
    """覆盖 ``[lo_hz, hi_hz]`` 的中心频点序列（首尾都要盖到，相邻窗必须重叠）。

    首窗左沿压频段下沿、末窗右沿压频段上沿，中间把剩余跨度**均分**
    （每段 ≤ ``step_hz``）。均分而不是"按步进走到底再补一窗"，
    是为了避免最后一跳超过步进（那样会留下 >15 MHz 的空档）。
    """
    if hi_hz <= lo_hz:
        raise ValueError("频段上沿必须大于下沿")
    if bw_hz <= 0 or step_hz <= 0:
        raise ValueError("窗宽与步进必须为正")
    half = bw_hz // 2
    start = lo_hz + half
    stop = hi_hz - half
    if stop <= start:  # 频段比窗还窄：一个窗居中盖住
        return [(lo_hz + hi_hz) // 2]
    span = stop - start
    count = max(1, -(-span // step_hz))  # 向上取整
    centers: list[int] = []
    for index in range(count + 1):
        center = start + span * index // count
        if not centers or center > centers[-1]:
            centers.append(center)
    if centers[-1] != stop:
        centers[-1] = stop
    return centers


def sweep_plan(bands: Sequence[Band] = BANDS) -> list[tuple[Band, list[int]]]:
    """一轮轮扫的完整计划：每个频段 + 它的窗口中心序列。"""
    return [(band, window_centers(band.lo_hz, band.hi_hz)) for band in bands]


def sweep_seconds(bands: Sequence[Band] = BANDS, dwell_ms: int = DWELL_MS) -> float:
    """跑完一轮（三个频段所有窗）要多久，单位秒。"""
    return sum(len(centers) for _band, centers in sweep_plan(bands)) * dwell_ms / 1000.0


class WaterfallMonitor(ModePlugin):
    """周期性全频段轮扫 + 时频热力图产出。"""

    id = "waterfall"
    name = "周期性频谱瀑布图"
    slice_weight = 1.0  # 与 wifi_check 各占一半射频时间（单射频分时，诚实的一半速度）

    def __init__(
        self,
        out_dir: str | None = None,
        page_seconds: float = PAGE_SECONDS,
        ring_frames: int = RING_FRAMES,
    ) -> None:
        self.out_dir = out_dir or DEFAULT_OUT
        self.page_seconds = page_seconds
        self._ring: deque[Snapshot] = deque(maxlen=max(8, ring_frames))
        self._last_output: Output | None = None
        self._last_render_at = 0.0
        self.frames = 0
        self.sweeps = 0
        self.per_band_frames: dict[str, int] = {band.id: 0 for band in BANDS}
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self._windows = 0
        #: 本模块自己的窗（按固件口径取整到 MHz）→ 计划里的窗序号。
        #: 单射频是**分时**扫的：上位机里 wifi_check / capture / guard 的窗也会流到这里，
        #: 若照单全收，环形缓冲会被别人的窗插花，``group_sweeps()`` 的「频点回卷 = 换轮」
        #: 假设就不成立（实测真板：48 行只能拼出 2 行，5G HIGH 那一段永远是空的）。
        self._plan_index: dict[int, int] = {}
        plan_index = 0
        for _band, centers in sweep_plan():
            for center in centers:
                # 固件 ``FREQ`` 只收整 MHz，回读的窗中心是取整后的值，这里按同一口径建表
                self._plan_index.setdefault(int(round(center / 1e6)) * 1_000_000, plan_index)
                plan_index += 1
            self._windows += len(centers)

    # ---- 契约 ------------------------------------------------------------
    def scan_requests(self) -> list[ScanRequest]:
        requests: list[ScanRequest] = []
        for band, centers in sweep_plan():
            for center in centers:
                requests.append(
                    ScanRequest(
                        freq_hz=int(center),
                        bw_hz=SWEEP_BW_HZ,
                        dwell_ms=DWELL_MS,
                        bins=BINS,
                    )
                )
        self.sweeps += 1
        return requests

    def on_snapshot(self, snap: Snapshot) -> None:
        # 只收**本模块计划里的窗**：别的模块的窗混进来会把「一轮」切碎（见 _plan_index 注释）
        if int(round(snap.center_hz / 1e6)) * 1_000_000 not in self._plan_index:
            return
        self.frames += 1
        if self.first_ts is None:
            self.first_ts = snap.ts
        self.last_ts = snap.ts
        for band in BANDS:
            if band.lo_hz <= snap.center_hz <= band.hi_hz:
                self.per_band_frames[band.id] += 1
                break
        # 环形缓冲按**窗**存，同窗连帧最大保持合并。
        # 真板一次 scan 能回 30+ 帧，若一帧一条，1200 帧连一轮（44 窗 × 37 帧 ≈ 1600）都装不下，
        # 上位机就永远只画得出一行半（实测 rows=2 / 30 s）。
        last = self._ring[-1] if self._ring else None
        if last is not None and last.center_hz == snap.center_hz and len(last.bins_db) == len(snap.bins_db):
            merged = [a if a >= b else b for a, b in zip(last.bins_db, snap.bins_db)]
            self._ring[-1] = Snapshot(
                ts=snap.ts,
                center_hz=snap.center_hz,
                bw_hz=snap.bw_hz,
                bins_db=merged,
                meta=dict(last.meta, **snap.meta),
            )
        else:
            self._ring.append(snap)

    def produce(self, force: bool = False) -> Output:
        """渲染最近缓冲的时频热力图（PNG + HTML）。

        上位机 ``HostSession.tables()`` 每秒都会调一次；这里做最小间隔节流，
        避免每秒重渲一遍 PNG。CLI/报告要立刻出图就传 ``force=True``。
        """
        now = time.monotonic()
        if (
            not force
            and self._last_output is not None
            and (now - self._last_render_at) < PRODUCE_MIN_INTERVAL_S
        ):
            return self._last_output
        self._last_render_at = now
        notes = [
            BANNER,
            f"缓冲 {len(self._ring)} 帧 / 累计 {self.frames} 帧，"
            f"{self._windows} 个窗 × {DWELL_MS} ms ≈ {sweep_seconds():.1f} s 一轮",
            "功率码是相对 dB 码（0..255），不是 dBm；色标只表示相对强弱。",
        ]
        if not self._ring:
            self._last_output = Output(
                kind="figure",
                title="周期性频谱瀑布图",
                notes=notes + ["还没有快照：先跑一轮扫描（app/waterfall.py --minutes N）"],
                payload={"bands": [], "frames": 0, "pages": 0},
            )
            return self._last_output
        result = render_waterfall(
            list(self._ring),
            self.out_dir,
            page_seconds=self.page_seconds,
        )
        rows = [
            [
                page.index,
                page.duration_s,
                page.frames,
                f"{page.code_range[0]}–{page.code_range[1]}",
                os.path.basename(page.png),
            ]
            for page in result.pages
        ]
        self._last_output = Output(
            kind="figure",
            title="周期性频谱瀑布图（时频热力图）",
            columns=["页", "时长 s", "帧数", "色标码", "PNG"],
            rows=rows,
            files=list(result.files),
            notes=notes,
            payload={
                "bands": self.band_summary(),
                "frames": result.frames,
                "pages": result.page_count,
                "span_s": round(result.span[1] - result.span[0], 1),
                "index": result.index_html,
                "files": result.files,
            },
        )
        return self._last_output

    # ---- 供上位机 / 报告用 ------------------------------------------------
    def web_payload(self) -> dict[str, Any]:
        """给上位机「瀑布图」页的紧凑数据（**一行 = 一轮完整轮扫**）。

        为什么发 base64 而不是 JSON 整数数组：功率码本来就是 0..255 的字节，
        一格一字节即可无损传输；48×180×3 = 25920 字节 → base64 约 35 KB，
        比 JSON 整数数组（约 100 KB+）小得多，1 Hz 推送才不会拖慢界面。

        口径与 PNG 完全一致：同一套 ``output.heatmap`` 矩阵构建（同格多窗取最大保持，
        0 码 = 这一格没扫到）；颜色映射由前端用 ``--code_lo/code_hi`` 拉伸，
        不改判决、不碰 ``bins_db`` 原值。
        """
        snaps = list(self._ring)
        sweeps = group_sweeps(snaps) if snaps else []
        rows = sweeps[-WEB_ROWS:]
        # 顶端那一行常常是**扫到一半的当前轮**：窗是按频率顺序扫的，5G HIGH 排在最后，
        # 于是「最新一行」里 5G HIGH 永远还没扫到 → 前端把这一行画出来就成了
        # 「5G HIGH 一片空白、别的频段正常」，看着像频段丢了（真板实测用户就是这么报的）。
        # 只画**扫满**的轮次；一轮都还没扫满时（刚开跑）才退回画当前轮，并把缺口如实标出来。
        complete = [sweep for sweep in rows if len(sweep) >= self._windows]
        pending = 0
        degraded = False
        if complete:
            if rows and len(rows[-1]) < self._windows:
                pending = len(rows[-1])
            rows = complete[-WEB_ROWS:]
        elif rows:
            # 一轮都还没扫满（刚开跑，或者轮次被打断）。
            # 老实现只画最后一行 = 当前轮，而窗按频率顺序扫、高段排在最后，
            # 于是「最新一行」的高段永远是空的 —— 用户会以为频段丢了（真板实测就是这么报的）。
            # 现在至少画最近**两**行：当前轮 + 上一轮，高段上一轮扫到的数据就能显示出来。
            degraded = True
            rows = rows[-2:] if len(rows) >= 2 else rows[-1:]
        grids = {band.id: blank_grid(len(rows), WEB_COLS) for band in BANDS}
        cache: dict[Any, Any] = {}
        for index, sweep in enumerate(rows):
            for snap in sweep:
                fill_row(grids, BANDS, snap, index, cache)
        lo, hi = code_range(grids) if rows else code_range({})
        t0 = rows[0][0].ts if rows else 0.0
        # 只用**扫满**的轮次估周期：环形缓冲最多留最近 RING_FRAMES 个窗，最老那一轮常被截断，
        # 拿它算会把周期算小（实测真板 30 s 只剩 2 轮、首轮是残的 → 1.8 s < 射频 4.4 s）
        stamps = [sweep[0].ts for sweep in rows if len(sweep) >= self._windows]
        deltas = sorted(b - a for a, b in zip(stamps, stamps[1:]) if b > a)
        # 多模块分时（上位机里 wifi_check 一起跑）会把整轮拉长，所以给前端一个**实测**墙钟周期
        row_period_s = round(deltas[len(deltas) // 2], 2) if deltas else 0.0
        bands: list[dict[str, Any]] = []
        for band in BANDS:
            raw = b"".join(bytes(row) for row in grids[band.id])
            bands.append(
                {
                    "id": band.id,
                    "label": band.label,
                    "lo_mhz": round(band.lo_hz / 1e6, 1),
                    "hi_mhz": round(band.hi_hz / 1e6, 1),
                    "cells": base64.b64encode(raw).decode("ascii"),
                }
            )
        return {
            "kind": "waterfall",
            "banner": BANNER,
            "rows": len(rows),
            "cols": WEB_COLS,
            "sweeps": len(sweeps),
            "frames": self.frames,
            "ring_frames": len(snaps),
            "windows": self._windows,
            "sweep_s": round(sweep_seconds(), 2),
            "row_period_s": row_period_s,
            "dwell_ms": DWELL_MS,
            "bw_mhz": round(SWEEP_BW_HZ / 1e6, 1),
            "step_mhz": round(SWEEP_STEP_HZ / 1e6, 1),
            "code_lo": lo,
            "code_hi": hi,
            "t0": round(t0, 3),
            "row_ts": [round(sweep[0].ts - t0, 2) for sweep in rows],
            "row_frames": [len(sweep) for sweep in rows],
            # 当前这轮已经扫了多少窗（还没扫满就画不成一行）；前端据此显示进度，不画空行
            "pending_windows": pending,
            "rows_complete": bool(complete),
            # 降级：没有扫满的轮，画的是最近两行（高段可能是上一轮的）
            "degraded": degraded,
            "bands": bands,
            "cover": self.band_summary(),
        }

    def band_summary(self) -> list[dict[str, Any]]:
        """每个频段的覆盖情况（给上位机画图 / 给报告做表）。"""
        out: list[dict[str, Any]] = []
        for band, centers in sweep_plan():
            out.append(
                {
                    "id": band.id,
                    "label": band.label,
                    "lo_mhz": band.lo_hz / 1e6,
                    "hi_mhz": band.hi_hz / 1e6,
                    "windows": len(centers),
                    "frames": self.per_band_frames.get(band.id, 0),
                }
            )
        return out

    def summary(self) -> dict[str, Any]:
        base = super().summary()
        base.update(
            {
                "frames": self.frames,
                "sweeps": self.sweeps,
                "span_s": round((self.last_ts or 0) - (self.first_ts or 0), 2),
            }
        )
        return base

    def reset(self) -> None:
        self._ring.clear()
        self._last_output = None
        self._last_render_at = 0.0
        self.frames = 0
        self.sweeps = 0
        self.per_band_frames = {band.id: 0 for band in BANDS}
        self.first_ts = None
        self.last_ts = None


__all__ = [
    "BINS",
    "DWELL_MS",
    "PAGE_SECONDS",
    "PRODUCE_MIN_INTERVAL_S",
    "RING_FRAMES",
    "SWEEP_BW_HZ",
    "SWEEP_STEP_HZ",
    "WEB_COLS",
    "WEB_ROWS",
    "WaterfallMonitor",
    "sweep_plan",
    "sweep_seconds",
    "window_centers",
]