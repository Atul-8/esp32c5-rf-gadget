"""`output/heatmap.py` 的测试：分组、矩阵、渲染与「图里真的能看出信号」。

两类用例：

* **手搓数据**（完全可控）：验证"一行 = 一轮、最大保持、宽带/窄带判定"这些机制；
* **mock 场景**（`microwave` / `mixed`）：验证任务书的验收判据——
  微波炉宽带噪声与 WiFi 突发块在同一张图里可分辨。
"""
from __future__ import annotations

import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path

from core.snapshot import Snapshot
from modes.waterfall import BINS, SWEEP_BW_HZ, sweep_plan
import output.heatmap as hm
from output.heatmap import (
    BANDS,
    BANNER,
    LUT,
    ascii_preview,
    code_range,
    column_profile,
    column_stats,
    group_sweeps,
    iter_pages,
    read_png_header,
    render_waterfall,
    runs_above,
)
from tests.mock_source import SpectrumSynthesizer

MB = 1_000_000
BAND_24 = BANDS[0]


def synthetic_codes(center_hz: int, on_burst: bool) -> tuple[int, ...]:
    """手搓一帧：2412 窄带突发 + 2430–2490 宽带底噪抬升 + 其余噪声底。"""
    start = center_hz - SWEEP_BW_HZ / 2
    step = SWEEP_BW_HZ / BINS
    out: list[int] = []
    for index in range(BINS):
        freq = start + (index + 0.5) * step
        value = 20
        if 2_430 * MB <= freq <= 2_490 * MB:
            value = 60  # 宽带（微波炉）
        if abs(freq - 2_412 * MB) <= 10 * MB and on_burst:
            value = 180  # 窄带突发（WiFi）
        out.append(value)
    return tuple(out)


def make_sweeps(sweeps: int, band_only: bool = True, dt: float = 0.1) -> list[Snapshot]:
    """按真实轮扫顺序造若干轮快照（偶数轮才"有突发"）。"""
    plan = [(BANDS[0], sweep_plan()[0][1])] if band_only else sweep_plan()
    snaps: list[Snapshot] = []
    ts = 1_000.0
    for sweep_index in range(sweeps):
        for band, centers in plan:
            for center in centers:
                snaps.append(
                    Snapshot(
                        ts=ts,
                        center_hz=int(center),
                        bw_hz=SWEEP_BW_HZ,
                        bins_db=synthetic_codes(center, on_burst=sweep_index % 2 == 0),
                    )
                )
                ts += dt
    return snaps


def mock_snapshots(scenario: str, sweeps: int, dt: float = 0.1, seed: int = 7) -> list[Snapshot]:
    """用 `tests/mock_source.SpectrumSynthesizer` 造真实场景数据（三频段全扫）。"""
    synth = SpectrumSynthesizer(scenario, seed=seed)
    snaps: list[Snapshot] = []
    ts = 2_000.0
    for _sweep in range(sweeps):
        for _band, centers in sweep_plan():
            for center in centers:
                snaps.append(
                    Snapshot(
                        ts=ts,
                        center_hz=int(center),
                        bw_hz=SWEEP_BW_HZ,
                        bins_db=synth.codes(center, SWEEP_BW_HZ, BINS),
                    )
                )
                ts += dt
    return snaps


class GroupSweepsTest(unittest.TestCase):
    def test_splits_when_center_wraps_back(self) -> None:
        snaps = make_sweeps(3)
        sweeps = group_sweeps(snaps)
        self.assertEqual(len(sweeps), 3)
        self.assertEqual(sum(len(s) for s in sweeps), len(snaps))
        for sweep in sweeps:
            centers = [s.center_hz for s in sweep]
            self.assertEqual(centers, sorted(centers))

    def test_single_band_never_wraps_but_is_one_sweep(self) -> None:
        snaps = make_sweeps(1)
        self.assertEqual(len(group_sweeps(snaps)), 1)

    def test_empty(self) -> None:
        self.assertEqual(group_sweeps([]), [])


