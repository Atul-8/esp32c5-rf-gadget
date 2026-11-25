"""同一个串口只允许一个上位机实例采集（跨进程互斥）。

**为什么要有这个东西**

``pyserial`` 对串口是独占的，但"两个窗口同时开"不会有人当场报错：

* 后启动的那个实例 ``open()`` 失败一次，接着 ``host/link_guard.py`` 每 3 s 重试
  ``reopen()``；先启动的那个只要有一瞬间释放端口（重连、切页、退出），端口就被
  后来者抢走；
* 被顶掉的窗口**不会自己说出来**：实时频谱页的扫频地图是**累积状态**
  （历史热力 + 并集地图），帧断了还画着上一次的 5G 热力；周期瀑布图必须靠
  **持续的新帧**才拼得满下一轮，于是一行都不再更新。

真板实测（2026-11-19 14:47:42 与 14:48:56 各起了一个 ``python app.py``）：
用户盯着被顶掉的那个窗口，看到的就是「频谱有 5G 热力、瀑布不更新」。

**做法**

临时目录里放一个 ``espsdr-<端口>.lock``，内容是本进程 pid：

* ``open()`` 之前 :func:`acquire`：锁里的 pid 若是**别的存活进程**就抛
  :class:`PortBusyError`，界面直接告诉用户"另一个窗口正拿着这个端口"；
* ``close()`` 时 :func:`release`：**只删自己占的那把锁**，不越权删别人的；
* 陈旧锁（pid 已经不存在，或不是本程序）直接接管 —— 崩溃残留不该挡住下次启动。

锁只是"温馨提示"级别的自保：临时目录写不进去时 :func:`acquire` 静默放行，
采集不能因为一个锁文件起不来。
"""
from __future__ import annotations

import os
import re
import tempfile

__all__ = ["PortBusyError", "acquire", "release", "holder", "lock_path"]


class PortBusyError(RuntimeError):
    """这个串口已经被另一个存活的上位机实例占着。"""


def _safe(port: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", str(port or "")) or "port"


def lock_path(port: str) -> str:
    """锁文件路径（放在系统临时目录，不往仓库/用户目录乱写）。"""
    return os.path.join(tempfile.gettempdir(), "espsdr-%s.lock" % _safe(port))


def _alive(pid: int) -> bool:
    """pid 对应的进程还活着吗（跨平台，Windows 走 kernel32）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def holder(port: str) -> int | None:
    """锁里的 pid；没有锁、锁坏了返回 ``None``。"""
    try:
        with open(lock_path(port), "r", encoding="ascii", errors="ignore") as fh:
            text = fh.read(32).strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def acquire(port: str) -> None:
    """占坑。被**别的存活进程**占着就抛 :class:`PortBusyError`。"""
    mine = os.getpid()
    other = holder(port)
    if other is not None and other != mine and _alive(other):
        raise PortBusyError(
            "端口 %s 已经被另一个上位机实例占用，pid %d："
            "同一个端口同时只能有一个窗口采集，请关掉那个窗口或换成别的端口再点开始"
            % (port, other)
        )
    try:
        with open(lock_path(port), "w", encoding="ascii") as fh:
            fh.write(str(mine))
    except OSError:
        # 临时目录不可写：宁可让它采集，也不要因为一个提示用的锁文件起不来
        return


def release(port: str) -> None:
    """放坑。只有锁里写着**自己**的 pid 才删。"""
    if holder(port) != os.getpid():
        return
    try:
        os.remove(lock_path(port))
    except OSError:
        pass