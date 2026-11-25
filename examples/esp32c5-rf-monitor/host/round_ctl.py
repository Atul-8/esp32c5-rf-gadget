"""round_ctl.py —— 圆屏轮播控制（``RD VIEWSEC``）。

固件侧语义（真板 2026-11-25 验过）：

* ``RD VIEWSEC a b c``：页 0/1/2 的轮播停留秒数，0 = 该页不停；
  **全 0 = 轮播关**。默认 8/25/8。
* ``RD VIEWSEC?`` → ``RDVIEWSEC a b c``（回包行首无空格）。

线程纪律（与 ``gain.py`` 同一条，踩过一次就记住的）：

* ``GAIN`` / ``RD VIEWSEC`` 命令都必须在**两次扫描之间**发。扫描中途插命令
  会把串口读缓冲打乱（实测 ``SPEC 启动回复异常``）。
* 所以这里**只写意图**：GUI 线程调 ``request_*()``，射频线程在
  ``GainAwareSource.scan()`` 开头（两次扫描之间的固定间隙）调 ``apply()`` 落地。
* device 自扫引擎下 ``link`` 是 ``SweepLinkFacade``：``command()`` 自带
  停扫→执行→恢复（``pause_around``），天然安全，只是稍重（用户点按钮低频，
  可接受）。主机逐窗引擎下此刻本来就没有帧流，直接发。
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

#: 每页秒数上限 = 固件上限（``sec > 255`` 回 ``ERR rd_viewsec_sec``）；0 = 关。
MAX_SECS = 255
DEFAULT_SECS = [8, 25, 8]

_RDVIEWSEC_ACK = re.compile(r"RDVIEWSEC\s+(\d+)\s+(\d+)\s+(\d+)")


def _clamp(v: int) -> int:
    return max(0, min(MAX_SECS, int(v)))


class RoundControl:
    """圆屏轮播控制器：GUI 线程写意图，射频线程落地（同 GainControl 模式）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[int] | None = None
        #: 读回意图：status 按钮排队一次真正的 ``RD VIEWSEC?``（同样只在扫描间隙发）
        self._pending_query = False
        #: 最近一次**读回确认**的秒数（apply 里发完立刻 ``RD VIEWSEC?`` 验证）。
        self.secs: list[int] = list(DEFAULT_SECS)
        self.sent = 0
        self.acked = 0
        self.note = ""

    # ---- GUI 线程：只写意图 --------------------------------------------
    def request_carousel(self, secs: list[int] | tuple[int, ...]) -> dict[str, Any]:
        vals = [_clamp(s) for s in list(secs)[:3]]
        if len(vals) < 3:
            st = self.state()
            st["ok"] = False
            st["message"] = "需要 3 个页秒数"
            return st
        with self._lock:
            self._pending = vals
        return self.state(message="已排队：[%d %d %d]%s" % (
            vals[0], vals[1], vals[2],
            "（轮播关）" if not any(vals) else ""))

    def request_default(self) -> dict[str, Any]:
        return self.request_carousel(DEFAULT_SECS)

    def request_query(self) -> dict[str, Any]:
        """「读回板子状态」：排队一次真正的串口读回（不写入）。"""
        with self._lock:
            self._pending_query = True
        return self.state(message="已排队：读回板子秒数")

    # ---- 射频线程：两次扫描之间落地 ------------------------------------
    def take_pending(self) -> list[int] | None:
        with self._lock:
            pending, self._pending = self._pending, None
            return pending

    def take_query(self) -> bool:
        with self._lock:
            flag, self._pending_query = self._pending_query, False
            return flag

    def apply(self, link: Any) -> None:
        """把挂起的意图落到固件上（**只在两次扫描之间调**）。"""
        pending = self.take_pending()
        query = self.take_query()
        if pending is None and not query:
            return
        command = getattr(link, "command", None)
        if not callable(command):
            with self._lock:
                self.note = "链路不支持 command()（mock 引擎无串口）"
                if pending is not None:
                    self.secs = list(pending)
            return
        try:
            if pending is not None:
                # 固件协议是逐页单条：RD VIEWSEC <view> <sec>（0..255）。
                # 一次发三个数字会被固件回 ERR command（实测 2026-11-25）。
                # 逐页容错：一页失败（超时/异常）也要把其余页发完——中途放弃会
                # 留下非零页，「任一页 >0 即开轮播」会让板子继续转（用户实测
                # “停止不了轮播”的直接根因之一）。
                errors: list[str] = []
                for view, sec in enumerate(pending):
                    try:
                        command("RD VIEWSEC %d %d" % (view, sec), strict=False)
                    except Exception as exc:  # noqa: BLE001 - 单页失败不拖垮其余页
                        errors.append("p%d %s" % (view, type(exc).__name__))
                if not errors:
                    self.sent += 1

            # 写完（或单纯查询）后立刻读回：此刻在扫描间隙，应答不会被帧流抢走。
            # 但不能零间隔——真板实测（2026-11-25）：扫描中固件串口任务忙于帧输出，
            # 立即读回会拿到旧值（写 [3,4,5] 读回 [3,4,8]，页 2 命令已在 RX 缓冲
            # 里但尚未被解析）；手动串口逐条带间隔发完全正常。给 30ms 解析时间。
            time.sleep(0.03)
            ack = ""
            try:
                ack = command("RD VIEWSEC?", strict=False) or ""
            except Exception as exc:  # noqa: BLE001 - 读回失败不算发送失败
                errors.append("ack %s" % type(exc).__name__)
            m = _RDVIEWSEC_ACK.search(ack)
            if pending is not None and m:
                got = [int(m.group(i)) for i in (1, 2, 3)]
                if got != list(pending):
                    # 写入不完整（实测：某页命令被窗切换打断，读回 [3,4,8]）。
                    # 最小闭环：同一间隙内对不一致的页补发一轮，再读回核对。
                    for view, sec in enumerate(pending):
                        if got[view] != sec:
                            try:
                                command("RD VIEWSEC %d %d" % (view, sec), strict=False)
                            except Exception:  # noqa: BLE001 - 补发失败走下方 note
                                pass
                    time.sleep(0.03)  # 同上：给固件解析补发命令的时间再读回
                    try:
                        ack = command("RD VIEWSEC?", strict=False) or ""
                    except Exception:  # noqa: BLE001
                        ack = ""
                    m = _RDVIEWSEC_ACK.search(ack)
            with self._lock:
                if m:
                    self.secs = [int(m.group(i)) for i in (1, 2, 3)]
                    self.acked += 1
                    self.note = "；".join(errors)  # 读回成功 → 板上状态已是意图值；留错源排查
                elif pending is not None:
                    self.secs = list(pending)
                    self.note = "已发送但未读回确认（串口繁忙）" + ("；" + "；".join(errors) if errors else "")
                else:
                    self.note = "读回未确认（串口繁忙，再试一次）"
        except Exception as exc:  # noqa: BLE001 - 一次设不上不该停掉整个采集
            with self._lock:
                self.note = "轮播设置失败：%s: %s" % (type(exc).__name__, exc)

    # ---- 状态 ----------------------------------------------------------
    def is_on(self) -> bool:
        return any(self.secs)

    def state(self, message: str = "") -> dict[str, Any]:
        with self._lock:
            return {
                "kind": "round",
                "ok": True,
                "secs": list(self.secs),
                "on": any(self.secs),
                "sent": self.sent,
                "acked": self.acked,
                "note": self.note,
                "message": message,
            }


def parse_payload(payload: str) -> dict[str, Any]:
    """前端 JSON → dict；坏输入返回 {}（调用方自己报错）。"""
    try:
        data = json.loads(payload) if payload else {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
