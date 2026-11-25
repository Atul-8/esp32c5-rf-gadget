"""``core/classifier.py`` 的自测（任务 5）。

跑法（仓库根 ``examples/esp32c5-rf-monitor`` 下）：``python -m unittest tests.test_classifier -v``

核心口径：**分不出去就落 ``unknown``，不许冒认**。``unknown`` 正是抓拍的触发条件，
所以「宁可说分不清」在这里不是缺点而是设计要求。
"""
from __future__ import annotations

import unittest

from core.classifier import (
    AMBIGUOUS_MIN_BW_HZ,
    AMBIGUOUS_MIN_DUTY,
    BLE_BW_RANGE_HZ,
    CW_MAX_BW_HZ,
    HOP_MAX_DUTY,
    KNOWN_LABELS,
    VIDEO_MIN_DURATION_S,
    WIFI_MIN_BW_HZ,
    WIFI_MIN_DUTY,
    Classification,
    SignalFeatures,
    classify,
)


def feats(bw_hz: float, duty: float, duration_s: float = 10.0, peak: float = 25.0, hop=None):
    return SignalFeatures(
        bw_hz=bw_hz,
        duration_s=duration_s,
        duty_cycle=duty,
        peak_over_noise_db=peak,
        hop_rate_hz=hop,
    )


class KnownLabelTest(unittest.TestCase):
    def test_wifi(self):
        got = classify(feats(12e6, 0.85))
        self.assertEqual(got.label, "wifi")
        self.assertGreater(got.confidence, 0.4)
        self.assertIn("WiFi", got.reason)

    def test_ble(self):
        got = classify(feats(1.5e6, 0.30))
        self.assertEqual(got.label, "ble")
        self.assertIn("BLE", got.reason)

    def test_analog_video(self):
        got = classify(feats(6e6, 0.90, duration_s=30.0))
        self.assertEqual(got.label, "analog_video")
        self.assertIn("图传", got.reason)

    def test_video_needs_duration(self):
        # 6 MHz + 高占空比，但只持续 1 s → 还不够长，落 unknown 而不是硬贴图传
        got = classify(feats(6e6, 0.90, duration_s=VIDEO_MIN_DURATION_S - 0.5))
        self.assertEqual(got.label, "unknown")


class UnknownTest(unittest.TestCase):
    def test_cw_is_unknown(self):
        got = classify(feats(120e3, 0.95))
        self.assertEqual(got.label, "unknown")
        self.assertIn("窄带", got.reason)

    def test_wideband_continuous_is_microwave_but_flagged(self):
        # 铺满整窗的连续抬升 → 只敢给大类 microwave，且必须自己承认「分不清」
        got = classify(feats(AMBIGUOUS_MIN_BW_HZ + 3e6, AMBIGUOUS_MIN_DUTY + 0.1))
        self.assertEqual(got.label, "microwave")
        self.assertLessEqual(got.confidence, 0.5)
        self.assertEqual(got.notes.get("ambiguous"), "wideband_continuous")
        self.assertIn("分不清", got.reason)

    def test_wideband_is_not_unknown(self):
        # 关键回归：宽带连续不能是 unknown，否则满负荷 WiFi 会被当成可疑信号抓拍
        for bw in (15e6, 18e6, 20e6, 30e6):
            for duty in (0.75, 0.9, 1.0):
                self.assertNotEqual(classify(feats(bw, duty)).label, "unknown")

    def test_hop_hint_goes_unknown(self):
        got = classify(feats(3e6, 0.4, hop=2.5))
        self.assertEqual(got.label, "unknown")
        self.assertIn("跳频", got.reason)

    def test_intermittent_goes_unknown(self):
        got = classify(feats(3e6, HOP_MAX_DUTY - 0.1))
        self.assertEqual(got.label, "unknown")
        self.assertIn("间歇", got.reason)


class BoundaryTest(unittest.TestCase):
    def test_band_edges(self):
        # 门槛两侧各测一格，避免「改判据忘了改测试」
        self.assertEqual(classify(feats(WIFI_MIN_BW_HZ, WIFI_MIN_DUTY, duration_s=1.0)).label, "wifi")
        self.assertEqual(
            classify(feats(WIFI_MIN_BW_HZ - 2e6, WIFI_MIN_DUTY, duration_s=1.0)).label, "unknown"
        )
        self.assertEqual(classify(feats(BLE_BW_RANGE_HZ[0], 0.40)).label, "ble")
        self.assertEqual(classify(feats(BLE_BW_RANGE_HZ[0], 0.80)).label, "unknown")
        self.assertEqual(classify(feats(CW_MAX_BW_HZ, 0.95)).label, "unknown")

    def test_duty_is_clamped(self):
        self.assertEqual(classify(feats(12e6, 5.0)).label, "wifi")
        self.assertEqual(classify(feats(12e6, -3.0)).label, "unknown")  # 越界占空比不许炸

    def test_negative_bandwidth_rejected(self):
        with self.assertRaises(ValueError):
            classify(feats(-1.0, 0.5))

    def test_labels_are_known(self):
        for args in [(12e6, 0.9), (1.5e6, 0.3), (0.2e6, 0.9), (18e6, 0.9)]:
            got = classify(feats(*args))
            self.assertIn(got.label, KNOWN_LABELS)

    def test_confidence_in_range(self):
        for bw in (0.1e6, 0.6e6, 1.5e6, 6e6, 20e6):
            for duty in (0.0, 0.3, 0.6, 0.95):
                got = classify(feats(bw, duty))
                self.assertGreaterEqual(got.confidence, 0.0)
                self.assertLessEqual(got.confidence, 1.0)

    def test_classification_validates(self):
        with self.assertRaises(ValueError):
            Classification("not_a_label", 0.5, "x")
        with self.assertRaises(ValueError):
            Classification("unknown", 1.5, "x")

    def test_features_as_dict_roundtrip(self):
        data = feats(2e6, 0.4, peak=30.0, hop=None).as_dict()
        self.assertEqual(data["bw_hz"], 2e6)
        self.assertIsNone(data["hop_rate_hz"])
        self.assertEqual(SignalFeatures(**data).bw_hz, 2e6)


if __name__ == "__main__":
    unittest.main()