"""给 ``EspSdrLink`` 补两件串口自保：写命令前排空残留、失效后自动重同步。

**为什么要排空**

一窗 ``SPEC`` 的结尾是 ``SPECEND`` 文本行，但真板在该行之后仍可能吐出收尾帧
（``drops`` 计数非零时尤其明显）。主机若没把这些字节读干净，下一条文本命令
（``FREQ`` / ``GAIN`` / ``SPEC``）的 ``readline()`` 就会把二进制帧当"行"读掉一部分，
紧接着 ``scan()`` 的 ``_read_exact(4)`` 落在帧中间 → magic 不匹配 →
``SerialLink.failed = True``，之后**所有** ``scan()`` 都抛「连接已标记失效」，
界面顶栏随即变成「未连接／SerialLinkError」。

真板实测：400~600 个窗里会撞上 1 次（约 1/400~1/600），撞上之后**不会自愈**
（``EspSdrLink.open()`` 不复位 ``failed``），只能重启上位机 —— 现场表现就是
「跑一会儿提示断开串口」。瀑布页最容易被撞上，只是因为它让人愿意长时间盯着看，
不是它特有的 bug。

**为什么放在 host 层而不是改 core/**

``core/`` 里的判据与协议解析一个字不动：这里只做两件外围保护 ——
① 每条文本命令前把读缓冲里的残留二进制排空（协议层的标准做法）；
② 链路被判失效后重开串口并复位失效标记（``open()`` 会重新 SYNC + 重读 CAPS）。
"""
from __future__ import annotations

import time
from typing import Any, Iterator

from core.serial_link import EspSdrLink, SerialLinkError
from core.snapshot import Snapshot

__all__ = ["ResilientLink", "REOPEN_COOLDOWN_S"]

#: 两次重同步之间的最小间隔：串口真被拔掉时不能让 RF 线程原地狂重试
REOPEN_COOLDOWN_S = 3.0


class ResilientLink(EspSdrLink):
    """``EspSdrLink`` + 前置排空 + 失效自愈（对外接口完全一致）。"""

    #: 最近一次重同步的时刻（``reopen()`` 成功后更新）
    last_reopen_at: float = 0.0
    #: 累计重同步次数（界面/自检可以读出来证明「自愈真的发生过」）
    reopen_count: int = 0

    # ---- 排空 -------------------------------------------------------------
    def _flush_input(self) -> int:
        """丢掉读缓冲里没人认领的字节（返回丢掉的字节数，仅用于诊断）。"""
        port = getattr(self, "_port", None)
        if port is None:
            return 0
        try:
            waiting = int(getattr(port, "in_waiting", 0) or 0)
        except Exception:  # noqa: BLE001 - 假固件/自定义 factory 没有 in_waiting
            return 0
        if waiting <= 0:
            return 0
        try:
            port.reset_input_buffer()
        except Exception:  # noqa: BLE001
            try:
                port.read(waiting)
            except Exception:  # noqa: BLE001
                return 0
        return waiting

    def _write(self, data: bytes) -> None:  # noqa: D102 - 见模块 docstring
        self._flush_input()
        super()._write(data)

    # ---- 自愈 -------------------------------------------------------------
    def reopen(self, cooldown_s: float | None = None) -> bool:
        """重开串口并复位 ``failed``；成功返回 True。

        只在**上一次重开已经过去** ``cooldown_s``（默认 :data:`REOPEN_COOLDOWN_S`）
        时才真的动手，避免串口被拔掉时原地死循环。
        """
        gap = REOPEN_COOLDOWN_S if cooldown_s is None else float(cooldown_s)
        now = time.monotonic()
        if self.reopen_count and now - self.last_reopen_at < gap:
            return False
        try:
            super().close()
        except Exception:  # noqa: BLE001 - 关不掉也要往下重开
            pass
        self.failed = False
        try:
            super().open()
        except Exception as exc:  # noqa: BLE001 - 记下来，让上层如实报错
            self.failed = True
            raise SerialLinkError(f"重新连接失败：{type(exc).__name__}: {exc}") from exc
        self.last_reopen_at = time.monotonic()
        self.reopen_count += 1
        return True

    def scan(self, request: Any) -> Iterator[Snapshot]:
        """扫一个窗：链路若已被判失效，先重同步再扫（而不是永远抛）。"""
        if self.failed:
            self.reopen()
        yield from super().scan(request)