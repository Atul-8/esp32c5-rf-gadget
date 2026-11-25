"""端到端：注册 → 调度 → 收快照 → 入库 → 产出（全部用 mock，无硬件）。"""
import os
import tempfile
import unittest

from core.registry import Registry, discover
from core.scheduler import Scheduler
from core.store import Store
from tests.mock_modes import make_plugin
from tests.mock_source import SCENARIOS, MockSource


class PipelineTests(unittest.TestCase):
    def test_full_chain_with_mock_source(self):
        tmp = tempfile.mkdtemp(prefix="c5mon-pipe-")
        db = os.path.join(tmp, "pipe.db")
        csv_path = os.path.join(tmp, "pipe.csv")
        # 全链路用测试替身，不用真实 modes/（功能分支会往里加模块）。
        registry = Registry()
        for plugin_id, weight in (("wifi_check", 4.0), ("ble_risk", 3.0), ("guard_58g", 2.0), ("capture", 1.0)):
            registry.register(make_plugin(plugin_id, weight))
        source = MockSource(scenario="mixed", realtime=False, frames_per_scan=2)
        with Store(db, csv_path=csv_path) as store:
            scheduler = Scheduler(source, registry, store=store)
            stats = scheduler.run(max_slices=200)
            self.assertEqual(stats.slices, 200)
            self.assertEqual(stats.snapshots, 400)
            self.assertEqual(store.count(), 400)
            self.assertEqual(set(store.count_by_module()), {"wifi_check", "ble_risk", "guard_58g", "capture"})
            share = stats.share()
            self.assertAlmostEqual(share["wifi_check"], 0.4, delta=0.05)
            self.assertAlmostEqual(share["ble_risk"], 0.3, delta=0.05)
            self.assertAlmostEqual(share["guard_58g"], 0.2, delta=0.05)
            self.assertAlmostEqual(share["capture"], 0.1, delta=0.05)
            for plugin in registry.plugins:
                output = plugin.produce()
                self.assertEqual(output.kind, "table")
                self.assertTrue(output.rows)
                self.assertTrue(output.notes)

    def test_scenarios_produce_distinguishable_spectra(self):
        """每个场景的关键判据必须在 mock 数据里真的成立，否则功能无法自测。"""
        source = MockSource(scenario="wifi_busy", realtime=False, frames_per_scan=1)
        snap = next(source.scan(make_plugin("probe", 1.0, 2_442_000_000, 80_000_000).requests[0]))
        self.assertGreater(snap.band_mean_db(2_402_000_000, 2_422_000_000), snap.band_mean_db(2_452_000_000, 2_472_000_000))

        quiet = MockSource(scenario="quiet", realtime=False, frames_per_scan=1)
        quiet_snap = next(quiet.scan(make_plugin("probe", 1.0, 2_442_000_000, 80_000_000).requests[0]))
        self.assertLess(quiet_snap.peak()[1], 60, "安静场景不该有强信号")

        video = MockSource(scenario="video_58g", realtime=False, frames_per_scan=1)
        video_snap = next(video.scan(make_plugin("probe", 1.0, 5_780_000_000, 80_000_000).requests[0]))
        peak_hz, _peak_db = video_snap.peak()
        self.assertLess(abs(peak_hz - 5_740_000_000), 700_000, "5.8G 场景的持续源应在 5740MHz")

        unknown = MockSource(scenario="unknown_cw", realtime=False, frames_per_scan=1)
        unknown_snap = next(unknown.scan(make_plugin("probe", 1.0, 2_442_000_000, 80_000_000).requests[0]))
        peak_hz, _ = unknown_snap.peak()
        self.assertLess(abs(peak_hz - 2_413_000_000), 700_000)

    def test_every_scenario_renders(self):
        for name in SCENARIOS:
            source = MockSource(scenario=name, realtime=False, frames_per_scan=1)
            snap = next(source.scan(make_plugin("probe", 1.0, 2_442_000_000, 80_000_000).requests[0]))
            self.assertEqual(snap.bin_count, 512)
            self.assertTrue(snap.summary())


class OutputFormatTests(unittest.TestCase):
    def test_table_alignment_with_cjk(self):
        from output.format import ascii_table

        text = ascii_table(["模块", "片数"], [["wifi", 3], ["守卫", 12]])
        lines = text.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertIn("wifi", text)
        self.assertIn("守卫", text)

    def test_bar_and_units(self):
        from output.format import bar, fmt_duration, fmt_hz, fmt_pct

        self.assertEqual(bar(0, 100, 5), "·····")
        self.assertEqual(bar(100, 100, 5), "█████")
        self.assertEqual(fmt_hz(2_442_000_000), "2.442 GHz")
        self.assertEqual(fmt_pct(0.256), "25.6%")
        self.assertEqual(fmt_duration(90), "1.5min")


if __name__ == "__main__":
    unittest.main()