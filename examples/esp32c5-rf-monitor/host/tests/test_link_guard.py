"""串口自保层：命令前排空残留帧 + 失效后自动重同步（host/link_guard.py）。

跑法（在 ``host/`` 下）：

    python tests/test_link_guard.py -v

为什么要这两件事（真板实测）：一窗 SPEC 结束后设备可能还吐着收尾帧，主机若没读干净，
下一条文本命令的 readline() 会把二进制帧当行读掉，随后 magic 对不上 →
``EspSdrLink.failed = True``，而 core 的 ``scan()`` 一旦置位就**永不再试**，
现场表现就是「跑一会儿提示断开串口，只能重启上位机」。这里守住两条行为。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import paths  # noqa: E402 - 本地上位机模块

paths.ensure_on_path()  # 让 host 模块能 import core/tests

from core.registry import ScanRequest  # noqa: E402
from link_guard import REOPEN_COOLDOWN_S, ResilientLink  # noqa: E402
from tests.fake_firmware import FakeFirmware  # noqa: E402


class FakePort:
    """只够测排空：write/read/readline/in_waiting/reset_input_buffer。"""

    def __init__(self, pending: bytes = b"") -> None:
        self.pending = bytearray(pending)
        self.written = bytearray()
        self.flushed = 0
        self.replies: list[bytes] = []

    # pyserial 兼容
    @property
    def in_waiting(self) -> int:
        return len(self.pending)

    def reset_input_buffer(self) -> None:
        self.flushed += 1
        self.pending.clear()

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self.pending[:size])
        del self.pending[:size]
        return chunk

    def readline(self) -> bytes:
        index = self.pending.find(b"\n")
        if index < 0:
            return b""
        chunk = bytes(self.pending[: index + 1])
        del self.pending[: index + 1]
        return chunk

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def close(self) -> None:
        return None


class FlushBeforeWrite(unittest.TestCase):
    """写命令之前必须把读缓冲里没人认领的字节丢掉。"""

    def test_pending_bytes_are_dropped_before_command(self):
        port = FakePort(pending=b"\xff\x00\x13\x37tail")  # 上一窗没读完的帧尾巴
        link = ResilientLink("MOCK")
        link._port = port  # 跳过握手，只测 _write 的保护
        link._write(b"FREQ 2412\n")
        self.assertEqual(port.flushed, 1, "写命令前没有排空读缓冲")
        self.assertEqual(len(port.pending), 0, "残留字节应当被丢掉")
        self.assertEqual(bytes(port.written), b"FREQ 2412\n", "命令本身要完整发出")

    def test_empty_buffer_costs_nothing(self):
        port = FakePort()
        link = ResilientLink("MOCK")
        link._port = port
        link._write(b"FREQ 2437\n")
        self.assertEqual(port.flushed, 0, "没有残留就不该动读缓冲")
        self.assertEqual(bytes(port.written), b"FREQ 2437\n")

    def test_no_port_is_harmless(self):
        link = ResilientLink("MOCK")
        self.assertEqual(link._flush_input(), 0)


def make_link(frames: int = 2) -> tuple[ResilientLink, FakeFirmware]:
    firmware = FakeFirmware(frames_per_scan=frames)
    link = ResilientLink("MOCK", factory=lambda port, baud, timeout=5.0: firmware)
    link.open()
    return link, firmware


class RecoverAfterFailure(unittest.TestCase):
    """failed 一旦置位，scan() 自己重同步，而不是永远抛「连接已标记失效」。"""

    def test_scan_recovers_from_failed(self):
        link, _firmware = make_link(frames=3)
        request = ScanRequest(2_442_000_000, 20_000_000, 200, 512)
        try:
            first = list(link.scan(request))
            self.assertEqual(len(first), 3)
            link.failed = True  # 模拟真板帧错位：magic 对不上
            healed = list(link.scan(request))
            self.assertEqual(len(healed), 3, "重同步之后应该照常出帧")
            self.assertFalse(link.failed, "重同步后失效标记必须清掉")
            self.assertEqual(link.reopen_count, 1, "重开次数要能被界面/自检看到")
        finally:
            link.close()

    def test_reopen_cooldown_blocks_hot_loop(self):
        link, _firmware = make_link()
        try:
            self.assertTrue(link.reopen())
            self.assertFalse(link.reopen(), "冷却期内不该再重开")
            self.assertTrue(link.reopen(cooldown_s=0.0), "显式放行时应当能重开")
            self.assertEqual(link.reopen_count, 2)
            self.assertGreaterEqual(REOPEN_COOLDOWN_S, 1.0, "冷却时间太短会原地空转")
        finally:
            link.close()

    def test_reopen_raises_when_port_is_gone(self):
        def broken(port, baud, timeout=5.0):
            raise OSError("could not open port")

        link = ResilientLink("MOCK", factory=broken)
        with self.assertRaises(Exception):
            link.reopen()
        self.assertTrue(link.failed, "重开失败要留成 failed，让上层走「自动停机」那条路")


if __name__ == "__main__":
    unittest.main(verbosity=2)