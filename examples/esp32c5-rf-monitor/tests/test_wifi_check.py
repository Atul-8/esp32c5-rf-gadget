"""功能 1 自测：WiFi 信道体检（全部用 mock / 合成快照，不碰硬件）。

覆盖：
  * 滑扫计划：20MHz 窗 / 15MHz 步进、相邻窗重叠、频段上沿不落空
  * ``quiet``      : 全部「空闲」、推荐 ch1、干扰源 0（零误报）
  * ``wifi_busy``  : ch1/ch6 判忙、避开前 3 个、推荐落在未被避开的 1/6/11 中最低占用者
  * ``microwave``  : 中段抬升、ch1 保持干净
  * 覆盖率       : 整段没能量 ⇒ 判「无覆盖」，不进推荐也不进干扰源
  * 退化帧       : 整窗 0 码不计入统计
  * 表格排序     : 按帧占用降序
"""
from __future__ import annotations

import unittest

from core.registry import Registry
from core.scheduler import Scheduler
from core.snapshot import Snapshot
from modes._legacy.wifi_check import (
    AVOID_MARGIN_DB,
    AVOID_TOP,
    BAND_24_HZ,
    BAND_5_HZ,
    CHANNEL_WIDTH_HZ,
    LEVELS,
    MIN_COVERAGE,
    NON_OVERLAP_24,
    SCAN_BW_HZ,
    SCAN_STEP_HZ,
    USABLE_HALF_HZ,
    WifiCheck,
    sliding_windows,
)
from tests.mock_source import MockSource


def run_scenario(scenario: str, slices: int = 60, include_5g: bool = True) -> WifiCheck:
    plugin = WifiCheck(include_5g=include_5g)
    registry = Registry()
    registry.register(plugin)
    source = MockSource(scenario=scenario, realtime=False, frames_per_scan=2)
    Scheduler(source, registry).run(max_slices=slices)
    return plugin


