"""``app/audit.py`` 的纯函数测试：只测「怎么汇总、怎么下结论」，不跑调度。"""
import unittest

from app.audit import BOUNDARIES, _sources_of, collect_sources, verdict


class FakePlugin:
    def __init__(self, pid, payload):
        self.id = pid
        self._payload = payload

    def web_payload(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeRegistry:
    def __init__(self, plugins):
        self.plugins = plugins


class SourcesOfTest(unittest.TestCase):
    def test_guard_alerts_become_rows(self):
        rows = _sources_of(
            "guard_58g",
            {
                "alerts": [
                    {
                        "id": "G58-001",
                        "kind": "持续窄带",
                        "center_mhz": 5731.25,
                        "bw_mhz": 6.5,
                        "duration_s": 9.1,
                        "peak_excess_db": 24.0,
                        "fill": 0.83,
                    }
                ],
                "windows": [],
            },
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "5.8G 守卫")
        self.assertEqual(rows[0]["level"], "需复核")
        self.assertIn("抬升 24 码", rows[0]["detail"])

    def test_guard_dead_window_reported_as_unknown(self):
        rows = _sources_of(
            "guard_58g",
            {"alerts": [], "windows": [{"center_hz": 5712_500_000, "floor_code": 40, "frames": 9, "coverage": 0.3}]},
        )
        self.assertEqual([r["kind"] for r in rows], ["该窗无覆盖"])
        self.assertEqual(rows[0]["level"], "未知")

    def test_wifi_only_heavy_channels(self):
        rows = _sources_of(
            "wifi_check",
            {
                "channels": [
                    {"band": "2.4G", "channel": 1, "level": "重度", "excess_db": 59.0, "occupancy": 0.88},
                    {"band": "2.4G", "channel": 2, "level": "轻度", "excess_db": 3.0, "occupancy": 0.1},
                ]
            },
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "2.4G ch1")
        self.assertIsNone(rows[0]["freq_mhz"], "没下发频点时必须留空，不许编一个数字")

    def test_capture_rows_are_suspicious_with_file(self):
        rows = _sources_of(
            "capture",
            {
                "captures": [
                    {
                        "id": "CAP-002",
                        "freq_mhz": 2427.0,
                        "path": "x.iq16",
                        "reason": "带宽 3.0 MHz、占空 55%",
                        "candidate": {"width_mhz": 3.0},
                    }
                ]
            },
        )
        self.assertEqual(rows[0]["level"], "可疑")
        self.assertEqual(rows[0]["kind"], "CAP-002")
        self.assertEqual(rows[0]["file"], "x.iq16")

    def test_ble_low_score_is_low_level(self):
        rows = _sources_of(
            "ble_risk",
            {"risk_type": "无明显风险", "score": 5.0, "level": "空闲", "wideband": 0.0, "ref_db": 18.0, "frames": 30},
        )
        self.assertEqual(rows[0]["level"], "低")

    def test_ble_without_frames_is_silent(self):
        self.assertEqual(_sources_of("ble_risk", {"risk_type": "无数据", "frames": 0}), [])


class CollectTest(unittest.TestCase):
    def test_broken_module_does_not_break_audit(self):
        registry = FakeRegistry([FakePlugin("ble_risk", RuntimeError("boom")), FakePlugin("capture", {"captures": []})])
        rows = collect_sources(registry)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "模块出错")
        self.assertIn("boom", rows[0]["detail"])

    def test_plugins_without_web_payload_skipped(self):
        class Bare:
            id = "probe"

        self.assertEqual(collect_sources(FakeRegistry([Bare()])), [])


class VerdictTest(unittest.TestCase):
    def test_suspicious_wins(self):
        headline, why = verdict([{"level": "可疑", "freq_mhz": 2402.0}], [{"id": "CAP-001"}])
        self.assertIn("可疑", headline)
        self.assertIn("不是「确认是摄像头」", why)

    def test_review_only(self):
        headline, why = verdict([{"level": "需复核"}], [])
        self.assertIn("复核", headline)
        self.assertIn("窄带持续", why)

    def test_clean_says_it_is_not_proof(self):
        headline, why = verdict([], [])
        self.assertIn("未抓到", headline)
        self.assertIn("不等于房间里没有摄像头", why)

    def test_boundaries_mention_relative_codes(self):
        self.assertTrue(any("dBm" in line for line in BOUNDARIES))
        self.assertTrue(any("分时" in line for line in BOUNDARIES))


if __name__ == "__main__":
    unittest.main()