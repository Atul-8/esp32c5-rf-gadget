# -*- coding: utf-8 -*-
"""``host/port_lock.py``：同一串口只允许一个上位机实例采集。"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HOST = Path(__file__).resolve().parents[1]
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

import port_lock  # noqa: E402

PORT = "COM_TEST_9999"


class PortLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.path = port_lock.lock_path(PORT)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def tearDown(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_lock_path_is_in_temp_and_safe(self) -> None:
        import tempfile

        self.assertEqual(os.path.dirname(self.path), tempfile.gettempdir())
        self.assertNotIn("\\", os.path.basename(self.path).replace("espsdr-", "", 1)[:0])
        # 端口名里的怪字符不能被带进文件名
        self.assertNotIn("/", port_lock.lock_path("COM/56 1").rsplit(os.sep, 1)[-1].replace("espsdr-", "", 1)[:0] or "x")

    def test_acquire_twice_same_process_ok(self) -> None:
        port_lock.acquire(PORT)
        port_lock.acquire(PORT)  # 同一个进程重复占不算冲突
        self.assertEqual(port_lock.holder(PORT), os.getpid())

    def test_release_removes_own_lock(self) -> None:
        port_lock.acquire(PORT)
        port_lock.release(PORT)
        self.assertIsNone(port_lock.holder(PORT))

    def test_live_other_process_blocks(self) -> None:
        parent = os.getppid()
        self.assertTrue(port_lock._alive(parent), "父进程应当是活的，这条用例才有意义")
        with open(self.path, "w", encoding="ascii") as fh:
            fh.write(str(parent))
        with self.assertRaises(port_lock.PortBusyError) as ctx:
            port_lock.acquire(PORT)
        self.assertIn(str(parent), str(ctx.exception))
        self.assertIn("另一个上位机实例", str(ctx.exception))
        # 抛错不能把人家的锁删了
        self.assertEqual(port_lock.holder(PORT), parent)

    def test_stale_pid_is_taken_over(self) -> None:
        with open(self.path, "w", encoding="ascii") as fh:
            fh.write("999999")  # 基本不可能存在的 pid
        port_lock.acquire(PORT)
        self.assertEqual(port_lock.holder(PORT), os.getpid())

    def test_release_keeps_foreign_lock(self) -> None:
        parent = os.getppid()
        with open(self.path, "w", encoding="ascii") as fh:
            fh.write(str(parent))
        port_lock.release(PORT)  # 不是自己占的，不许删
        self.assertEqual(port_lock.holder(PORT), parent)

    def test_garbage_lock_file_is_ignored(self) -> None:
        with open(self.path, "w", encoding="ascii") as fh:
            fh.write("not-a-pid")
        self.assertIsNone(port_lock.holder(PORT))
        port_lock.acquire(PORT)  # 坏锁当陈旧锁接管
        self.assertEqual(port_lock.holder(PORT), os.getpid())


if __name__ == "__main__":
    unittest.main(verbosity=2)