class GridTest(unittest.TestCase):
    def test_fill_row_takes_max_hold(self) -> None:
        page = next(iter(iter_pages(make_sweeps(2), page_seconds=300.0)))
        grid = page.grids[BAND_24.id]
        self.assertEqual(page.rows, 2)
        col = int((2_412 * MB - BAND_24.lo_hz) / BAND_24.span_hz * len(grid[0]))
        low, high, on, off = column_stats(grid, col)
        self.assertEqual((low, high), (20, 180))  # 奇偶轮 = 有/无突发
        self.assertEqual((on, off), (1, 1))

    def test_wide_band_is_wide(self) -> None:
        page = next(iter(iter_pages(make_sweeps(2), page_seconds=300.0)))
        grid = page.grids[BAND_24.id]
        cols = len(grid[0])
        col_hz = BAND_24.span_hz / cols
        profile = column_profile(grid)
        # 60 MHz 宽带应落成一条连续亮带，边界误差不超过 2 个格
        threshold = (20 + 60) / 2
        runs = runs_above(profile, threshold)
        self.assertTrue(runs, "没有找到宽带")
        start, stop = max(runs, key=lambda r: r[1] - r[0])
        width_mhz = (stop - start + 1) * col_hz / MB
        # 手搓宽带是 2430–2490，但 2.4G 频段到 2483.5 就截断了 → 只剩 53.5 MHz
        self.assertGreaterEqual(width_mhz, 50.0)
        self.assertLessEqual(width_mhz, 56.0)
        self.assertAlmostEqual(BAND_24.lo_hz + start * col_hz, 2_430 * MB, delta=1 * MB)

    def test_band_outside_gets_no_data(self) -> None:
        page = next(iter(iter_pages(make_sweeps(2), page_seconds=300.0)))
        self.assertEqual(set(page.grids[BANDS[1].id][0]), {0})
        self.assertEqual(page.code_range, code_range(page.grids))


class PageSplitTest(unittest.TestCase):
    def test_pages_split_by_time(self) -> None:
        snaps = make_sweeps(6)  # 6 轮 × 6 窗 × 0.1 s = 3.6 s
        pages = list(iter_pages(snaps, page_seconds=1.0))
        self.assertGreaterEqual(len(pages), 2)
        self.assertEqual(sum(p.frames for p in pages), len(snaps))
        self.assertEqual(sum(p.rows for p in pages), 6)

    def test_no_snapshots(self) -> None:
        self.assertEqual(list(iter_pages([], page_seconds=10.0)), [])


class AsciiPreviewTest(unittest.TestCase):
    def test_preview_has_rows_and_width(self) -> None:
        page = next(iter(iter_pages(make_sweeps(6), page_seconds=300.0)))
        self.assertEqual(page.rows, 6)
        text = ascii_preview(page.grids[BAND_24.id], width=48, height=6, lo=20, hi=180)
        lines = text.splitlines()
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertEqual(len(line), 50)  # 两侧边框
        self.assertTrue(any(ch in text for ch in "#%@"), "亮带没画出来")


def decode_rgb(path: str | Path) -> tuple[int, int, list[bytes]]:
    """把 PNG 解成逐行像素（只认 filter 0 / RGB8，与本项目写出器一致）。"""
    data = Path(path).read_bytes()
    at = 8
    width = height = 0
    idat = bytearray()
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        tag = data[at + 4 : at + 8]
        body = data[at + 8 : at + 8 + length]
        if tag == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
        elif tag == b"IDAT":
            idat += body
        at += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = width * 3 + 1
    return width, height, [raw[y * stride + 1 : (y + 1) * stride] for y in range(height)]


class PngMatchesGridTest(unittest.TestCase):
    """PNG 上的每个像素必须就是矩阵里那格的颜色（不许被刻度线盖掉）。"""

    def test_strip_pixels_equal_lut_of_grid(self) -> None:
        snaps = make_sweeps(3)
        with tempfile.TemporaryDirectory() as tmp:
            result = render_waterfall(snaps, tmp, page_seconds=300.0)
            page = result.pages[0]
            width, height, rows = decode_rgb(page.png)
        lo, hi = page.code_range
        y = hm.MARGIN_T
        for band in BANDS:
            grid = page.grids[band.id]
            for r in range(page.rows):
                pixel = rows[y + hm.LABEL_H + r][hm.MARGIN_L * 3 : (hm.MARGIN_L + 720) * 3]
                expect = bytes(b for v in grid[r][:720] for b in LUT[hm._normalize(v, lo, hi)])
                self.assertEqual(pixel, expect, f"{band.id} 第 {r} 行像素与矩阵不一致")
            y += hm.LABEL_H + page.rows + hm.AXIS_H + hm.STRIP_GAP
        self.assertEqual(width, hm.MARGIN_L + 720 + hm.MARGIN_R)
        self.assertGreater(height, hm.MARGIN_T)


