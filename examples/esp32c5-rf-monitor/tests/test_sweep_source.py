# -*- coding: utf-8 -*-
"""设备端自扫源（``core/sweep_source.py``）的单测；跑在假固件上，不碰真板。

跑法：``cd examples/esp32c5-rf-monitor && python -m unittest tests.test_sweep_source -v``
"""
from __future__ import annotations

import unittest

from core.registry import ScanRequest
from core.serial_link import EspSdrLink
from core.sweep_source import SweepLinkFacade, SweepSource, sweep_key
from tests.fake_firmware import FakeFirmware

WINDOWS_HZ = [2_410_000_000, 2_435_000_000, 2_460_000_000]


def _link(**kwargs) -> EspSdrLink:
    link = EspSdrLink(
        "MOCK",
        2_000_000,
        factory=lambda port, baud, timeout=5.0: FakeFirmware(port, baud, timeout, **kwargs),
    )
    link.open()
    return link


class SweepKeyTest(unittest.TestCase):
    def test_key_is_mhz(self):
        self.assertEqual(sweep_key(2_410_000_000), 2410)
        self.assertEqual(sweep_key(2_410_600_000), 2411)  # 四舍五入到整 MHz
        self.assertEqual(sweep_key(5710_000_000), 5710)


class SweepSourceTest(unittest.TestCase):
    def setUp(self):
        self.link = _link()
        self.sweep = SweepSource(
            self.link,
            [(sweep_key(hz), 100, index) for index, hz in enumerate(WINDOWS_HZ)],
            bins=512,
            rate_code=2,
        )

    def tearDown(self):
        self.link.close()

    def test_caps_advertises_fsm(self):
        self.assertTrue(self.link.sweep_supported)

    def test_plan_roundtrip(self):
        count = self.link.set_sweep_plan([(2410, 100, 0), (2435, 100, 1)])
        self.assertEqual(count, 2)
        # 假固件把计划存在自己的状态里：回读一致才说明 W 行没读错位
        firmware = self.link._port
        self.assertEqual([(m, d) for m, d, _ in firmware.fsm_plan], [(2410, 100), (2435, 100)])

    def test_scan_returns_requested_window(self):
        snap = next(iter(self.sweep.scan(ScanRequest(WINDOWS_HZ[1], 20_000_000, 100, 512))))
        self.assertEqual(sweep_key(snap.center_hz), 2435)
        self.assertEqual(len(snap.bins_db), 512)
        self.assertEqual(snap.meta.get("engine"), "device")

    def test_frames_of_other_windows_are_cached(self):
        # 先要最后一个窗：设备从第一个窗开始转，中间的帧必须留在缓存里而不是丢掉
        self.sweep.scan(ScanRequest(WINDOWS_HZ[2], 20_000_000, 100, 512)).__next__()
        self.assertGreaterEqual(len(self.sweep._cache), 1)
        cached = sorted(self.sweep._cache)
        self.assertIn(2410, cached)
        # 缓存里的帧能直接满足后续请求（不必再读串口）
        snap = next(iter(self.sweep.scan(ScanRequest(WINDOWS_HZ[0], 20_000_000, 100, 512))))
        self.assertEqual(sweep_key(snap.center_hz), 2410)

    def test_off_plan_request_falls_back_to_window_driven(self):
        snap = next(iter(self.sweep.scan(ScanRequest(2_442_000_000, 20_000_000, 100, 512))))
        self.assertEqual(sweep_key(snap.center_hz), 2442)
        self.assertEqual(self.sweep.fallbacks, 1)

    def test_pause_around_stops_and_restarts(self):
        self.sweep.start()
        self.assertTrue(self.sweep.running)
        seen: list[str] = []

        def action():
            seen.append(self.link._port.fsm_running and "running" or "stopped")
            return "done"

        self.assertEqual(self.sweep.pause_around(action), "done")
        self.assertEqual(seen, ["stopped"])  # 干活时设备必须已经停稳
        self.assertTrue(self.sweep.running)  # 干完接上
        self.assertEqual(self.sweep.started, 2)
        self.sweep.stop()
        self.assertFalse(self.sweep.running)
        self.assertFalse(self.link._port.fsm_running)

    def test_facade_routes_scan_and_commands(self):
        facade = SweepLinkFacade(self.link, self.sweep)
        self.sweep.start()
        self.assertEqual(facade.command("DC?"), "DC 1")  # 命令期间自扫让路
        self.assertTrue(self.sweep.running)  # 命令后恢复
        snap = next(iter(facade.scan(ScanRequest(WINDOWS_HZ[0], 20_000_000, 100, 512))))
        self.assertEqual(sweep_key(snap.center_hz), 2410)

    def test_state_reports_counters(self):
        self.sweep.start()
        state = self.sweep.state()
        self.assertTrue(state["running"])
        self.assertEqual(state["windows"], 3)
        self.assertEqual(state["started"], 1)


if __name__ == "__main__":
    unittest.main()