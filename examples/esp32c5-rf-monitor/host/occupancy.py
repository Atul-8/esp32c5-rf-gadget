"""逐信道占用统计（上位机侧）——「哪些信道有人、占多久」的慢变量统计。

为什么要单独一个模块
--------------------
圆屏第二页（OCCUPY）已经有这张表，但①只有 5 行一屏、②数字在板子上、③拿不出来。
上位机要做**其他协议**的统计（BLE 广播、图传、无绳电话、无人机……它们不在
WiFi 栅格上），需要一整张表 + 会话内的峰值/均值 + 能落 CSV。

判据与固件同源（``main/common/spectrum.c`` / ``round_ui.c``）
------------------------------------------------------------
一帧里取**中间一半** bin，算 p25 / p50；``p50 − p25 > 15 码`` 判该帧「有传输」。
功率码是 0..255 的**相对**码，不是 dBm（本机没有绝对校准）。

与固件的两处差别（读数字时必须记住）
------------------------------------
1. **触发单位**：固件是「一个 20 MHz 窗采 8 帧 → 一个占空比 → EMA(α=1/8)」，τ≈24 s；
   上位机是「扫到哪算哪，一帧一次」，EMA(α=1/8) 的 τ≈8 帧（约 1 s）。
   所以上位机的 ``pct`` 跳得快得多，两者**不能逐位比**，只能比「同一时刻谁更高」。
2. **帧归属**：只有帧中心离某信道 ``MATCH_MHZ`` 以内才算该信道的测量；
   宽窗（80 MS/s）中心之外的信道不会被这帧代表，宁可少算也不张冠李戴。

对外三个数
----------
* ``pct``  逐帧命中指示的 EMA（0..100）——「现在有多热」
* ``max``  会话内 ``pct`` 的最高值 —— 认**突发**协议靠它（BLE 广播是尖的）
* ``avg``  累计命中帧/总帧 ——「这一整段时间里一直在发射吗」
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable

#: 与固件 ``s_ch_mhz[]`` 同序（``main/common/round_ui.c``）。改这里要同步改固件，
#: 否则上位机的表和圆屏第二页会对不上号。
CHANNELS: tuple[int, ...] = (
    2412, 2417, 2422, 2427, 2432, 2437, 2442, 2447, 2452, 2457, 2462, 2467, 2472,
    5180, 5200, 5220, 5240, 5260, 5280, 5300, 5320,
    5500, 5520, 5540, 5560, 5580, 5600, 5620, 5640, 5660, 5680, 5700, 5720,
    5745, 5765, 5785, 5805, 5825,
)

#: 帧中心离信道多远才算「这一帧测的是这个信道」。20 MHz 窗给 2.5 MHz 余量，
#: 刚好容下固件的整数取整（``(uint16_t)mhz``）与规划里的频率微差。
MATCH_MHZ = 2.5

#: 判「有传输」的门限（码），与固件 ``OCC_DB`` 一致。
THR_CODE = 15

#: EMA 分母（α = 1/8），与固件同量级；上位机是逐帧迭代，详见模块注释。
EMA_DEN = 8


def quantile(ordered: list[float], num: int, den: int) -> float:
    """升序列表上的分位（与固件 ``hist_quantile()`` 同构：第 n*num/den 个样本，1 基）。"""
    if not ordered:
        return 0.0
    idx = max(1, len(ordered) * num // den) - 1
    return ordered[min(idx, len(ordered) - 1)]


def is_busy(bins: Iterable[float]) -> bool:
    """这一帧算不算「有传输」：只取中间一半 bin，看 p50 与 p25 的落差。"""
    vals = list(bins)
    n = len(vals)
    if n < 8:
        return False
    mid = sorted(vals[n // 4: n - n // 4])
    return (quantile(mid, 1, 2) - quantile(mid, 1, 4)) > THR_CODE


def wifi_channel(mhz: int) -> str:
    """WiFi 信道号（2.4G: (f−2407)/5；5G: (f−5000)/5）；非栅格点回空串。"""
    if 2407 <= mhz <= 2484:
        num = (mhz - 2407) / 5
    elif 5000 < mhz <= 5925:
        num = (mhz - 5000) / 5
    else:
        return ""
    return str(round(num)) if abs(num - round(num)) < 1e-6 else ""


def band_of(mhz: int) -> str:
    if mhz < 3000:
        return "2.4G"
    if mhz < 5470:
        return "5G"
    return "5.8G"


class ChannelOccupancy:
    """线程安全：射频线程只调 :meth:`feed`，主线程只调 :meth:`snapshot` / :meth:`reset`。

    刻意**不是** QObject —— 跨线程读 QObject 的实例属性在 PySide6 里是已知的死法
    （栈溢出、无回溯）。这里用普通对象 + 锁，和 ``Runner.box`` 的约定一致。
    """

    def __init__(self, channels: Iterable[int] = CHANNELS) -> None:
        self._lock = threading.Lock()
        self.channels: tuple[int, ...] = tuple(int(c) for c in channels)
        self._reset_locked()
        self.t0 = time.time()

    # ---- 内部 ------------------------------------------------------------
    def _reset_locked(self) -> None:
        self._st: dict[int, dict[str, Any]] = {
            ch: {"frames": 0, "hits": 0, "pct": 0.0, "max": 0.0, "last_ts": 0.0}
            for ch in self.channels
        }
        self._fed = 0          # 进来多少帧
        self._matched = 0      # 其中有多少帧对上了信道
        self._misses = 0       # 对上信道但落在已知频段之外（理论上不会有）

    def _match(self, center_mhz: float) -> int | None:
        best: int | None = None
        best_d = MATCH_MHZ + 1e-9
        for ch in self.channels:
            d = abs(center_mhz - ch)
            if d < best_d:
                best, best_d = ch, d
        return best

    # ---- 供射频线程 ------------------------------------------------------
    def feed(self, snap: Any) -> int | None:
        """吃一帧 ``Snapshot``；返回被判为「有传输」的信道（没对上就 None）。"""
        bins = getattr(snap, "bins_db", None)
        center_hz = getattr(snap, "center_hz", 0) or 0
        if not bins or not center_hz:
            return None
        center_mhz = float(center_hz) / 1e6
        ch = self._match(center_mhz)
        busy = is_busy(bins) if ch is not None else False
        now = time.time()
        with self._lock:
            self._fed += 1
            if ch is None:
                self._misses += 1
                return None
            self._matched += 1
            st = self._st[ch]
            st["frames"] += 1
            if busy:
                st["hits"] += 1
            hit = 100.0 if busy else 0.0
            st["pct"] += (hit - st["pct"]) / EMA_DEN
            if st["pct"] > st["max"]:
                st["max"] = st["pct"]
            st["last_ts"] = now
        return ch if busy else None

    # ---- 供界面线程 ------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self._reset_locked()
        self.t0 = time.time()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            rows = []
            for ch in self.channels:
                st = self._st[ch]
                frames = int(st["frames"])
                avg = (100.0 * st["hits"] / frames) if frames else 0.0
                rows.append({
                    "mhz": ch,
                    "wifi_ch": wifi_channel(ch),
                    "band": band_of(ch),
                    "pct": round(float(st["pct"]), 1),
                    "max": round(float(st["max"]), 1),
                    "avg": round(avg, 1),
                    "frames": frames,
                    "hits": int(st["hits"]),
                    "seen": bool(frames),
                    "age_s": round(now - float(st["last_ts"]), 1) if frames else None,
                })
            return {
                "ok": True,
                "channels": rows,
                "fed": self._fed,
                "matched": self._matched,
                "unmatched": self._misses,
                "match_mhz": MATCH_MHZ,
                "threshold": THR_CODE,
                "ema_den": EMA_DEN,
                "elapsed_s": round(now - self.t0, 1),
            }

    def csv_rows(self) -> list[str]:
        """一行一个信道（快照口径），给导出用。"""
        snap = self.snapshot()
        out = []
        for r in snap["channels"]:
            out.append("%d,%s,%s,%.1f,%.1f,%.1f,%d,%d"
                       % (r["mhz"], r["wifi_ch"], r["band"], r["pct"], r["max"],
                          r["avg"], r["frames"], r["hits"]))
        return out

    @staticmethod
    def csv_header() -> str:
        return "mhz,wifi_ch,band,occ_pct,max_pct,avg_pct,frames,hits"