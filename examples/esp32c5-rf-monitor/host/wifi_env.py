"""WiFi 环境扫描 —— 上位机侧拿「有名字的热点名单」。

为什么需要它
------------
板子上跑的是哑射频前端，只有 FFT 功率谱，**没有 802.11 协议栈**，
拿不到 SSID/BSSID。所以「哪台设备在哪个信道」这件事必须由上位机做：
电脑的无线网卡本来就会解析管理帧，``netsh wlan show networks`` 直接给出
SSID / BSSID / 信道 / 信号 / **频道利用率** / 关联客户端数。

拿到名单后通过 ``RD KNOWN <中心频率>`` 灌进固件，固件才能做差分：
「这个频点有能量，而且不在已知名单里」→ 才敢叫「未知」。

一个关键的诚实边界
------------------
路由器会自动换信道（ACS，Auto Channel Selection）。名单是**快照**，
过期后会把换过信道的老路由器误报成「未知」。所以：

* 名单要**周期刷新**（建议 60~120 s），每次先 ``RD KNOWN CLR`` 再全量重灌；
* 单帧的「未知」结论不可信，**持续多轮都无主**才可信。

字段来源（Windows ``netsh wlan show networks mode=bssid``）
--------------------------------------------------------
中文/英文 Windows 输出的标签不同，这里两套都认：

======================  ==================  ==========================
中文标签                 英文标签             含义
======================  ==================  ==========================
``频道``                 ``Channel``          信道号 → 换算中心频率
``信号``                 ``Signal``           信号百分比
``波段``                 ``Band``             2.4 / 5 / 6 GHz
``频道利用率``           ``Channel utilization``  AP 自报的 BSS Load 利用率
``连接的电台``           ``Connected stations``   关联客户端数
======================  ==================  ==========================

``频道利用率`` 是 BSS Load 元素里 AP **自己**报的，不是我们测的，
所以它反映「这个 AP 有多忙」，而不是「这个频段有多挤」——后者要靠
板子的能量扫描。两者对照才有意思：AP 说很忙 + 板子测到能量高 = 真挤；
AP 说很闲 + 板子测到能量高 = 有别人在同一个频点发射（干扰或未知设备）。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

#: 固件里 ``RD_KNOWN_MAX`` 的上限，超了固件会回 ``RDKNOWN full``。

KNOWN_MAX = 48

#: 信道号 → 中心频率 (MHz)。只覆盖 WiFi 实际会用到的编号段。
def channel_to_mhz(ch: int) -> int | None:
    """把 802.11 信道号换成中心频率（MHz）。认不出来就返回 ``None``。"""
    if ch == 14:
        return 2484
    if 1 <= ch <= 13:
        return 2407 + 5 * ch
    if 32 <= ch <= 177:          # 5 GHz（含 5.9 GHz 扩展）
        return 5000 + 5 * ch
    if 1 <= ch <= 233:           # 6 GHz 用 5950 + 5*ch，与 5G 段不重叠
        return 5950 + 5 * ch
    return None


@dataclass
class Bss:
    """一个 BSSID（一个 AP 的一个频点）。"""

    bssid: str = ""
    channel: int = 0
    mhz: int = 0
    band: str = ""
    signal: int = 0
    utilization: int = -1       # BSS Load 频道利用率 %，-1 = AP 没上报
    util_raw: int = -1          # 原始字节 0..255（netsh 直接给这个）
    stations: int = -1          # 关联客户端数，-1 = 没上报


@dataclass
class Network:
    """一个 SSID（可能横跨多个 BSSID / 多个频段）。"""

    ssid: str = ""
    hidden: bool = False        # SSID 为空 → 隐藏网络，netsh 显示成空串
    auth: str = ""
    bss: list[Bss] = field(default_factory=list)

    def mhz_list(self) -> list[int]:
        """这个 SSID 占用的所有中心频率（去重、升序）——就是 ``RD KNOWN`` 的入参。"""
        return sorted({b.mhz for b in self.bss if b.mhz})

    def best_util(self) -> int:
        """各 BSSID 里最高的频道利用率（-1 = 全都没上报）。"""
        vals = [b.utilization for b in self.bss if b.utilization >= 0]
        return max(vals) if vals else -1


# 标签正则：中英文都认，冒号可能是半角或全角
_RE_SSID = re.compile(r"^\s*SSID\s+(\d+)\s*:\s*(.*)$", re.I)
_RE_BSSID = re.compile(r"^\s*BSSID\s+(\d+)\s*:\s*([0-9a-fA-F:]{17})", re.I)
_RE_KEYVAL = re.compile(r"^\s*([^:：]+?)\s*[:：]\s*(.+?)\s*$")

#: 标签必须**长键优先**匹配。早先用 `"频道" in key` 判信道，
#: 结果 ``频道利用率: 134 (52%)`` 被当成「信道 134」，利用率和信道号一起废掉。
_KEYS_UTIL = ("频道利用率", "channelutilization")
_KEYS_STATIONS = ("连接的电台", "connectedstations")
_KEYS_CHANNEL = ("频道", "channel")
_KEYS_SIGNAL = ("信号", "signal")
_KEYS_BAND = ("波段", "band")
_KEYS_AUTH = ("身份验证", "authentication")


def _norm(s: str) -> str:
    """去掉空白和全角空格、转小写：``Channel Utilization`` → ``channelutilization``。"""
    return re.sub(r"[\s　]+", "", s).lower()


def _match(key: str, names: tuple[str, ...]) -> bool:
    return any(_norm(key).startswith(_norm(n)) for n in names)


def _first_int(text: str) -> int:
    """从 ``134 (52% )`` / ``94%`` / ``6`` 里抠第一个整数。"""
    m = re.search(r"-?\d+", text)
    return int(m.group(0)) if m else -1


def parse_netsh(text: str) -> list[Network]:
    """解析 ``netsh wlan show networks mode=bssid`` 的输出。

    容错优先：不认识的标签直接跳过，缺字段就留默认值。
    真实机器的输出会因为驱动/语言/Windows 版本有出入，**宁可少解析也不要抛异常**。
    """
    nets: list[Network] = []
    cur: Network | None = None
    cur_bss: Bss | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _RE_SSID.match(line)
        if m:
            cur = Network(ssid=m.group(2).strip())
            cur.hidden = not cur.ssid
            nets.append(cur)
            cur_bss = None
            continue

        if cur is None:
            continue

        m = _RE_BSSID.match(line)
        if m:
            cur_bss = Bss(bssid=m.group(2).lower())
            cur.bss.append(cur_bss)
            continue

        m = _RE_KEYVAL.match(line)
        if not m:
            continue
        key = m.group(1).strip()
        val = m.group(2).strip()

        # 长键优先：利用率/客户端数必须在「频道」「信号」之前判定
        if _match(key, _KEYS_UTIL):
            if cur_bss is not None:
                # netsh 打印成 ``134 (52% )``：前面是 BSS Load 的原始字节，
                # 括号里才是百分比。原始值 0..255，除以 255 才能当百分比用，
                # 直接当百分比会得到 134%/211% 这种明显越界的数。
                raw = _first_int(val)
                if raw >= 0:
                    cur_bss.util_raw = min(raw, 255)
                    cur_bss.utilization = round(min(raw, 255) * 100 / 255)
        elif _match(key, _KEYS_STATIONS):
            if cur_bss is not None:
                cur_bss.stations = _first_int(val)
        elif _match(key, _KEYS_AUTH):
            if cur_bss is None and not cur.auth:
                cur.auth = val
        elif _match(key, _KEYS_CHANNEL):
            if cur_bss is not None:
                ch = _first_int(val)
                if ch > 0:
                    cur_bss.channel = ch
                    cur_bss.mhz = channel_to_mhz(ch) or 0
        elif _match(key, _KEYS_SIGNAL):
            if cur_bss is not None:
                cur_bss.signal = _first_int(val)
        elif _match(key, _KEYS_BAND):
            if cur_bss is not None:
                cur_bss.band = val

    return nets


def force_scan() -> bool:
    """让无线网卡真做一次**全信道扫描**（Windows ``wlanapi.dll`` WlanScan）。

    为什么光跑 netsh 不够
    --------------------
    ``netsh wlan show networks`` **只读 Windows 的扫描缓存**，本身不触发扫描。
    已连接状态下 Windows 会退化成「部分信道扫描」，缓存里常年只有 1 个 AP：
    实测连发 5 次 netsh，每次 1.4 s，结果恒为 1 个 SSID。而防偷拍场景里
    名单不全直接等于「真实 AP 被当未知」→ B 虚高，判据就废了。

    ``WlanScan`` 直接向驱动下 ``OID_DOT11_SCAN_REQUEST``，**不断开现有连接**。
    实测：调用前 netsh 1 个 → 调用后 3 s 内 47 个，连发 3 次均成功（0.05 s）。

    返回 ``True`` 表示扫描请求已下发（**不代表已经扫完**，驱动要 2~3 s）；
    非 Windows / 无网卡 / 调用失败一律返回 ``False``，绝不抛异常 ——
    刷新失败只是名单偏少，不该让上位机崩掉。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("wlanapi")
        ver = wintypes.DWORD()
        handle = wintypes.HANDLE()
        if api.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)) != 0:
            return False
        try:
            plist = ctypes.POINTER(ctypes.c_byte)()
            if api.WlanEnumInterfaces(handle, None, ctypes.byref(plist)) != 0:
                return False
            try:
                base = ctypes.cast(plist, ctypes.POINTER(ctypes.c_byte))
                # WLAN_INTERFACE_INFO_LIST = dwNumberOfItems(4) + dwIndex(4) + 数组
                if ctypes.cast(base, ctypes.POINTER(ctypes.c_int))[0] <= 0:
                    return False
                raw = bytes(base[8 + i] & 0xFF for i in range(16))
                guid = ctypes.create_string_buffer(raw, 16)
                return api.WlanScan(handle, guid, None, None, None) == 0
            finally:
                api.WlanFreeMemory(plist)
        finally:
            api.WlanCloseHandle(handle, None)
    except Exception:  # noqa: BLE001 - 刷新是尽力而为，绝不打断扫描主流程
        return False