class RenderWaterfallTest(unittest.TestCase):
    def test_writes_png_html_index(self) -> None:
        snaps = make_sweeps(4)
        with tempfile.TemporaryDirectory() as tmp:
            result = render_waterfall(snaps, tmp, page_seconds=300.0)
            self.assertEqual(result.page_count, 1)
            self.assertEqual(result.frames, len(snaps))
            index = Path(result.index_html)
            self.assertTrue(index.exists())
            page = result.pages[0]
            header = read_png_header(page.png)
            self.assertGreater(header["width"], 700)
            self.assertGreater(header["height"], 60)
            self.assertEqual(header["color_type"], 2)
            html = Path(page.html).read_text(encoding="utf-8")
            self.assertIn("周期快照，非连续实时", html)
            self.assertIn(BANNER, html)
            self.assertIn(Path(page.png).name, html)
            self.assertIn("一行 = 一轮完整轮扫", html)
            index_html = index.read_text(encoding="utf-8")
            self.assertIn("周期快照，非连续实时", index_html)
            self.assertIn(Path(page.html).name, index_html)
            for path in result.files:
                self.assertTrue(Path(path).exists(), path)

    def test_empty_writes_error_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = render_waterfall([], tmp)
            self.assertEqual(result.page_count, 0)
            html = Path(result.index_html).read_text(encoding="utf-8")
            self.assertIn("没有历史快照", html)
            self.assertIn("周期快照，非连续实时", html)


class MockScenarioTest(unittest.TestCase):
    """任务书验收判据：mock 数据里 WiFi 突发块与微波炉宽带要能分辨。"""

    def test_microwave_wideband_with_brighter_wifi_inside(self) -> None:
        """`microwave` 场景：一条 ≥40 MHz 的连续抬升带，带内有更亮的一段 WiFi。"""
        page = next(iter(iter_pages(mock_snapshots("microwave", sweeps=10), 300.0)))
        grid = page.grids[BAND_24.id]
        cols = len(grid[0])
        col_hz = BAND_24.span_hz / cols
        profile = column_profile(grid)
        base = sorted(profile)[int(cols * 0.10)]  # 2400–2430 是干净段，取低分位当噪声底

        # 1) 宽带：连成一片，宽度 ≥ 40 MHz，起点压在 2430 MHz 附近
        wide = [r for r in runs_above(profile, base + 10) if (r[1] - r[0] + 1) * col_hz >= 40 * MB]
        self.assertTrue(wide, f"没找到微波炉宽带（base={base:.1f}）")
        start, stop = max(wide, key=lambda r: r[1] - r[0])
        self.assertLessEqual(abs(BAND_24.lo_hz + start * col_hz - 2_430 * MB), 6 * MB)

        # 2) 带内 WiFi（2437）明显更亮：可用"亮度差"把它从宽带噪声里分出来
        def mean_at(f0: float, f1: float) -> float:
            a = int((f0 * MB - BAND_24.lo_hz) / col_hz)
            b = int((f1 * MB - BAND_24.lo_hz) / col_hz)
            chunk = profile[a : b + 1]
            return sum(chunk) / len(chunk)

        wifi = mean_at(2432, 2442)
        shoulder = mean_at(2458, 2472)
        self.assertGreater(wifi - shoulder, 25, f"WiFi 段 {wifi:.1f} vs 宽带肩部 {shoulder:.1f}")

        # 3) 突发：同一列在时间轴上有亮有暗（不是恒定的一条平线）
        col = int((2_437 * MB - BAND_24.lo_hz) / col_hz)
        low, high, on, off = column_stats(grid, col)
        self.assertGreater(high - low, 20)
        self.assertGreater(on, 0)
        self.assertGreater(off, 0)

    def test_mixed_5gh_has_one_narrow_source(self) -> None:
        """`mixed`：5G 高段只有 5728–5752 一条窄带（图传/固定源），低段干净。"""
        page = next(iter(iter_pages(mock_snapshots("mixed", sweeps=10), 300.0)))
        band = BANDS[2]
        grid = page.grids[band.id]
        cols = len(grid[0])
        col_hz = band.span_hz / cols
        profile = column_profile(grid)
        median = sorted(profile)[int(cols * 0.5)]
        runs = runs_above(profile, median + 15)
        self.assertTrue(runs, "5G 高段没有信号")
        start, stop = max(runs, key=lambda r: r[1] - r[0])
        width = (stop - start + 1) * col_hz / MB
        self.assertLess(width, 30, "5G 高段的源应是窄带")
        self.assertAlmostEqual((band.lo_hz + start * col_hz) / MB, 5728, delta=3)

        low = page.grids[BANDS[1].id]
        low_profile = column_profile(low)
        low_median = sorted(low_profile)[int(cols * 0.5)]
        self.assertEqual(runs_above(low_profile, low_median + 15), [], "5G 低段不该有信号")

    def test_quiet_has_no_false_wideband(self) -> None:
        page = next(iter(iter_pages(mock_snapshots("quiet", sweeps=6), 300.0)))
        grid = page.grids[BAND_24.id]
        profile = column_profile(grid)
        median = sorted(profile)[int(len(profile) * 0.5)]
        self.assertEqual(runs_above(profile, median + 8), [])


if __name__ == "__main__":
    unittest.main()