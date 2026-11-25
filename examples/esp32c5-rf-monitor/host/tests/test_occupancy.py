"""``host/occupancy.py`` 自测（不需要窗口，也不需要真板）。

跑法（在 ``host/`` 下）：

    python tests/test_occupancy.py -v

覆盖四件事：
  1. 判据与固件同源（中间一半 bin、p50 − p25 > 15 码，含两条边界）；
  2. 帧归属（帧中心离信道 > MATCH_MHZ 就不计入任何一行）；
  3. EMA / 峰值 / 累计平均三个口径各自的行为；
  4. 快照结构与清零。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import occupancy  # noqa: E402 - 被测模块（纯 Python，不依赖 Qt）


def frame(frac: float, n: int = 512, floor: int = 10, peak: int = 40) -> tuple[int, ...]:
    """造一帧：中间一半里 ``frac`` 的 bin 抬到 ``peak``，其余是 ``floor``。

    ⚠️ 判据（p50 − p25 > 15）**不是**「有信号就亮」那么简单：
    抬升比例要到中间一半的 50%~75% 之间才亮（低于一半时 p50 还在底噪上，
    高于 75% 时 p25 也被抬起来）。90% 抬升会**回落成不忙** —— 这是固件
    同一个已知盲区（AGENTS.md 待修清单里的「宽而强」），这里如实钉住它。
    """
    lo = n // 4
    mid = n // 2
    k = int(mid * frac)
    bins = [floor] * n
    for i in range(lo, lo + k):
        bins[i] = peak
    return tuple(bins)


class FakeSnap:
    def __init__(self, mhz: float, bins) -> None:
        self.center_hz = int(mhz * 1_000_000)
        self.bins_db = tuple(bins)


class CriterionTest(unittest.TestCase):
    def test_pure_noise_is_idle(self) -> None:
        self.assertFalse(occupancy.is_busy([10] * 512))

    def test_below_half_elevated_is_idle(self) -> None:
        # p50 仍是底噪 → 看起来「有信号」但判据不亮（和固件一致）
        self.assertFalse(occupancy.is_busy(frame(0.30)))
        self.assertFalse(occupancy.is_busy(frame(0.50)))

    def test_above_half_elevated_is_busy(self) -> None:
        self.assertTrue(occupancy.is_busy(frame(0.60)))
        self.assertTrue(occupancy.is_busy(frame(0.75)))

    def test_documented_blind_spot_wide_signal(self) -> None:
        # 占满 90% 时 p25 也被抬高 → 回落成「不忙」。已知盲区，不许悄悄改掉。
        self.assertFalse(occupancy.is_busy(frame(0.90)))

    def test_too_few_bins_never_busy(self) -> None:
        self.assertFalse(occupancy.is_busy([0] * 4))

    def test_quantile_matches_firmware_1based(self) -> None:
        vals = list(range(1, 257))          # 1..256，升序
        self.assertEqual(occupancy.quantile(vals, 1, 4), 64)   # 第 64 个
        self.assertEqual(occupancy.quantile(vals, 1, 2), 128)


class ChannelMapTest(unittest.TestCase):
    def test_channel_norms(self) -> None:
        self.assertEqual(occupancy.CHANNELS[0], 2412)
        self.assertEqual(occupancy.CHANNELS[-1], 5825)
        self.assertEqual(len(occupancy.CHANNELS), 38)
        self.assertEqual(len(set(occupancy.CHANNELS)), 38)      # 不许重复

    def test_wifi_channel_names(self) -> None:
        self.assertEqual(occupancy.wifi_channel(2412), "1")
        self.assertEqual(occupancy.wifi_channel(2437), "6")
        self.assertEqual(occupancy.wifi_channel(5180), "36")
        self.assertEqual(occupancy.wifi_channel(5745), "149")
        self.assertEqual(occupancy.wifi_channel(3000), "")

    def test_band_split(self) -> None:
        self.assertEqual(occupancy.band_of(2412), "2.4G")
        self.assertEqual(occupancy.band_of(5200), "5G")
        self.assertEqual(occupancy.band_of(5765), "5.8G")


class FeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.occ = occupancy.ChannelOccupancy()

    def test_busy_channel_wins(self) -> None:
        for _ in range(30):
            self.occ.feed(FakeSnap(2412, frame(0.6)))
            self.occ.feed(FakeSnap(2437, frame(0.0)))
        rows = {r["mhz"]: r for r in self.occ.snapshot()["channels"]}
        self.assertTrue(rows[2412]["seen"])
        self.assertTrue(rows[2437]["seen"])
        self.assertEqual(rows[2412]["avg"], 100.0)
        self.assertEqual(rows[2437]["avg"], 0.0)
        self.assertGreater(rows[2412]["pct"], 90.0)
        self.assertLess(rows[2437]["pct"], 1.0)
        # 没扫过的信道必须是 seen=False，不能被当成 0%
        self.assertFalse(rows[2462]["seen"])
        self.assertIsNone(rows[2462]["age_s"])

    def test_frame_far_from_any_channel_is_ignored(self) -> None:
        self.occ.feed(FakeSnap(3000, frame(0.6)))
        snap = self.occ.snapshot()
        self.assertEqual(snap["fed"], 1)
        self.assertEqual(snap["matched"], 0)
        self.assertEqual(snap["unmatched"], 1)
        self.assertTrue(all(not r["seen"] for r in snap["channels"]))

    def test_ema_reaches_and_decays(self) -> None:
        # 打到满 → EMA 逼近 100；改成空 → EMA 按 α=1/8 衰减（不是一步归零）
        for _ in range(40):
            self.occ.feed(FakeSnap(5180, frame(0.6)))
        pct_busy = self._row(5180)["pct"]
        self.assertGreater(pct_busy, 95.0)
        for _ in range(8):
            self.occ.feed(FakeSnap(5180, frame(0.0)))
        pct_after = self._row(5180)["pct"]
        self.assertLess(pct_after, pct_busy * 0.5)
        self.assertGreater(pct_after, 0.0)
        # 峰值只记得高水位，不会被后来的安静拉下来
        self.assertGreaterEqual(self._row(5180)["max"], pct_busy)

    def test_burst_protocol_shows_peak_over_avg(self) -> None:
        # 20 帧里 2 帧发射 = 突发（BLE 广播的形态）：平均低、峰值明显更高
        for i in range(20):
            self.occ.feed(FakeSnap(2427, frame(0.6) if i % 10 == 0 else frame(0.0)))
        row = self._row(2427)
        self.assertEqual(row["frames"], 20)
        self.assertEqual(row["hits"], 2)
        self.assertEqual(row["avg"], 10.0)
        self.assertGreater(row["max"], row["avg"])

    def test_reset_clears_everything(self) -> None:
        for _ in range(10):
            self.occ.feed(FakeSnap(2412, frame(0.6)))
        self.occ.reset()
        snap = self.occ.snapshot()
        self.assertEqual(snap["fed"], 0)
        self.assertEqual(snap["matched"], 0)
        self.assertTrue(all(not r["seen"] for r in snap["channels"]))

    def test_snapshot_shape_and_csv(self) -> None:
        for _ in range(4):
            self.occ.feed(FakeSnap(2412, frame(0.6)))
        snap = self.occ.snapshot()
        self.assertTrue(snap["ok"])
        for key in ("channels", "fed", "matched", "unmatched", "match_mhz", "threshold", "ema_den"):
            self.assertIn(key, snap)
        self.assertEqual(len(snap["channels"]), len(occupancy.CHANNELS))
        rows = self.occ.csv_rows()
        self.assertEqual(len(rows), len(occupancy.CHANNELS))
        self.assertEqual(len(rows[0].split(",")), len(occupancy.ChannelOccupancy.csv_header().split(",")))

    def _row(self, mhz: int) -> dict:
        return {r["mhz"]: r for r in self.occ.snapshot()["channels"]}[mhz]


if __name__ == "__main__":
    unittest.main(verbosity=2)