# -*- coding: utf-8 -*-
"""设备自扫引擎（``engine=device``）的会话级单测；跑在假固件上，不碰真板。

跑法：``cd host && python tests/test_sweep_session.py -v``
"""
from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import paths  # noqa: E402 - 本地上位机模块

paths.ensure_on_path()

from session import HostConfig, HostSession  # noqa: E402

ORDER = ("wifi_check", "waterfall", "ble_risk", "guard_58g", "capture")


def _config(**kwargs) -> HostConfig:
    raw = {
        "mock": True,
        "scenario": "mixed",
        "engine": "device",
        "gain_mode": "MANUAL",
        "bins_out": 256,
        **kwargs,
    }
    import json

    return HostConfig.from_json(json.dumps(raw))


class EngineConfigTest(unittest.TestCase):
    def test_default_engine_is_host(self):
        config = HostConfig.from_json('{"mock": true}')
        self.assertEqual(config.engine, "host")

    def test_device_engine_accepted(self):
        self.assertEqual(_config().engine, "device")

    def test_unknown_engine_rejected(self):
        with self.assertRaises(ValueError):
            HostConfig.from_json('{"mock": true, "engine": "gpu"}')


class DeviceSweepSessionTest(unittest.TestCase):
    def setUp(self):
        self.session = HostSession(bins_out=256)
        self.info = self.session.open(_config())

    def tearDown(self):
        self.session.close()

    def test_engine_reported_as_device(self):
        self.assertEqual(self.info["engine"], "device")
        self.assertTrue(self.info["sweep_supported"])
        self.assertIn("设备自扫", self.info["engine_note"])

    def test_plan_covers_every_module_window(self):
        state = self.session.sweep_state()
        self.assertEqual(state["windows"], len(self.session.sweep.windows))
        # 计划里的频点必须覆盖所有模块计划里的频点（同频点合并）
        expected: set[int] = set()
        for plugin in self.session.registry.plugins:
            for request in plugin.scan_requests():
                expected.add(int(round(request.freq_hz / 1e6)))
        self.assertEqual({mhz for mhz, _d, _m in self.session.sweep.windows}, expected)

    def test_slices_produce_snapshots_from_device(self):
        seen: list[int] = []

        def on_snapshot(snap):
            seen.append(int(snap.center_hz))

        self.session.step(0.6, on_snapshot=on_snapshot)
        self.assertGreater(len(seen), 0, "设备自扫一片都没产出帧")
        state = self.session.sweep_state()
        self.assertGreater(state["frames"], 0)
        self.assertTrue(state["running"])

    def test_restrict_modules_narrows_plan(self):
        # 先跑一片，让计划生效（自扫是懒启动：第一片才开始扫）
        self.session.step(0.6, on_snapshot=lambda snap: None)
        # 只留全频段轮扫后，收窄到唯一模块 = 计划恒等：窗口集必须与它的计划完全一致
        self.session.restrict_modules(["waterfall"])
        windows = self.session.sweep.windows
        waterfall = [
            plugin for plugin in self.session.registry.plugins if plugin.id == "waterfall"
        ][0]
        expected = {
            int(round(request.freq_hz / 1e6)) for request in waterfall.scan_requests()
        }
        self.assertEqual({mhz for mhz, _d, _m in windows}, expected)
        self.assertTrue(self.session.sweep_state()["running"])  # 收窄之后设备仍在扫

    def test_close_stops_device(self):
        self.session.step(0.6, on_snapshot=lambda snap: None)
        sweep = self.session.sweep
        self.assertTrue(sweep.running)
        self.session.close()
        self.assertFalse(sweep.running)


class HostEngineStillWorksTest(unittest.TestCase):
    """回归：默认 host 引擎一点没变（不建自扫源、计划照旧）。"""

    def test_host_engine_has_no_sweep(self):
        session = HostSession(bins_out=256)
        try:
            info = session.open(_config(engine="host"))
            self.assertEqual(info["engine"], "host")
            self.assertIsNone(session.sweep)
            self.assertEqual(session.sweep_state()["engine"], "host")
            session.restrict_modules(["wifi_check"])  # 空操作，不该炸
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()