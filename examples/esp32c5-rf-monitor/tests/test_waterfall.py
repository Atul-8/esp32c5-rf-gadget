"""`modes/waterfall.py` + `app/waterfall.py` 的测试：轮扫计划、契约、CLI 端到端。"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path

from app.waterfall import PacedMockSource, main as cli_main, virtual_plan
from core.registry import Registry, ScanRequest
from core.snapshot import Snapshot
from modes.waterfall import (
    BINS,
    DWELL_MS,
    SWEEP_BW_HZ,
    SWEEP_STEP_HZ,
    WEB_COLS,
    WEB_ROWS,
    WaterfallMonitor,
    sweep_plan,
    sweep_seconds,
    window_centers,
)
from output.heatmap import BANDS
from tests.mock_source import MockSource

MB = 1_000_000


class WindowCentersTest(unittest.TestCase):
    def test_covers_band_edges(self) -> None:
        centers = window_centers(BANDS[0].lo_hz, BANDS[0].hi_hz)
        self.assertGreaterEqual(centers[0] - SWEEP_BW_HZ // 2, BANDS[0].lo_hz)
        self.assertLessEqual(centers[-1] + SWEEP_BW_HZ // 2, BANDS[0].hi_hz)
        self.assertLessEqual(centers[0] - SWEEP_BW_HZ // 2, BANDS[0].lo_hz + SWEEP_STEP_HZ)
        self.assertGreaterEqual(centers[-1] + SWEEP_BW_HZ // 2, BANDS[0].hi_hz - 1)

    def test_ascending_and_overlapping(self) -> None:
        centers = window_centers(BANDS[0].lo_hz, BANDS[0].hi_hz)
        self.assertEqual(centers, sorted(centers))
        self.assertEqual(len(centers), len(set(centers)))
        for prev, cur in zip(centers, centers[1:]):
            gap = cur - prev
            self.assertGreater(gap, 0)
            self.assertLessEqual(gap, SWEEP_STEP_HZ, "步进不得超过用户裁决的 15 MHz")
            self.assertLess(gap, SWEEP_BW_HZ, "相邻窗必须重叠（gap < 窗宽）")

    def test_short_band_gives_single_window_when_it_fits(self) -> None:
        self.assertEqual(window_centers(2_400 * MB, 2_420 * MB), [2_410 * MB])

    def test_rejects_inverted_band(self) -> None:
        with self.assertRaises(ValueError):
            window_centers(2_500 * MB, 2_400 * MB)


class SweepPlanTest(unittest.TestCase):
    def test_three_bands_and_firmware_limits(self) -> None:
        plan = sweep_plan()
        self.assertEqual([band.id for band, _ in plan], ["24g", "5gl", "5gh"])
        total = 0
        for band, centers in plan:
            total += len(centers)
            for center in centers:
                self.assertGreaterEqual(center, 100 * MB)
                self.assertLessEqual(center, 6_000 * MB)
                self.assertLessEqual(center + SWEEP_BW_HZ // 2, 5_900 * MB)
        self.assertEqual(total, 44, "任务书口径：一轮 44 个 20 MHz 窗")
        self.assertAlmostEqual(sweep_seconds(), 4.4, places=6)

    def test_monitor_requests_match_plan(self) -> None:
        monitor = WaterfallMonitor()
        requests = monitor.scan_requests()
        self.assertEqual(len(requests), 44)
        self.assertEqual(monitor.sweeps, 1)
        self.assertTrue(all(isinstance(r, ScanRequest) for r in requests))
        for request in requests:
            self.assertEqual(request.bw_hz, SWEEP_BW_HZ)
            self.assertEqual(request.bins, BINS)
            self.assertGreaterEqual(request.dwell_ms, 100)
            self.assertLessEqual(request.dwell_ms, 60_000)
        expected = [center for _band, centers in sweep_plan() for center in centers]
        self.assertEqual([r.freq_hz for r in requests], expected)


class WaterfallMonitorTest(unittest.TestCase):
    def test_empty_produce_is_honest(self) -> None:
        monitor = WaterfallMonitor()
        output = monitor.produce()
        self.assertEqual(output.kind, "figure")
        self.assertEqual(output.payload["frames"], 0)
        self.assertEqual(output.payload["pages"], 0)
        joined = " ".join(output.notes)
        self.assertIn("周期快照，非连续实时", joined)
        self.assertIn("不是 dBm", joined)

    def test_on_snapshot_covers_bands(self) -> None:
        monitor = WaterfallMonitor()
        for band, centers in sweep_plan():
            for center in centers:
                monitor.on_snapshot(
                    Snapshot(
                        ts=1_000.0 + monitor.frames * 0.1,
                        center_hz=int(center),
                        bw_hz=SWEEP_BW_HZ,
                        bins_db=(20,) * BINS,
                    )
                )
        self.assertEqual(monitor.frames, 44)
        self.assertEqual(monitor.per_band_frames["24g"], 6)
        self.assertEqual(monitor.per_band_frames["5gl"], 13)
        self.assertEqual(monitor.per_band_frames["5gh"], 25)
        summary = monitor.summary()
        self.assertEqual(summary["frames"], 44)
        self.assertAlmostEqual(summary["span_s"], 4.3, places=6)
        self.assertEqual([b["windows"] for b in monitor.band_summary()], [6, 13, 25])

    def test_reset_clears_state(self) -> None:
        monitor = WaterfallMonitor()
        monitor.scan_requests()
        monitor.on_snapshot(
            Snapshot(ts=1.0, center_hz=2_410 * MB, bw_hz=SWEEP_BW_HZ, bins_db=(30,) * BINS)
        )
        monitor.reset()
        self.assertEqual(monitor.frames, 0)
        self.assertEqual(monitor.sweeps, 0)
        self.assertEqual(monitor.per_band_frames, {"24g": 0, "5gl": 0, "5gh": 0})
        self.assertIsNone(monitor.first_ts)
        self.assertEqual(monitor.produce().payload["frames"], 0)

    def test_produce_writes_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = WaterfallMonitor(out_dir=tmp, ring_frames=64)
            for band, centers in sweep_plan():
                for center in centers:
                    monitor.on_snapshot(
                        Snapshot(
                            ts=1_000.0 + monitor.frames * 0.1,
                            center_hz=int(center),
                            bw_hz=SWEEP_BW_HZ,
                            bins_db=tuple(20 + (i % 7) for i in range(BINS)),
                        )
                    )
            output = monitor.produce()
            self.assertEqual(output.payload["pages"], 1)
            self.assertEqual(output.payload["frames"], 44)
            self.assertTrue(Path(output.payload["index"]).exists())
            self.assertGreaterEqual(len(output.files), 3)  # index + png + html
            for path in output.files:
                self.assertTrue(Path(path).exists(), path)
            self.assertEqual(output.columns, ["页", "时长 s", "帧数", "色标码", "PNG"])

    def test_produce_is_throttled_but_force_rerenders(self) -> None:
        """上位机每秒都会 poll 一次 produce()，必须节流；点了「出图」按钮要能强制重渲。"""
        monitor = WaterfallMonitor()
        monitor.on_snapshot(
            Snapshot(ts=1.0, center_hz=2_410 * MB, bw_hz=SWEEP_BW_HZ, bins_db=(30,) * BINS)
        )
        first = monitor.produce(force=True)
        again = monitor.produce()
        self.assertIs(again, first, "最小间隔内应该直接复用上一次的产物对象")
        forced = monitor.produce(force=True)
        self.assertIsNot(forced, first, "force=True 必须真的重渲")
        monitor.reset()
        self.assertIsNot(monitor.produce(force=True), forced, "reset() 之后缓存要清掉")


class WebPayloadTest(unittest.TestCase):
    """上位机「瀑布图」页的数据契约：一格一字节 + 一行 = 一轮完整轮扫。"""

    def _feed(self, monitor: WaterfallMonitor, sweeps: int) -> None:
        for index in range(sweeps):
            for band, centers in sweep_plan():
                for center in centers:
                    level = 20 + (index * 3 + len(band.id)) % 9
                    monitor.on_snapshot(
                        Snapshot(
                            ts=1_000.0 + monitor.frames * 0.1,
                            center_hz=int(center),
                            bw_hz=SWEEP_BW_HZ,
                            bins_db=tuple(level for _ in range(BINS)),
                        )
                    )

    def test_shape_and_encoding(self) -> None:
        monitor = WaterfallMonitor(ring_frames=256)
        self._feed(monitor, 2)
        payload = monitor.web_payload()
        self.assertEqual(payload["kind"], "waterfall")
        self.assertEqual(payload["rows"], 2)
        self.assertEqual(payload["cols"], WEB_COLS)
        self.assertEqual(payload["sweeps"], 2)
        self.assertEqual([band["id"] for band in payload["bands"]], ["24g", "5gl", "5gh"])
        for band in payload["bands"]:
            raw = base64.b64decode(band["cells"])
            self.assertEqual(len(raw), payload["rows"] * payload["cols"], "一格必须正好一字节")
            self.assertGreater(max(raw), 0, "扫过的格子不该全是 0 码（0 = 没扫到）")
        # 时间轴：第一行是 0，往后递增
        self.assertEqual(payload["row_ts"][0], 0.0)
        self.assertEqual(payload["row_ts"], sorted(payload["row_ts"]))
        self.assertIn("周期快照", payload["banner"])
        # 两轮以上的时间戳差要能给出一个正数墙钟周期（单轮时为 0，前端退回射频时间）
        self.assertGreater(payload["row_period_s"], 0.0)
        self.assertAlmostEqual(payload["row_period_s"], 4.4, delta=0.2)
        self.assertEqual([cover["windows"] for cover in payload["cover"]], [6, 13, 25])

    def test_empty_payload_is_honest(self) -> None:
        payload = WaterfallMonitor().web_payload()
        self.assertEqual(payload["rows"], 0)
        self.assertEqual(payload["sweeps"], 0)
        for band in payload["bands"]:
            self.assertEqual(base64.b64decode(band["cells"]), b"")
        self.assertEqual(payload["row_ts"], [])

    def test_ring_merges_frames_of_same_window(self) -> None:
        """真板一次 scan 回 30+ 帧：环形缓冲必须按**窗**存，否则连一轮都装不下。"""
        monitor = WaterfallMonitor(ring_frames=64)
        for level in (30, 55, 41):
            monitor.on_snapshot(
                Snapshot(
                    ts=1_000.0 + level * 0.001,
                    center_hz=2_410 * MB,
                    bw_hz=SWEEP_BW_HZ,
                    bins_db=(level,) * BINS,
                )
            )
        # 换到下一格：必须用**计划里的窗**（12.7 MHz 均分出来的 2422.7 MHz）。
        # 计划外的频点现在会被丢掉——单射频分时，别的模块的窗不能插进这张图。
        monitor.on_snapshot(
            Snapshot(ts=1_010.0, center_hz=2_422_700_000, bw_hz=SWEEP_BW_HZ, bins_db=(20,) * BINS)
        )
        self.assertEqual(monitor.frames, 4, "frames 仍然数真帧，不能因为合并漏计")
        self.assertEqual(len(monitor._ring), 2, "同窗连帧要合并成一条")
        self.assertEqual(max(monitor._ring[0].bins_db), 55, "合并取最大保持（与热力图口径一致）")
        self.assertEqual(monitor._ring[1].center_hz, 2_422_700_000, "换窗要新起一条")

    def test_row_period_needs_two_complete_sweeps(self) -> None:
        """只有一轮是扫满的，就别硬算周期（真板截断残轮会把周期算小到 1.8 s）。"""
        monitor = WaterfallMonitor(ring_frames=256)  # 够装下这 1.5 轮，残的全在最后一轮
        centers = [c for _band, rows in sweep_plan() for c in rows]
        total = len(centers)
        for index in range(total + total // 2):
            monitor.on_snapshot(
                Snapshot(
                    ts=1_000.0 + index * 0.1,
                    center_hz=int(centers[index % total]),
                    bw_hz=SWEEP_BW_HZ,
                    bins_db=(30,) * BINS,
                )
            )
        payload = monitor.web_payload()
        complete = [c for c in payload["row_frames"] if c >= payload["windows"]]
        self.assertEqual(len(complete), 1, "这里只喂了 1.5 轮，扫满的只有 1 轮")
        self.assertEqual(payload["row_period_s"], 0.0, "凑不齐两整轮就返回 0，让前端退回射频时间")

    def test_row_limit_keeps_newest(self) -> None:
        # 环形缓冲要够大：否则被裁掉的是「最旧的那几轮」，测不到 WEB_ROWS 这个行数上限
        monitor = WaterfallMonitor(ring_frames=(WEB_ROWS + 4) * 64)
        self._feed(monitor, WEB_ROWS + 3)
        payload = monitor.web_payload()
        self.assertEqual(payload["rows"], WEB_ROWS, "只画最近 WEB_ROWS 轮，别把 48 行画爆")
        self.assertEqual(payload["sweeps"], WEB_ROWS + 3)
        self.assertEqual(len(payload["bands"][0]["cells"]), len(payload["bands"][0]["cells"]))


class PacedMockSourceTest(unittest.TestCase):
    def test_virtual_timeline_is_linear(self) -> None:
        source = PacedMockSource(
            MockSource(scenario="quiet", frames_per_scan=2), seconds_per_frame=0.1, t0=500.0
        )
        request = ScanRequest(freq_hz=2_410 * MB, bw_hz=SWEEP_BW_HZ, dwell_ms=DWELL_MS, bins=BINS)
        snaps = list(source.scan(request))
        self.assertEqual(len(snaps), 2)
        self.assertAlmostEqual(snaps[0].ts, 500.1, places=6)
        self.assertAlmostEqual(snaps[1].ts, 500.2, places=6)
        self.assertTrue(snaps[0].meta["virtual_time"])
        self.assertAlmostEqual(source.dt, 0.1)

    def test_virtual_plan_honours_minutes(self) -> None:
        snapshots, dt, span = virtual_plan(10.0, 1)
        self.assertAlmostEqual(span, 600.0, delta=sweep_seconds())
        self.assertGreaterEqual(snapshots, 44)
        self.assertAlmostEqual(dt, 0.1, places=6)
        # 帧数 × 每帧耗时 == 等效时长
        self.assertAlmostEqual(snapshots * dt, span, places=3)


class CliTest(unittest.TestCase):
    def test_end_to_end_mock_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "wf.db")
            out = os.path.join(tmp, "out")
            code = cli_main(
                [
                    "--minutes",
                    "0.2",
                    "--scenario",
                    "microwave",
                    "--db",
                    db,
                    "--out",
                    out,
                    "--frames-per-scan",
                    "1",
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(db))
            index = Path(out) / "index.html"
            self.assertTrue(index.exists())
            html = index.read_text(encoding="utf-8")
            self.assertIn("周期快照，非连续实时", html)
            pngs = sorted(Path(out).glob("waterfall-p*.png"))
            self.assertTrue(pngs)
            self.assertGreater(pngs[0].stat().st_size, 2_000)

    def test_render_only_requires_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                cli_main(["--render-only", "--db", os.path.join(tmp, "none.db"), "--out", tmp])


if __name__ == "__main__":
    unittest.main()