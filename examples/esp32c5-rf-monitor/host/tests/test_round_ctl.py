# -*- coding: utf-8 -*-
"""round_ctl.RoundControl 单测：意图排队 / 射频线程落地 / 读回确认 / 关闭语义。

纪律同 gain：GUI 线程只写意图，apply() 模拟射频线程在两次扫描之间调用。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from round_ctl import DEFAULT_SECS, MAX_SECS, RoundControl  # noqa: E402


class FakeLink:
    """假链路：记录发过的命令，VIEWSEC? 返回固件式应答。"""

    def __init__(self, fail=False, ack=None, ack_script=None):
        self.commands = []
        self.fail = fail
        self.ack = ack
        self.ack_script = list(ack_script or [])  # 每次 VIEWSEC? 依次弹出应答

    def command(self, line, strict=True):
        if self.fail:
            raise IOError("COM56 gone")
        self.commands.append(line)
        if line.startswith("RD VIEWSEC?"):
            if self.ack_script:
                return self.ack_script.pop(0)
            return self.ack
        return "OK"


class TestRoundControl(unittest.TestCase):
    def test_default_state(self):
        rc = RoundControl()
        self.assertEqual(rc.secs, DEFAULT_SECS)
        st = rc.state()
        self.assertTrue(st["on"])
        self.assertEqual(st["secs"], DEFAULT_SECS)

    def test_request_then_apply(self):
        rc = RoundControl()
        st = rc.request_carousel([3, 4, 5])
        self.assertTrue(st["ok"])
        link = FakeLink(ack="RDVIEWSEC 3 4 5")
        rc.apply(link)
        self.assertEqual(
            [c for c in link.commands if not c.startswith("RD VIEWSEC?")],
            ["RD VIEWSEC 0 3", "RD VIEWSEC 1 4", "RD VIEWSEC 2 5"])
        self.assertEqual(rc.secs, [3, 4, 5])
        self.assertEqual(rc.acked, 1)
        self.assertEqual(rc.sent, 1)

    def test_stop_is_all_zero(self):
        rc = RoundControl()
        rc.request_carousel([0, 0, 0])
        link = FakeLink(ack="RDVIEWSEC 0 0 0")
        rc.apply(link)
        self.assertEqual(rc.secs, [0, 0, 0])
        self.assertFalse(rc.is_on())

    def test_clamp(self):
        rc = RoundControl()
        st = rc.request_carousel([-5, 999, 7])
        self.assertTrue(st["ok"])
        link = FakeLink(ack="RDVIEWSEC 0 %d 7" % MAX_SECS)
        rc.apply(link)
        self.assertEqual([c for c in link.commands if not c.startswith("RD VIEWSEC?")],
                         ["RD VIEWSEC 0 0", "RD VIEWSEC 1 %d" % MAX_SECS,
                          "RD VIEWSEC 2 7"])

    def test_bad_secs_rejected(self):
        rc = RoundControl()
        st = rc.request_carousel([1, 2])
        self.assertFalse(st["ok"])
        # 什么都不该发
        link = FakeLink()
        rc.apply(link)
        self.assertEqual(link.commands, [])

    def test_no_pending_no_op(self):
        rc = RoundControl()
        link = FakeLink()
        rc.apply(link)
        self.assertEqual(link.commands, [])

    def test_send_failure_keeps_note(self):
        rc = RoundControl()
        rc.request_carousel([1, 2, 3])
        link = FakeLink(fail=True)
        rc.apply(link)
        st = rc.state()
        self.assertIn("OSError", st["note"])  # 逐页容错：三页失败原因都留痕
        self.assertNotEqual(st["note"], "")
        # 失败后 pending 已消费（不重试轰炸）；secs 不变
        self.assertEqual(rc.take_pending(), None)

    def test_no_ack_fallback(self):
        rc = RoundControl()
        rc.request_carousel([6, 7, 8])
        link = FakeLink(ack=None)  # 发成功但读不回
        rc.apply(link)
        self.assertEqual(rc.acked, 0)
        self.assertEqual(rc.secs, [6, 7, 8])
        self.assertIn("读回", rc.state()["note"])

    def test_mock_link_without_command(self):
        rc = RoundControl()
        rc.request_carousel([1, 2, 3])
        rc.apply(object())  # mock 引擎：链路没有 command
        self.assertIn("mock", rc.state()["note"])

    def test_take_pending_consumed_once(self):
        rc = RoundControl()
        rc.request_carousel([1, 1, 1])
        self.assertEqual(rc.take_pending(), [1, 1, 1])
        self.assertEqual(rc.take_pending(), None)

    def test_query_intent(self):
        rc = RoundControl()
        link = FakeLink(ack="RDVIEWSEC 2 3 4")
        rc.apply(link)               # 无意图：什么都不发
        self.assertEqual(link.commands, [])
        rc.request_query()
        rc.apply(link)               # 纯查询：只发读回，不写入
        self.assertEqual(link.commands, ["RD VIEWSEC?"])
        self.assertEqual(rc.secs, [2, 3, 4])
        self.assertEqual(rc.acked, 1)
        self.assertEqual(rc.sent, 0)

    def test_query_unconfirmed_note(self):
        rc = RoundControl()
        rc.request_query()
        rc.apply(FakeLink(ack=None))
        self.assertIn("读回", rc.state()["note"])


if __name__ == "__main__":
    unittest.main()

    def test_incomplete_write_retries_missing_pages(self):
        """实测坑：某页命令被窗切换打断，读回 [3,4,8]——补发一轮后读到意图值。"""
        rc = RoundControl()
        rc.request_carousel([3, 4, 5])
        link = FakeLink(ack_script=["RDVIEWSEC 8 25 8", "RDVIEWSEC 3 4 5"])
        rc.apply(link)
        st = rc.state()
        self.assertEqual(st["secs"], [3, 4, 5])
        self.assertEqual(st["acked"], 1)
        resent = [c for c in link.commands if c == "RD VIEWSEC 2 5"]
        self.assertEqual(resent, ["RD VIEWSEC 2 5"])  # 只补发不一致的页 2
