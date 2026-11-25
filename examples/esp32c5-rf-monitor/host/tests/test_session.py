"""上位机逻辑层自测（不需要开窗口，也不需要真板）。

跑法（在本目录 ``host/`` 下）：

    python tests/test_session.py -v          # 直接跑文件（不要用 -m unittest discover：
                                             # host/tests 会遮蔽分析层的 tests 包）
"""
from __future__ import annotations

import json
import base64
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import paths  # noqa: E402 - 本地上位机模块

paths.ensure_on_path()  # 让 session.py 能 import core/tests

from session import LANGS, MESSAGES, HostConfig, HostSession, downsample_bins, list_ports, to_json, tr  # noqa: E402


class SourceDirTests(unittest.TestCase):
    """上位机独立目录后，靠 paths.py 找到 Python 分析层（core/ modes/）。"""

    def test_find_source_has_registry(self):
        found = paths.find_source()
        self.assertTrue((found / "core" / "registry.py").is_file())
        self.assertTrue((found / "modes").is_dir())

    def test_env_override_wins(self):
        old = os.environ.get(paths.ENV_VAR)
        os.environ[paths.ENV_VAR] = str(paths.HOST_DIR)
        try:
            self.assertEqual(paths.candidates()[0], paths.HOST_DIR)
            with self.assertRaises(RuntimeError):  # 该目录不是分析层 → 明确报错
                paths.find_source()
        finally:
            if old is None:
                os.environ.pop(paths.ENV_VAR, None)
            else:
                os.environ[paths.ENV_VAR] = old


class I18nTests(unittest.TestCase):
    """界面文案：中文必须有，英文可缺但 key 必须一致（缺了退回中文，绝不抛）。"""

    def test_langs(self):
        self.assertEqual(LANGS, ("zh", "en"))
        self.assertTrue(all(k in MESSAGES for k in LANGS))

    def test_en_covers_zh_keys(self):
        missing = sorted(set(MESSAGES["zh"]) - set(MESSAGES["en"]))
        self.assertEqual(missing, [], f"英文缺这些键：{missing}")

    def test_tr_and_fallback(self):
        self.assertIn("已停止", tr("zh", "stopped"))
        self.assertIn("Stopped", tr("en", "stopped"))
        self.assertEqual(tr("fr", "stopped"), tr("zh", "stopped"))  # 未知语言退回中文
        self.assertEqual(tr("zh", "no_such_key"), "no_such_key")     # 未知 key 原样返回
        self.assertIn("ch1", tr("zh", "started", what="ch1"))


class ConfigTests(unittest.TestCase):
    def test_from_json_defaults(self):
        cfg = HostConfig.from_json("{}")
        self.assertEqual(cfg.port, "COM56")
        self.assertEqual(cfg.baudrate, 2_000_000)
        self.assertIsNone(cfg.gain_index)
        self.assertFalse(cfg.mock)

    def test_from_json_overrides(self):
        cfg = HostConfig.from_json(
            to_json({"port": "COM56", "mock": True, "scenario": "quiet", "bins_out": 128, "baudrate": 2_000_000})
        )
        self.assertTrue(cfg.mock)
        self.assertEqual(cfg.scenario, "quiet")
        self.assertEqual(cfg.bins_out, 128)

    def test_lang_default_and_override(self):
        self.assertEqual(HostConfig.from_json("{}").lang, "zh")
        self.assertEqual(HostConfig.from_json(to_json({"lang": "EN"})).lang, "en")

    def test_unknown_lang_rejected(self):
        with self.assertRaises(ValueError):
            HostConfig.from_json(to_json({"lang": "fr"}))

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            HostConfig.from_json(to_json({"port": "COM56", "nonsense": 1}))

    def test_unknown_scenario_rejected(self):
        with self.assertRaises(ValueError):
            HostConfig.from_json(to_json({"scenario": "no_such_scenario"}))

    def test_mock_drops_gain(self):
        cfg = HostConfig.from_json(to_json({"mock": True, "gain_index": 60}))
        cfg.validate()
        self.assertIsNone(cfg.gain_index)

    def test_empty_port_rejected_for_real_board(self):
        cfg = HostConfig.from_json(to_json({"mock": False, "port": ""}))
        with self.assertRaises(ValueError):
            cfg.validate()


