"""host/gain.py 的测试：手动档位 / 串口落地时机 / AUTO 闭环。

用 `unittest` + 标准库（本机没有 pytest 也没有 numpy）。

关键几条不是"跑通就行"，而是踩过的坑：

* **增益命令只能在两次扫描之间发** — 扫描中途插一条 `GAIN` 会把串口读缓冲打乱
  （实测 `SPEC 启动回复异常`）。所以 `GainAwareSource.scan()` 的顺序被单独钉住。
* **AUTO 不能拿几十帧的抖动当结论** — 先攒够 `AUTO_WARMUP` 帧、两次调整之间留
  `AUTO_COOLDOWN_S`，否则是在拿噪声追自己。
* **目标附近要有死区** — 落在 `target ± AUTO_TOLERANCE` 就不动，不然会来回抖一档。
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

paths.ensure_on_path()  # 让 core/ modes/ 可导入

from gain import (  # noqa: E402
    AUTO_CONFIRM,
    AUTO_STEP,
    AUTO_TARGET,
    AUTO_TOLERANCE,
    AUTO_WARMUP,
    MAX_INDEX,
    MIN_INDEX,
    GainAwareSource,
    GainControl,
    clamp_index,
    parse_gain_line,
    percentile,
)


class FakeSnap:
    """只带 gain 闭环会用到的三样东西：时间戳、bins、meta。"""

    def __init__(self, ts: float, floor: float, code: int | None = 60, bins: int = 64) -> None:
        self.ts = ts
        # 前半段平底、后半段抬高：p10 落在平底上，等价于"底噪参考码 = floor"
        self.bins_db = [floor] * (bins // 2) + [floor + 40.0] * (bins // 2)
        self.meta = {"gain_code": code} if code is not None else {}


class FakeLink:
    """记下每次 set_gain 的调用，用来验证落地时机与顺序。"""

    def __init__(self, snaps: int = 3) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.scans = 0
        self.snaps = snaps

    def set_gain(self, mode: str = "HARDWARE", index: int | None = None) -> None:
        self.calls.append((mode, index))

    def scan(self, request: object):  # noqa: ARG002 - 签名对齐真实链路
        self.scans += 1
        for i in range(self.snaps):
            yield FakeSnap(ts=float(i + 1), floor=20.0)


class HelperTest(unittest.TestCase):
    def test_percentile_basic(self):
        data = [0, 10, 20, 30, 40]
        self.assertAlmostEqual(percentile(data, 0), 0.0)
        self.assertAlmostEqual(percentile(data, 50), 20.0)
        self.assertAlmostEqual(percentile(data, 100), 40.0)
        # 线性插值：10 与 20 之间取中点
        self.assertAlmostEqual(percentile(data, 25), 10.0)

    def test_percentile_empty_and_nan(self):
        self.assertEqual(percentile([], 10), 0.0)
        # NaN 自己不等于自己，正好被过滤掉
        self.assertAlmostEqual(percentile([float("nan"), 5.0, 15.0], 0), 5.0)

    def test_clamp(self):
        self.assertEqual(clamp_index(-5), MIN_INDEX)
        self.assertEqual(clamp_index(999), MAX_INDEX)
        self.assertEqual(clamp_index(42), 42)

    def test_parse_gain_line(self):
        got = parse_gain_line("GAIN MANUAL 72 0 90 1")
        self.assertTrue(got["ok"])
        self.assertEqual(got["mode"], "MANUAL")
        self.assertEqual(got["index"], 72)
        self.assertEqual((got["min_index"], got["max_index"], got["step"]), (0, 90, 1))

        agc = parse_gain_line("GAIN HARDWARE -1")
        self.assertTrue(agc["ok"])
        self.assertEqual(agc["index"], -1)

        self.assertFalse(parse_gain_line("SPEC 1 2402").get("ok", False))
        self.assertFalse(parse_gain_line("").get("ok", False))


class RequestTest(unittest.TestCase):
    def test_step_from_hardware_switches_to_manual(self):
        ctl = GainControl(mode="HARDWARE", index=60)
        state = ctl.request_step(1)
        self.assertEqual(state["mode"], "MANUAL")
        self.assertEqual(state["index"], 61)
        self.assertEqual(ctl.take_pending(), [("MANUAL", 61)])

    def test_step_clamped_at_bounds(self):
        ctl = GainControl(index=MAX_INDEX)
        self.assertEqual(ctl.request_step(5)["index"], MAX_INDEX)
        ctl2 = GainControl(index=MIN_INDEX)
        self.assertEqual(ctl2.request_step(-5)["index"], MIN_INDEX)

    def test_mode_switch_queues_firmware_command(self):
        ctl = GainControl(index=60)
        ctl.request_mode("hardware")  # 大小写不敏感
        self.assertEqual(ctl.take_pending(), [("HARDWARE", None)])
        ctl.request_mode("MANUAL")
        self.assertEqual(ctl.take_pending(), [("MANUAL", 60)])

    def test_unknown_mode_rejected(self):
        ctl = GainControl(mode="MANUAL")
        state = ctl.request_mode("TURBO")
        self.assertFalse(state["ok"])
        self.assertIn("未知增益模式", state["note"])
        self.assertEqual(ctl.mode, "MANUAL")  # 不因为传错就把状态改坏
        self.assertEqual(ctl.take_pending(), [])

    def test_target_switch_forces_auto(self):
        ctl = GainControl(mode="MANUAL")
        state = ctl.request_target(9.0)
        self.assertEqual(state["mode"], "AUTO")
        self.assertAlmostEqual(state["target"], 9.0)

    def test_limits_from_firmware(self):
        # 实板 GAIN? 报 0..81：AUTO 不能把档位推到固件不认的地方
        ctl = GainControl(index=60)
        ctl.set_limits(parse_gain_line("GAIN HARDWARE -1 0 81 0"))
        self.assertEqual((ctl.min_index, ctl.max_index), (0, 81))
        self.assertEqual(ctl.request_index(200)["index"], 81)
        self.assertEqual(ctl.state()["max_index"], 81)

    def test_set_limits_ignores_garbage(self):
        ctl = GainControl(index=60)
        before = (ctl.min_index, ctl.max_index)
        ctl.set_limits(None)
        ctl.set_limits({"min_index": 5, "max_index": 5})  # 区间非法：不采纳
        ctl.set_limits({"min_index": "x", "max_index": 9})
        self.assertEqual((ctl.min_index, ctl.max_index), before)

    def test_state_is_json_serialisable(self):
        ctl = GainControl(mode="AUTO", index=30)
        text = json.dumps(ctl.state())  # 桥只传 JSON 字符串
        self.assertIn('"kind": "gain"', text)


class AutoLoopTest(unittest.TestCase):
    """AUTO 闭环：一轮（一轮 = 多帧 + 一次 end_scan）只决策一次。"""

    def _auto(self, **kw) -> GainControl:
        ctl = GainControl(mode="AUTO", index=60, **kw)
        ctl.take_pending()  # 丢掉构造时挂的那条
        return ctl

    def _scan(self, ctl: GainControl, floors, frames: int = 5) -> None:
        """模拟一轮：喂若干帧（floor 可以是列表，模拟逐帧抖动），然后结算。"""
        values = floors if isinstance(floors, (list, tuple)) else [floors] * frames
        for i, floor in enumerate(values):
            ctl.observe(FakeSnap(ts=float(i + 1) * 0.1, floor=float(floor)))
        ctl.end_scan()

    def _settle(self, ctl: GainControl, floor, scans: int) -> None:
        for _ in range(scans):
            self._scan(ctl, floor)

    def test_warmup_before_first_adjust(self):
        ctl = self._auto()
        self._settle(ctl, 30.0, AUTO_WARMUP)  # 热身轮：一档都不许动
        self.assertEqual(ctl.index, 60)
        # 热身完还要连续 AUTO_CONFIRM 轮同方向越界才动手（单轮尖峰不认账）
        self._settle(ctl, 30.0, 1)
        self.assertEqual(ctl.index, 60)
        self._settle(ctl, 30.0, 1)
        self.assertEqual(ctl.index, 60 - AUTO_STEP)

    def test_high_floor_lowers_index(self):
        ctl = self._auto()
        self._settle(ctl, AUTO_TARGET + 20, AUTO_WARMUP + 6)
        self.assertLess(ctl.index, 60)
        self.assertGreaterEqual(ctl.adjusts, 1)

    def test_low_floor_raises_index(self):
        ctl = self._auto()
        self._settle(ctl, 1.0, AUTO_WARMUP + 6)
        self.assertGreater(ctl.index, 60)

    def test_degenerate_frames_ignored(self):
        # 实板单帧 p10 会在 0 和 73 之间跳：全零帧不进控制量，否则档位会被往错误方向推
        ctl = self._auto()
        self._settle(ctl, [0.0, 0.0, 0.0, 0.0], AUTO_WARMUP + 6)
        self.assertEqual(ctl.index, 60)
        self.assertEqual(ctl.adjusts, 0)
        self.assertIsNone(ctl.floor_smooth)  # 一轮里没有有效帧 → 连平滑值都没有

    def test_scan_median_damps_single_spike(self):
        ctl = self._auto()
        self._settle(ctl, AUTO_TARGET, AUTO_WARMUP + 2)  # 稳在目标
        settled = ctl.index
        # 一轮里夹一帧尖峰：中位数不受影响，档位不动
        self._scan(ctl, [AUTO_TARGET, AUTO_TARGET + 60, AUTO_TARGET, AUTO_TARGET])
        self.assertEqual(ctl.index, settled)
        self.assertEqual(ctl.adjusts, 0)

    def test_deadband_around_target(self):
        ctl = self._auto()
        self._settle(ctl, AUTO_TARGET + AUTO_TOLERANCE * 0.5, AUTO_WARMUP + 6)
        self.assertEqual(ctl.index, 60)
        self.assertEqual(ctl.adjusts, 0)
        self.assertIn("到位", ctl.note)

    def test_confirm_paces_adjustments(self):
        # 调整节奏只由「连续 AUTO_CONFIRM 轮确认」决定，不用墙上时间：
        # 实板每轮约 1.1 s，若设 1.2 s 冷却会每一轮都被卡死（真停摆过）
        ctl = self._auto()
        self._settle(ctl, 40.0, AUTO_WARMUP + AUTO_CONFIRM)
        first = ctl.index
        self.assertEqual(ctl.adjusts, 1)
        self._scan(ctl, 40.0)  # 刚调完：确认计数清零，这一轮不许再动
        self.assertEqual(ctl.index, first)
        self.assertEqual(ctl.adjusts, 1)
        self._scan(ctl, 40.0)
        self._scan(ctl, 40.0)  # 连续两轮越界 → 再调一档
        self.assertEqual(ctl.adjusts, 2)
        self.assertLess(ctl.index, first)

    def test_manual_mode_never_adjusts(self):
        ctl = GainControl(mode="MANUAL", index=60)
        ctl.take_pending()
        self._settle(ctl, 60.0, AUTO_WARMUP + 10)
        self.assertEqual(ctl.index, 60)
        self.assertEqual(ctl.take_pending(), [])

    def test_mock_never_adjusts(self):
        # 假固件的 GAIN 命令是空转：这时候还去调档，等于给用户看假动作
        ctl = GainControl(mode="AUTO", index=60, mock=True)
        ctl.take_pending()
        self._settle(ctl, 60.0, AUTO_WARMUP + 10)
        self.assertEqual(ctl.index, 60)
        self.assertEqual(ctl.adjusts, 0)

    def test_history_is_bounded(self):
        ctl = self._auto()
        for _ in range(80):
            self._scan(ctl, 40.0)
        self.assertLessEqual(len(ctl.history), 20)

    def test_effective_index_read_from_meta(self):
        ctl = GainControl(mode="MANUAL", index=60)
        ctl.observe(FakeSnap(ts=1.0, floor=5.0, code=57))
        self.assertEqual(ctl.effective, 57)  # AGC 下固件自己挑的档位也能看到

    def test_scans_counter(self):
        ctl = self._auto()
        self._settle(ctl, 10.0, 3)
        self.assertEqual(ctl.scans, 3)
        self.assertEqual(ctl.samples, 15)


class SourceOrderTest(unittest.TestCase):
    def test_gain_applied_before_scan(self):
        link = FakeLink()
        ctl = GainControl(mode="MANUAL", index=60)
        ctl.take_pending()
        ctl.request_index(70)
        source = GainAwareSource(link, ctl)
        snaps = list(source.scan(object()))
        # 先发 GAIN 再扫：命令不会插进扫描中途
        self.assertEqual(link.calls, [("MANUAL", 70)])
        self.assertEqual(link.scans, 1)
        self.assertEqual(len(snaps), 3)
        self.assertEqual(ctl.samples, 3)
        self.assertEqual(ctl.floor_code, 20.0)
        self.assertEqual(ctl.scans, 1)  # 轮末结算过（end_scan 由 finally 保证）
        self.assertIsNotNone(ctl.floor_smooth)

    def test_failed_set_gain_keeps_running(self):
        class BadLink(FakeLink):
            def set_gain(self, mode="HARDWARE", index=None):
                raise RuntimeError("串口断了")

        ctl = GainControl(mode="MANUAL", index=60)
        ctl.take_pending()
        ctl.request_step(1)
        source = GainAwareSource(BadLink(), ctl)
        self.assertEqual(len(list(source.scan(object()))), 3)  # 一次设不上不能停采集
        self.assertIn("设增益失败", ctl.state()["note"])

    def test_other_attributes_pass_through(self):
        link = FakeLink()
        link.identity = {"chip": "esp32c5"}
        source = GainAwareSource(link, GainControl())
        self.assertEqual(source.identity, {"chip": "esp32c5"})


class SessionGainTest(unittest.TestCase):
    """走一遍真会话路径：顶栏控件 → HostSession.gain_control → 状态回推。"""

    def _session(self):
        from session import HostConfig, HostSession, to_json

        session = HostSession(bins_out=64)
        session.open(HostConfig.from_json(to_json({"mock": True, "scenario": "quiet"})))
        return session, to_json

    def test_gain_state_reaches_stats_and_info(self):
        session, _ = self._session()
        try:
            stats = session.stats()
            self.assertIn("gain", stats)
            self.assertEqual(stats["gain"]["kind"], "gain")
            self.assertIn("gain", session.describe())
        finally:
            session.close()

    def test_gain_control_actions(self):
        session, to_json = self._session()
        try:
            got = session.gain_control(to_json({"action": "mode", "mode": "AUTO"}))
            self.assertEqual(got["mode"], "AUTO")
            got = session.gain_control(to_json({"action": "step", "delta": -1}))
            self.assertEqual(got["mode"], "MANUAL")  # 按档 = 手动
            got = session.gain_control(to_json({"action": "index", "index": 12}))
            self.assertEqual(got["index"], 12)
            got = session.gain_control(to_json({"action": "target", "target": 8}))
            self.assertAlmostEqual(got["target"], 8.0)
            self.assertEqual(got["mode"], "AUTO")
        finally:
            session.close()

    def test_gain_control_bad_input_does_not_raise(self):
        session, _ = self._session()
        try:
            self.assertFalse(session.gain_control("不是 JSON")["ok"])
            self.assertFalse(session.gain_control("[1,2]")["ok"])
            self.assertFalse(session.gain_control('{"action":"wat"}')["ok"])
            self.assertFalse(session.gain_control('{"action":"index","index":"abc"}')["ok"])
        finally:
            session.close()

    def test_config_rejects_unknown_gain_mode(self):
        from session import HostConfig

        with self.assertRaises(ValueError):
            HostConfig.from_json('{"gain_mode": "TURBO"}')

    def test_config_accepts_gain_mode_and_target(self):
        from session import HostConfig

        cfg = HostConfig.from_json('{"gain_mode": "auto", "gain_target": 6.5, "gain_index": 30}')
        self.assertEqual(cfg.gain_mode, "AUTO")
        self.assertAlmostEqual(cfg.gain_target, 6.5)


if __name__ == "__main__":
    unittest.main()