def scan(interface: str = "", refresh: bool = False,
         wait_s: float = 2.5) -> list[Network]:
    """跑一次 ``netsh`` 并解析。

    ``interface`` 留空则由系统挑。失败时返回空表而不是抛异常——
    扫描 WiFi 失败不该让整个上位机崩掉，前端会显示「本机扫不到」。

    ``refresh=True`` 时先 ``force_scan()`` 触发全信道扫描再等 ``wait_s`` 秒。
    **默认不刷新**：调用方自己决定要不要慢这 2.5 s。周期监测、手动扫描按钮
    都该要（名单准 > 快 2 秒），单测不该要（否则每跑一次测试就去敲真网卡）。
    """
    if refresh and force_scan():
        time.sleep(wait_s)
    cmd = ["netsh", "wlan", "show", "networks", "mode=bssid"]
    if interface:
        cmd += [f"name={interface}"]
    try:
        # 编码坑：中文 Windows 的 netsh 走 GBK，但驱动偶发输出 UTF-8 片段。
        # 用 errors="replace" 硬解，反正我们只认 ASCII 标签和数字。
        out = subprocess.run(cmd, capture_output=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.stdout.decode("utf-8", "replace")
        if "SSID" not in text:
            text = out.stdout.decode("gbk", "replace")
    except Exception:  # noqa: BLE001 - 扫不到就是没数据，不该炸
        return []
    return parse_netsh(text)


def known_frequencies(nets: list[Network]) -> list[int]:
    """汇总所有已知热点的中心频率（去重升序）——直接喂给 ``RD KNOWN``。"""
    seen: set[int] = set()
    for n in nets:
        seen.update(n.mhz_list())
    return sorted(seen)


__all__ = ["KNOWN_MAX", "Bss", "Network", "channel_to_mhz", "force_scan",
           "known_frequencies", "parse_netsh", "scan"]