class DownsampleTests(unittest.TestCase):
    def test_keeps_peak_of_each_bucket(self):
        bins = [0] * 8 + [200, 0, 0, 0] + [0] * 4
        out = downsample_bins(bins, 4)
        self.assertEqual(len(out), 4)
        self.assertEqual(max(out), 200)  # 窄带峰不能被平均掉

    def test_small_input_not_padded(self):
        out = downsample_bins([1, 2, 3], 8)
        self.assertEqual(len(out), 3)
        self.assertEqual(out, [1, 2, 3])

    def test_empty_input(self):
        self.assertEqual(downsample_bins([], 256), [])

    def test_never_exceeds_bin_count(self):
        bins = list(range(256))
        self.assertEqual(len(downsample_bins(bins, 1000)), 256)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.session = HostSession(bins_out=64)

    def tearDown(self):
        self.session.close()

    def test_open_mock_and_describe(self):
        info = self.session.open(HostConfig.from_json(to_json({"mock": True, "scenario": "wifi_busy"})))
        self.assertEqual(info["kind"], "info")
        self.assertTrue(info["mock"])
        self.assertEqual(info["identity"].split()[0], "C5SDR")
        self.assertTrue(info["profiles"])
        self.assertTrue(info["plugins"], "modes/ 里应当至少有一个功能模块")

    def test_step_produces_spectrum_json(self):
        self.session.open(HostConfig.from_json(to_json({"mock": True, "scenario": "wifi_busy"})))
        seen = []
        self.session.step(0.45, on_snapshot=lambda snap: seen.append(self.session.spectrum(snap, "wifi_check")))
        self.assertTrue(seen, "假固件应当产出快照")
        frame = seen[0]
        self.assertEqual(len(frame["bins"]), 64)
        self.assertEqual(frame["bins"].__class__, list)
        self.assertGreater(frame["freq_hi_hz"], frame["freq_lo_hz"])
        self.assertEqual(frame["center_hz"], (frame["freq_lo_hz"] + frame["freq_hi_hz"]) // 2)
        self.assertTrue(all(0 <= v <= 255 for v in frame["bins"]))
        json.dumps(frame)  # 必须能序列化成 JSON 字符串发给前端

    def test_tables_carry_waterfall_payload(self):
        """只留全频段轮扫后：表格里必须有 waterfall，且带满 44 窗的记忆热力。"""
        self.session.open(HostConfig.from_json(to_json({"mock": True, "scenario": "wifi_busy"})))
        self.session.step(2.0)
        tables = self.session.tables()
        self.assertEqual(tables["kind"], "tables")
        items = [p for p in tables["plugins"] if p["id"] == "waterfall"]
        self.assertTrue(items, "应当有 waterfall 模块")
        payload = items[0]["payload"]
        self.assertIsNotNone(payload)
        self.assertEqual(payload["kind"], "waterfall")
        self.assertEqual(payload["windows"], 44, "全频段滚动扫描共 44 个窗")
        self.assertEqual(payload["bw_mhz"], 20.0)
        self.assertEqual(payload["step_mhz"], 15.0)
        self.assertTrue(payload["bands"], "三个频段都要有数据块")
        self.assertGreaterEqual(payload["rows"], 1)
        self.assertEqual(payload["cols"], 180, "WEB_COLS = 180")
        for band in payload["bands"]:
            cells = base64.b64decode(band["cells"])
            self.assertEqual(
                len(cells), payload["rows"] * payload["cols"], band["id"])
        self.assertLessEqual(payload["code_lo"], payload["code_hi"])
        # 记忆热力：按频段分块（现口径，2026-11 起）——bands[].cells =
        # base64(rows*cols)，一行 = 一轮完整轮扫、一格一字节；
        # 旧单条 heat(44 字节) / heat_sat / heat_peak_codes 结构已删，
        # 颜色范围改用 code_lo/code_hi 由前端拉伸。

    def test_tables_gate_to_one_module(self):
        """页面门控：只给一个模块名时**只算它**。

        为什么值得单测：``produce()`` + ``web_payload()`` 是每秒真算一遍的，
        模块多了就是 N 倍白工。门控失效不会报错，只会悄悄变慢，所以必须自证。
        """
        self.session.open(HostConfig.from_json(to_json({"mock": True, "scenario": "wifi_busy"})))
        self.session.step(2.0)
        gated = self.session.tables(["waterfall"])
        self.assertEqual(gated["modules"], ["waterfall"], "只算了 waterfall 就该只报 waterfall")
        self.assertEqual([p["id"] for p in gated["plugins"]], ["waterfall"])
        # 全量对照：不给 modules 时全部模块都在
        full = self.session.tables()
        self.assertIsNone(full["modules"], "不给 modules 表示全量，modules 字段应为 None")
        self.assertEqual(len(full["plugins"]), len(self.session.registry.plugins))

    def test_tables_gate_skips_unknown_ids(self):
        """页面门控按 id 过滤：给了不存在的模块名不能当成「全量」也不能炸。"""
        self.session.open(HostConfig.from_json(to_json({"mock": True, "scenario": "quiet"})))
        self.session.step(1.0)
        empty = self.session.tables(["not_a_module"])
        self.assertEqual(empty["plugins"], [])
        self.assertEqual(empty["modules"], [])

    def test_describe_carries_scan_plans(self):
        """扫频进度条靠这个画频段地图：必须是 [[中心频点, 带宽], ...] 的扫描顺序表。"""
        info = self.session.open(HostConfig.from_json(to_json({"mock": True, "scenario": "wifi_busy"})))
        plans = info.get("plans") or {}
        self.assertIn("waterfall", plans, "全功能并行常开后应当有 waterfall 的扫描计划")
        plan = plans["waterfall"]
        self.assertEqual(len(plan), 44, "2.4G 6 窗 + 5G 低 13 窗 + 5G 高 25 窗")
        self.assertEqual(plan[0], [2_410_000_000, 20_000_000])
        self.assertEqual(plan[-1], [5_840_000_000, 20_000_000])
        # 扫描顺序 = 频点升序（2.4G 先、5G 后），前端据此判「到终点回卷」
        centers = [item[0] for item in plan]
        self.assertEqual(centers, sorted(centers))
        self.assertEqual(len(set(centers)), 44)
        for entry in plan:
            self.assertEqual(len(entry), 2)
            self.assertEqual(entry[1], 20_000_000)

    def test_render_text_matches_cli_shape(self):
        self.session.open(HostConfig.from_json(to_json({"mock": True, "scenario": "quiet"})))
        self.session.step(1.2)
        text = self.session.render_text()
        self.assertIn("周期性频谱瀑布图", text)
        self.assertIn("44 个窗", text)

    def test_list_ports_shape(self):
        ports = list_ports()
        self.assertIsInstance(ports, list)
        for item in ports:
            self.assertIn("device", item)


class RelinkTests(unittest.TestCase):
    """串口被判失效后 session.relink() 能把采集救回来（现场：跑一会儿提示断开串口）。"""

    def setUp(self):
        self.session = HostSession(bins_out=64)
        self.session.open(HostConfig.from_json(to_json({"mock": True, "scenario": "wifi_busy"})))

    def tearDown(self):
        self.session.close()

    def test_scan_heals_itself_when_link_fails(self):
        """链路被判失效后，下一次 scan() 自己重同步 —— 用户不用做什么。"""
        before = self.session.step(0.5).snapshots
        self.assertGreater(before, 0)
        self.session.link.failed = True  # 模拟真板帧错位
        after = self.session.step(0.5)
        self.assertEqual(after.source_errors, 0, "自愈成功就不该记成采集错误")
        self.assertGreater(after.snapshots, 0, "自愈之后必须继续出帧")
        self.assertFalse(self.session.link.failed)
        self.assertEqual(self.session.link.reopen_count, 1, "自愈应当留下重开次数")

    def test_relink_recovers_when_auto_heal_is_blocked(self):
        """自动重同步被冷却/拔线挡住时，session.relink() 是最后一道手动恢复。"""
        link = self.session.link
        original = link.reopen
        link.reopen = lambda *a, **k: False  # 冷却中：自动重同步暂时不生效
        link.failed = True
        broken = self.session.step(0.5)
        self.assertGreater(broken.source_errors, 0, "失效链路应当被如实记成错误")
        link.reopen = original
        healed = self.session.relink()
        self.assertTrue(healed["ok"], healed)
        self.assertFalse(link.failed, "重同步后失效标记必须清掉")
        self.assertIn("已重新同步", healed["message"])
        after = self.session.step(0.5)
        self.assertEqual(after.source_errors, broken.source_errors, "恢复后不该再有新错误")
        self.assertGreater(after.snapshots, 0, "恢复后必须继续出帧")

    def test_relink_without_session_is_reported(self):
        self.session.close()
        result = self.session.relink()
        self.assertFalse(result["ok"])
        self.assertTrue(result["message"], "链路已关闭时也要给一句能显示的话")

    def test_relink_messages_are_bilingual(self):
        for key in ("relink_ok", "relink_failed", "relink_closed"):
            self.assertIn(key, MESSAGES["zh"])
            self.assertIn(key, MESSAGES["en"])
            self.assertFalse(
                any(ord(ch) > 127 for ch in MESSAGES["en"][key]),
                f"{key} 的英文文案不能带非 ASCII",
            )


if __name__ == "__main__":
    unittest.main()