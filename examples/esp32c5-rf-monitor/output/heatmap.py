"""任务 2 产出物：把历史快照渲染成时频热力图（PNG + 可翻页 HTML）。

边界声明（产出物上必须出现，不许美化）：

    **周期快照，非连续实时** —— 单射频分时扫描，一个频点驻留完才跳到下一个，
    帧与帧之间存在接收空档。图上每一行是"扫到过"的功率码，不是连续采样的结果。

渲染规则（写进 HTML 页脚，也写在这里）：

* 每个频段一个横条，等宽显示（720 px），横轴刻度按各自跨度标注；
* **一行 = 一轮完整轮扫**（三个频段 44 个窗，≈4.4 s），不是固定 1 s 时间桶；
  这样每个频段的每一行都是被真实扫到过的，不会出现"这 1 秒只扫了 2.4G"的空洞；
* 列 = 一个频率格（频段跨度 / 720），同一格被多个窗覆盖时取**最大保持**；
* 纵轴时间自上而下递增（越往下越新），刻度取自每轮的首帧时间戳；
* 功率码是**相对 dB 码（0..255），不是 dBm**，色标只做"相对强弱"。

历史数据从 `core.store.Store` 来，且必须只取本模块的帧::

    with Store(db_path) as store:
        render_waterfall(store.iter_between(t0, t1, module="waterfall"), ".data/wf")
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from core.snapshot import Snapshot
from output.png import Canvas, Color, read_png_header, text_width

MB = 1_000_000

BANNER = "周期快照，非连续实时 —— 单射频分时扫描，帧与帧之间存在接收空档；图上的行是「扫到过」的功率码，不是连续采样。"


@dataclass(frozen=True)
class Band:
    """一个轮扫频段（与 `modes/waterfall.py` 的计划共用同一份定义）。"""

    id: str
    label: str
    lo_hz: int
    hi_hz: int

    @property
    def span_hz(self) -> int:
        return self.hi_hz - self.lo_hz


BANDS: tuple[Band, ...] = (
    Band("24g", "2.4G ISM", 2_400 * MB, 2_483_500_000),
    Band("5gl", "5G LOW", 5_150 * MB, 5_350 * MB),
    Band("5gh", "5G HIGH", 5_470 * MB, 5_850 * MB),
)

# 冷→热色标：暗蓝 → 蓝 → 青 → 绿 → 黄 → 橙 → 红
COLOR_STOPS: tuple[tuple[float, Color], ...] = (
    (0.00, (6, 10, 22)),
    (0.30, (18, 52, 110)),
    (0.50, (0, 132, 168)),
    (0.66, (16, 178, 130)),
    (0.80, (214, 186, 48)),
    (0.92, (226, 110, 40)),
    (1.00, (236, 52, 42)),
)

STRIP_W = 720
MARGIN_L = 44
MARGIN_R = 10
MARGIN_T = 36
MARGIN_B = 34
STRIP_GAP = 12
LABEL_H = 12
AXIS_H = 13
TICK_PX_MIN = 55
TICK_STEPS_MHZ = (10, 20, 25, 50, 100, 200, 250, 500)
#: 单轮帧数上限：防御性上限，防止某个频段数据永远不"回卷"时行数失控
SWEEP_FRAME_LIMIT = 4096


# --------------------------------------------------------------------------
# 色标
# --------------------------------------------------------------------------
def color_lut(size: int = 256) -> list[Color]:
    """把 0..1 的一维强度摊成 256 级 RGB。"""
    stops = COLOR_STOPS
    lut: list[Color] = []
    for i in range(size):
        v = i / (size - 1)
        for k in range(len(stops) - 1):
            v0, c0 = stops[k]
            v1, c1 = stops[k + 1]
            if v0 <= v <= v1:
                t = 0.0 if v1 == v0 else (v - v0) / (v1 - v0)
                lut.append(
                    (
                        int(round(c0[0] + (c1[0] - c0[0]) * t)),
                        int(round(c0[1] + (c1[1] - c0[1]) * t)),
                        int(round(c0[2] + (c1[2] - c0[2]) * t)),
                    )
                )
                break
        else:  # pragma: no cover - 浮点边界兜底
            lut.append(stops[-1][1])
    return lut


LUT = color_lut()

#: ASCII 预演用的字符梯度（0 = 没扫到/最暗）
ASCII_RAMP = " .:-=+*#%@"


# --------------------------------------------------------------------------
# 页 / 渲染结果
# --------------------------------------------------------------------------
@dataclass
class Page:
    """一页瀑布图：一段连续时间内的若干轮轮扫 + 落盘产物。"""

    index: int
    t0: float
    t1: float
    frames: int
    row_ts: list[float] = field(default_factory=list)
    grids: dict[str, list[bytearray]] = field(default_factory=dict)
    code_range: tuple[int, int] = (0, 255)
    png: str = ""
    html: str = ""

    @property
    def rows(self) -> int:
        return len(self.row_ts)

    @property
    def duration_s(self) -> float:
        return self.t1 - self.t0


@dataclass
class RenderResult:
    out_dir: str
    index_html: str
    pages: list[Page]
    files: list[str]
    span: tuple[float, float]
    frames: int

    @property
    def page_count(self) -> int:
        return len(self.pages)


# --------------------------------------------------------------------------
# 轮扫分组（一行 = 一轮）
# --------------------------------------------------------------------------
def group_sweeps(snapshots: Iterable[Snapshot]) -> list[list[Snapshot]]:
    """把按时间排序的快照切成"轮"：中心频点相对上一帧回卷 = 新一轮。

    轮扫顺序是 2410 → … → 5850 → 2410，所以 ``center_hz`` 变小就是换轮。
    只用于单模块数据（``store.iter_between(..., module="waterfall")``）。
    """
    sweeps: list[list[Snapshot]] = []
    current: list[Snapshot] = []
    prev: int | None = None
    for snap in snapshots:
        wrapped = prev is not None and snap.center_hz < prev
        if current and (wrapped or len(current) >= SWEEP_FRAME_LIMIT):
            sweeps.append(current)
            current = []
        current.append(snap)
        prev = snap.center_hz
    if current:
        sweeps.append(current)
    return sweeps


# --------------------------------------------------------------------------
# 矩阵构建
# --------------------------------------------------------------------------
def band_columns(band: Band, strip_w: int = STRIP_W) -> int:
    """一个频段在图上占多少个频率格。"""
    return max(8, int(strip_w))


def blank_grid(rows: int, cols: int) -> list[bytearray]:
    """rows×cols 的功率码矩阵；0 表示"这一格没扫到"（也渲成最暗色）。"""
    return [bytearray(cols) for _ in range(rows)]


def _bin_mapping(
    center_hz: int,
    bw_hz: int,
    n_bins: int,
    band: Band,
    cols: int,
) -> list[tuple[int, int]]:
    """一个频点在某个频段里命中的 (bin 下标, 列号) 列表。"""
    col_hz = band.span_hz / cols
    start = center_hz - bw_hz / 2
    bin_w = bw_hz / n_bins
    out: list[tuple[int, int]] = []
    for i in range(n_bins):
        freq = start + (i + 0.5) * bin_w
        if band.lo_hz <= freq < band.hi_hz:
            col = int((freq - band.lo_hz) / col_hz)
            out.append((i, cols - 1 if col >= cols else col))
    return out


def fill_row(
    grids: dict[str, list[bytearray]],
    bands: Sequence[Band],
    snap: Snapshot,
    row: int,
    cache: dict[tuple[int, int, int, str, int], list[tuple[int, int]]] | None = None,
) -> bool:
    """把一帧快照按最大保持写进矩阵的某一行；返回是否真的写进去了。"""
    first = grids[bands[0].id]
    if row < 0 or row >= len(first):
        return False
    codes = snap.bins_db
    n_bins = len(codes)
    wrote = False
    for band in bands:
        grid = grids[band.id]
        cols = len(grid[0])
        key = (snap.center_hz, snap.bw_hz, n_bins, band.id, cols)
        mapping = None if cache is None else cache.get(key)
        if mapping is None:
            mapping = _bin_mapping(snap.center_hz, snap.bw_hz, n_bins, band, cols)
            if cache is not None:
                cache[key] = mapping
        if not mapping:
            continue
        target = grid[row]
        for bin_index, col in mapping:
            value = int(codes[bin_index])
            if value > 255:
                value = 255
            if value > target[col]:
                target[col] = value
        wrote = True
    return wrote


def code_range(grids: dict[str, list[bytearray]]) -> tuple[int, int]:
    """取整页的非零码范围（用于色标映射）；空页给 (0, 255)。"""
    low, high = 256, -1
    for grid in grids.values():
        for row in grid:
            for value in row:
                if value == 0:
                    continue
                if value < low:
                    low = value
                if value > high:
                    high = value
    if high < 0:
        return 0, 255
    if high - low < 4:
        low = max(0, low - 2)
        high = min(255, high + 2)
    return low, high


def _normalize(value: int, lo: int, hi: int) -> int:
    if value <= 0:
        return 0
    span = max(1, hi - lo)
    return max(0, min(len(LUT) - 1, int((value - lo) / span * (len(LUT) - 1))))


# --------------------------------------------------------------------------
# 读图小工具（测试 / 自检都会用，所以放正本里）
# --------------------------------------------------------------------------
def column_profile(grid: list[bytearray]) -> list[float]:
    """每列的时间均值（列 = 频率格）。"""
    if not grid:
        return []
    rows = len(grid)
    cols = len(grid[0])
    return [sum(row[c] for row in grid) / rows for c in range(cols)]


def runs_above(profile: Sequence[float], threshold: float) -> list[tuple[int, int]]:
    """连续高于阈值的列区间（闭区间）。"""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(profile):
        if value >= threshold and start is None:
            start = index
        elif value < threshold and start is not None:
            out.append((start, index - 1))
            start = None
    if start is not None:
        out.append((start, len(profile) - 1))
    return out


def column_stats(grid: list[bytearray], col: int) -> tuple[float, float, int, int]:
    """某列 (min, max, 亮行数, 暗行数)，按 (min+max)/2 分亮暗。"""
    values = [row[col] for row in grid]
    low, high = min(values), max(values)
    mid = (low + high) / 2
    on = sum(1 for v in values if v > mid)
    return low, high, on, len(values) - on


def ascii_preview(
    grid: list[bytearray],
    width: int = 96,
    height: int = 18,
    lo: int | None = None,
    hi: int | None = None,
) -> str:
    """把矩阵压成 ASCII 灰阶图（人眼在终端里也能核对，不只依赖图片）。

    ``lo`` / ``hi`` 建议传整页的 ``page.code_range``：否则纯噪声的频段会被
    自身动态范围拉满，看起来"满屏都是信号"（假信号）。
    """
    if not grid:
        return ""
    rows, cols = len(grid), len(grid[0])
    if lo is None or hi is None:
        low, high = 0, 255
        nonzero = [v for row in grid for v in row if v]
        if nonzero:
            low, high = min(nonzero), max(nonzero)
    else:
        low, high = lo, hi
    lines: list[str] = []
    for r in range(min(height, rows)):
        row_from = int(r * rows / min(height, rows))
        row_to = max(row_from + 1, int((r + 1) * rows / min(height, rows)))
        chars: list[str] = []
        for c in range(width):
            col_from = int(c * cols / width)
            col_to = max(col_from + 1, int((c + 1) * cols / width))
            peak = 0
            for rr in range(row_from, min(row_to, rows)):
                for cc in range(col_from, min(col_to, cols)):
                    peak = max(peak, grid[rr][cc])
            level = _normalize(peak, low, high) / (len(LUT) - 1)
            chars.append(ASCII_RAMP[min(len(ASCII_RAMP) - 1, int(level * (len(ASCII_RAMP) - 1) + 0.5))])
        lines.append("|" + "".join(chars) + "|")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# PNG 渲染
# --------------------------------------------------------------------------
def _tick_step_hz(band: Band, strip_w: int) -> int:
    for step_mhz in TICK_STEPS_MHZ:
        if step_mhz * MB / band.span_hz * strip_w >= TICK_PX_MIN:
            return step_mhz * MB
    return TICK_STEPS_MHZ[-1] * MB


def _fmt_hhmmss(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _fmt_mmss(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def render_page_png(
    page: Page,
    bands: Sequence[Band],
    out_path: str | Path,
    strip_w: int = STRIP_W,
    title: str = "WATERFALL",
) -> str:
    """把一页矩阵画成 PNG（带频段标题、频率刻度、时间刻度、色标）。"""
    rows = max(1, page.rows)
    blocks = len(bands)
    height = MARGIN_T + blocks * (LABEL_H + rows + AXIS_H) + (blocks - 1) * STRIP_GAP + MARGIN_B
    width = MARGIN_L + strip_w + MARGIN_R
    canvas = Canvas(width, height, bg=(10, 12, 18))

    canvas.text(MARGIN_L, 8, f"{title} p{page.index}", (236, 240, 244))
    canvas.text(
        MARGIN_L,
        20,
        "periodic snapshot / not continuous - relative code, not dBm",
        (148, 156, 168),
    )

    lo, hi = page.code_range
    y = MARGIN_T
    for band in bands:
        grid = page.grids.get(band.id) or blank_grid(rows, strip_w)
        canvas.text(
            MARGIN_L,
            y,
            f"{band.label}  {band.lo_hz // MB}-{math.ceil(band.hi_hz / MB)} MHz",
            (206, 212, 220),
        )
        top = y + LABEL_H
        for r in range(rows):
            row_codes = grid[r] if r < len(grid) else bytearray(strip_w)
            buf = bytearray(strip_w * 3)
            limit = min(strip_w, len(row_codes))
            for c in range(limit):
                color = LUT[_normalize(row_codes[c], lo, hi)]
                at = c * 3
                buf[at] = color[0]
                buf[at + 1] = color[1]
                buf[at + 2] = color[2]
            base = ((top + r) * canvas.width + MARGIN_L) * 3
            canvas.pixels[base : base + len(buf)] = buf
        canvas.frame_rect(MARGIN_L - 1, top - 1, strip_w + 2, rows + 2, (52, 58, 70))

        # ---- 频率刻度 ----
        axis_y = top + rows + 3
        step = _tick_step_hz(band, strip_w)
        tick = (band.lo_hz // step) * step
        while tick <= band.hi_hz:
            if tick >= band.lo_hz:
                x = MARGIN_L + int((tick - band.lo_hz) / band.span_hz * strip_w)
                canvas.fill_rect(x, top + rows, 1, 3, (120, 128, 140))
                label = f"{tick // MB}"
                canvas.text(x - text_width(label) // 2, axis_y, label, (152, 158, 170))
            tick += step
        canvas.text(
            MARGIN_L + strip_w - text_width("MHz"),
            axis_y,
            "MHz",
            (110, 116, 128),
        )

        # ---- 时间刻度（左边缘，自上而下） ----
        marks = 4
        for k in range(marks + 1):
            frac = k / marks
            r = min(rows - 1, int(frac * (rows - 1)))
            ts = page.row_ts[r] if r < len(page.row_ts) else page.t0
            label = _fmt_mmss(ts - page.t0)
            # 刻度只画在左边距里：不许盖到数据像素上（否则图像与矩阵不一致）
            canvas.fill_rect(MARGIN_L - 4, top + r, 3, 1, (120, 128, 140))
            canvas.text(2, top + r - 3, label, (152, 158, 170))

        y = top + rows + AXIS_H + STRIP_GAP

    # ---- 色标 ----
    bar_w, bar_h = 240, 10
    bar_x = MARGIN_L
    bar_y = height - MARGIN_B + 8
    for i in range(bar_w):
        canvas.fill_rect(bar_x + i, bar_y, 1, bar_h, LUT[int(i / (bar_w - 1) * (len(LUT) - 1))])
    canvas.frame_rect(bar_x - 1, bar_y - 1, bar_w + 2, bar_h + 2, (52, 58, 70))
    canvas.text(bar_x, bar_y + bar_h + 3, f"code {lo}", (152, 158, 170))
    canvas.text(bar_x + bar_w - text_width(f"{hi}"), bar_y + bar_h + 3, f"{hi}", (152, 158, 170))
    canvas.text(
        bar_x + bar_w + 16,
        bar_y + 1,
        f"1 row = 1 sweep  rows={rows}  frames={page.frames}",
        (132, 138, 150),
    )
    return canvas.save(out_path)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
_CSS = """
:root { color-scheme: dark; }
body { margin: 0; padding: 24px; background: #0b0d12; color: #dfe3e8;
       font: 14px/1.6 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: #93a0b0; font-size: 13px; }
.banner { margin: 14px 0 18px; padding: 10px 14px; border-radius: 8px;
          background: #3a1a1a; border: 1px solid #7d3030; color: #ffb4a8; font-weight: 600; }
table { border-collapse: collapse; margin: 10px 0 18px; font-size: 13px; }
th, td { border: 1px solid #2a3140; padding: 4px 10px; text-align: left; }
th { background: #171c26; color: #aab6c6; font-weight: 600; }
img { max-width: 100%; height: auto; image-rendering: pixelated;
      border: 1px solid #2a3140; border-radius: 6px; background: #05070c; }
nav { margin: 16px 0 6px; display: flex; gap: 10px; align-items: center; }
nav a { color: #6fb3ff; text-decoration: none; padding: 4px 10px; border: 1px solid #2a3140;
        border-radius: 6px; }
nav a:hover { background: #171c26; }
nav a.off { color: #4a5261; pointer-events: none; border-color: #1b212b; }
.foot { margin-top: 22px; color: #7d8798; font-size: 12px; border-top: 1px solid #1b212b;
        padding-top: 12px; }
code { color: #9fd0ff; }
"""


def _nav(prev: str | None, index: str, nxt: str | None) -> str:
    def link(href: str | None, text: str) -> str:
        if href is None:
            return f'<a class="off">{text}</a>'
        return f'<a href="{href}">{text}</a>'

    return f"<nav>{link(prev, '&larr; 上一页')}{link(index, '目录')}{link(nxt, '下一页 &rarr;')}</nav>"


def render_page_html(
    page: Page,
    bands: Sequence[Band],
    total_pages: int,
    title: str,
    prev_name: str | None,
    next_name: str | None,
    index_name: str,
) -> str:
    """一页的 HTML（自带 PNG 引用、导航、边界声明）。"""
    rows_html = "".join(
        f"<tr><td>{band.label}</td><td>{band.lo_hz // MB} – {math.ceil(band.hi_hz / MB)} MHz</td>"
        f"<td>{band.span_hz // MB} MHz</td></tr>"
        for band in bands
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title} · 第 {page.index}/{total_pages} 页</title>
<style>{_CSS}</style></head>
<body>
<h1>{title} · 第 {page.index} / {total_pages} 页</h1>
<div class="sub">时间范围 {_fmt_hhmmss(page.t0)} – {_fmt_hhmmss(page.t1)}
 （{page.rows} 轮轮扫，{page.frames} 帧）</div>
<div class="banner">⚠ {BANNER}</div>
<table>
<tr><th>频段</th><th>覆盖</th><th>跨度</th></tr>
{rows_html}
</table>
<img src="{Path(page.png).name}" alt="{title} 第 {page.index} 页">
{_nav(prev_name, index_name, next_name)}
<div class="foot">
横轴 = 频率（每个频段等宽显示，刻度按各自跨度标注）；纵轴 = 时间，自上而下递增，刻度是相对本页开始的秒数。<br>
<b>一行 = 一轮完整轮扫</b>（三个频段 44 个 20 MHz 窗，驻留 100 ms，约 4.4 s 一轮），
所以图上每行都真的被扫到过，但同一行内不同频段的采样时刻并不相同。<br>
同一频率格被多个扫描窗覆盖时取<b>最大保持</b>；色标是相对功率码（0..255），<b>不是 dBm</b>。<br>
数据来源：SQLite 历史存档（<code>store.iter_between(..., module="waterfall")</code>），不是连续采样。
</div>
</body></html>
"""


def render_index_html(
    result: RenderResult,
    bands: Sequence[Band],
    title: str,
    error: str | None = None,
) -> str:
    rows = "".join(
        f'<tr><td><a href="{Path(p.html).name}">{p.index}</a></td>'
        f"<td>{_fmt_hhmmss(p.t0)} – {_fmt_hhmmss(p.t1)}</td>"
        f"<td>{p.rows}</td><td>{p.frames}</td>"
        f"<td>{p.code_range[0]} – {p.code_range[1]}</td></tr>"
        for p in result.pages
    )
    span = result.span[1] - result.span[0]
    note = f'<div class="banner">⚠ {error}</div>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title} · 目录</title>
<style>{_CSS}</style></head>
<body>
<h1>{title} · 目录</h1>
<div class="sub">{_fmt_hhmmss(result.span[0])} – {_fmt_hhmmss(result.span[1])}
 （共 {span:.0f} s，{result.page_count} 页，{result.frames} 帧）</div>
<div class="banner">⚠ {BANNER}</div>
{note}
<table>
<tr><th>页</th><th>时间范围</th><th>轮数</th><th>帧数</th><th>色标码范围</th></tr>
{rows}
</table>
<div class="foot">
页时长 {result.page_count and result.pages[0].duration_s:.0f} s（每页一个 PNG，可逐页翻看小时级历史）。<br>
频段：{'、'.join(f'{b.label} {b.lo_hz // MB}–{math.ceil(b.hi_hz / MB)} MHz' for b in bands)}。
</div>
</body></html>
"""


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def iter_pages(
    snapshots: Iterable[Snapshot],
    page_seconds: float,
    bands: Sequence[Band] = BANDS,
    strip_w: int = STRIP_W,
) -> Iterator[Page]:
    """按「轮扫」分组并按页切分（内部用，测试也直接吃它）。"""
    snaps = sorted(snapshots, key=lambda s: s.ts)
    if not snaps:
        return
    sweeps = group_sweeps(snaps)
    if not sweeps:
        return
    t0 = sweeps[0][0].ts
    buckets: dict[int, list[list[Snapshot]]] = {}
    for sweep in sweeps:
        index = max(1, int((sweep[0].ts - t0) // page_seconds) + 1)
        buckets.setdefault(index, []).append(sweep)
    cache: dict[tuple[int, int, int, str, int], list[tuple[int, int]]] = {}
    for index in sorted(buckets):
        group = buckets[index]
        p0 = t0 + (index - 1) * page_seconds
        p1 = max(
            p0 + page_seconds,
            max(sweep[-1].ts for sweep in group),
        )
        rows = len(group)
        grids = {band.id: blank_grid(rows, band_columns(band, strip_w)) for band in bands}
        row_ts: list[float] = []
        frames = 0
        for row, sweep in enumerate(group):
            row_ts.append(sweep[0].ts)
            for snap in sweep:
                if fill_row(grids, bands, snap, row, cache):
                    frames += 1
        yield Page(
            index=index,
            t0=p0,
            t1=p1,
            frames=frames,
            row_ts=row_ts,
            grids=grids,
            code_range=code_range(grids),
        )


def render_waterfall(
    snapshots: Iterable[Snapshot],
    out_dir: str | Path,
    *,
    page_seconds: float = 300.0,
    strip_w: int = STRIP_W,
    bands: Sequence[Band] = BANDS,
    title: str = "周期频谱瀑布图",
) -> RenderResult:
    """把一批历史快照渲染成 ``index.html`` + 每页 PNG/HTML。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    snaps = list(snapshots)
    pages = list(iter_pages(snaps, page_seconds, bands, strip_w))
    if not pages:
        index = target / "index.html"
        empty = RenderResult(
            out_dir=str(target),
            index_html="",
            pages=[],
            files=[],
            span=(0.0, 0.0),
            frames=0,
        )
        index.write_text(
            render_index_html(empty, bands, title, error="没有历史快照（先跑一次轮扫或 --minutes N 生成）"),
            encoding="utf-8",
        )
        return RenderResult(
            out_dir=str(target),
            index_html=str(index),
            pages=[],
            files=[str(index)],
            span=(0.0, 0.0),
            frames=0,
        )

    for page in pages:
        stem = f"waterfall-p{page.index}"
        page.png = str(target / f"{stem}.png")
        render_page_png(page, bands, page.png, strip_w=strip_w, title="WATERFALL")
        page.html = str(target / f"{stem}.html")

    result = RenderResult(
        out_dir=str(target),
        index_html="",
        pages=pages,
        files=[],
        span=(pages[0].t0, pages[-1].t1),
        frames=sum(p.frames for p in pages),
    )
    for page in pages:
        prev = Path(pages[page.index - 2].html).name if page.index > 1 else None
        nxt = Path(pages[page.index].html).name if page.index < len(pages) else None
        Path(page.html).write_text(
            render_page_html(page, bands, len(pages), title, prev, nxt, "index.html"),
            encoding="utf-8",
        )
    index = target / "index.html"
    result.index_html = str(index)
    index.write_text(render_index_html(result, bands, title), encoding="utf-8")
    result.files = [str(index)]
    for page in pages:
        result.files.extend([page.png, page.html])
    return result


__all__ = [
    "ASCII_RAMP",
    "BANDS",
    "BANNER",
    "Band",
    "Page",
    "RenderResult",
    "ascii_preview",
    "blank_grid",
    "code_range",
    "color_lut",
    "column_profile",
    "column_stats",
    "fill_row",
    "group_sweeps",
    "iter_pages",
    "read_png_header",
    "render_index_html",
    "render_page_html",
    "render_page_png",
    "render_waterfall",
    "runs_above",
]