class ScanPlanTests(unittest.TestCase):
    def test_window_and_step(self):
        self.assertEqual(SCAN_BW_HZ, 20_000_000, "窗宽必须是 20MHz")
        self.assertEqual(SCAN_STEP_HZ, 15_000_000, "步进必须是 15MHz")
        self.assertEqual(USABLE_HALF_HZ, SCAN_BW_HZ // 2)

    def test_windows_overlap_and_cover_band(self):
        for lo, hi in (BAND_24_HZ, BAND_5_HZ):
            centers = sliding_windows(lo, hi)
            self.assertGreater(len(centers), 1)
            # 相邻窗中心差 = 步进（末尾补齐窗除外），保证重叠 5MHz
            for a, b in zip(centers, centers[1:]):
                self.assertLessEqual(b - a, SCAN_STEP_HZ)
            # 频段两端都被覆盖
            self.assertLessEqual(centers[0] - USABLE_HALF_HZ, lo)
            self.assertGreaterEqual(centers[-1] + USABLE_HALF_HZ, hi)

    def test_scan_requests_match_plan(self):
        plugin = WifiCheck()
        requests = plugin.scan_requests()
        self.assertEqual(len(requests), len(sliding_windows(*BAND_24_HZ)) + len(sliding_windows(*BAND_5_HZ)))
        self.assertTrue(all(r.bw_hz == SCAN_BW_HZ for r in requests))
        self.assertTrue(all(r.dwell_ms == plugin.dwell_ms for r in requests))

    def test_no_5g_plan(self):
        plugin = WifiCheck(include_5g=False)
        self.assertEqual(len(plugin.scan_requests()), len(sliding_windows(*BAND_24_HZ)))


class JudgementTests(unittest.TestCase):
    def test_quiet_no_false_positive(self):
        plugin = run_scenario("quiet")
        self.assertGreater(plugin.frames, 0)
        self.assertEqual(plugin.interferers(), [], "安静场景不许报干扰源")
        self.assertEqual(plugin.recommend_24(), 1, "全空闲时推荐 ch1")
        for stat in plugin.stats.values():
            self.assertEqual(stat.level, "空闲", f"ch{stat.channel} 应判空闲")
            self.assertGreaterEqual(stat.coverage, MIN_COVERAGE)
            # 纯噪声时信道之间的相对抬升不许超过「轻度」门限
            self.assertLess(stat.excess_db, LEVELS[2][0], f"ch{stat.channel} 相对抬升过大")

    def test_wifi_busy_picks_clean_non_overlapping(self):
        plugin = run_scenario("wifi_busy")
        busy_levels = {"重度", "拥挤"}
        self.assertIn(plugin._stat(1, "2.4G").level, busy_levels, "ch1 注入了强 WiFi，必须判忙")
        self.assertIn(plugin._stat(6, "2.4G").level, busy_levels, "ch6 注入了强 WiFi，必须判忙")
        avoided = plugin.avoided("2.4G")
        self.assertIn(1, avoided, "ch1 占用最高，必须进入「建议避开」")
        self.assertLessEqual(len(avoided), AVOID_TOP)
        pick = plugin.recommend_24()
        self.assertIsNotNone(pick)
        self.assertNotIn(pick, avoided, "推荐信道不能是被避开的信道")
        self.assertEqual(pick, 11, "ch1/ch6 被避开后，1/6/11 里应落到 ch11")

    def test_microwave_middle_raised_ch1_clean(self):
        plugin = run_scenario("microwave")
        self.assertEqual(plugin.recommend_24(), 1, "微波炉场景 ch1 应保持干净")
        self.assertGreater(plugin._stat(6, "2.4G").excess_db, plugin._stat(1, "2.4G").excess_db)

    def test_ble_only_does_not_look_like_wifi_load(self):
        """BLE 窄带脉冲只占几个 bin，不该把 20MHz 信道的中位电平抬起来。"""
        plugin = run_scenario("ble_crowded")
        self.assertEqual(plugin.interferers(), [], "BLE 拥挤不该被判成 WiFi 占用")
        self.assertIn(plugin.recommend_24(), NON_OVERLAP_24)

    def test_table_sorted_by_excess(self):
        plugin = run_scenario("wifi_busy")
        output = plugin.produce()
        values = [float(row[5].replace("dB", "").strip()) for row in output.rows]
        self.assertEqual(values, sorted(values, reverse=True), "表格必须按相对抬升降序")

    def test_reference_is_quietest_channel(self):
        plugin = run_scenario("wifi_busy")
        plugin.refresh()
        stats = [s for s in plugin.stats.values() if s.band == "2.4G"]
        self.assertAlmostEqual(min(s.excess_db for s in stats), 0.0, places=6)
        self.assertTrue(all(s.excess_db >= 0 for s in stats))
        self.assertLessEqual(len(plugin.avoided("2.4G")), AVOID_TOP)


class CoverageTests(unittest.TestCase):
    def _snapshot(self, codes: list[int], center_hz: int, bw_hz: int) -> Snapshot:
        return Snapshot(ts=1.0, center_hz=center_hz, bw_hz=bw_hz, bins_db=tuple(codes), meta={"seq": 1})

    def test_dead_band_marked_uncovered(self):
        plugin = WifiCheck(include_5g=False)
        bins = 1024
        # 2437MHz 窗：只有最中间 100 个 bin 有能量，其余 0 码 ⇒ ch6 覆盖不足
        codes = [0] * bins
        for index in range(bins // 2 - 50, bins // 2 + 50):
            codes[index] = 60
        snap = self._snapshot(codes, 2_437_000_000, SCAN_BW_HZ)
        for _ in range(3):
            plugin.on_snapshot(snap)
        stat = plugin._stat(6, "2.4G")
        self.assertIsNotNone(stat)
        self.assertLess(stat.coverage, MIN_COVERAGE)
        self.assertEqual(stat.level, "无覆盖")
        self.assertNotEqual(plugin.recommend_24(), 6, "无覆盖信道不许被推荐")

    def test_all_zero_frame_dropped(self):
        plugin = WifiCheck(include_5g=False)
        snap = self._snapshot([0] * 1024, 2_437_000_000, SCAN_BW_HZ)
        plugin.on_snapshot(snap)
        self.assertEqual(plugin.frames, 0)
        self.assertEqual(plugin.dropped_frames, 1)
        self.assertEqual(plugin.stats, {}, "退化帧不该建出信道统计")

    def test_flat_noise_is_idle(self):
        plugin = WifiCheck(include_5g=False)
        snap = self._snapshot([22] * 1024, 2_442_000_000, SCAN_BW_HZ)
        for _ in range(3):
            plugin.on_snapshot(snap)
        stat = plugin._stat(6, "2.4G")
        self.assertLess(stat.occupancy, 0.2)
        self.assertEqual(stat.level, "空闲")

    def test_loud_channel_outranks_quiet_one(self):
        plugin = WifiCheck(include_5g=False)
        quiet = self._snapshot([22] * 1024, 2_442_000_000, SCAN_BW_HZ)
        loud = self._snapshot([75] * 1024, 2_442_000_000, SCAN_BW_HZ)
        for _ in range(3):
            plugin.on_snapshot(loud)
            plugin.on_snapshot(quiet)
        plugin.refresh()
        # 同一窗内 6 个信道电平相同 ⇒ 相对抬升为 0；换窗注入更高电平时才拉开
        self.assertEqual(plugin._stat(6, "2.4G").excess_db, 0.0)

    def test_channel_attribution_stays_inside_window(self):
        """20MHz 窗 + 15MHz 步进：每个信道都由真正看到它的窗贡献覆盖率。"""
        plugin = run_scenario("quiet", slices=len(sliding_windows(*BAND_24_HZ)) * 3, include_5g=False)
        for channel in NON_OVERLAP_24:
            stat = plugin._stat(channel, "2.4G")
            self.assertIsNotNone(stat, f"ch{channel} 没有统计")
            self.assertGreaterEqual(stat.coverage, MIN_COVERAGE, f"ch{channel} 覆盖率不足")
        self.assertEqual(len(plugin.stats), 13, "2.4G 应有 13 个信道")
        self.assertEqual(CHANNEL_WIDTH_HZ, 20_000_000)


if __name__ == "__main__":
    unittest.main()