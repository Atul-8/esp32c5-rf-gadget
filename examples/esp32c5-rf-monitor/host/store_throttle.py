"""把 ``core.store.Store`` 包一层：同窗多帧合并，跨窗立刻落盘。

为什么需要（真板实测，不是推测）：

* 上位机默认把快照写进 SQLite 留档，而 ``core.store.Store.add()`` 是**一条一提交**；
* 真板主机逐窗模式一窗回 ~60 帧，全部落库 → 采集时间片从 **134 ms 涨到 526 ms**
  （窗率 7.6/s → 2.6/s，见 ``testing/`` 里的 visit 探针），瀑布图一轮从 ~6 s 变成 20 s 以上，
  页面上就只剩两行、5G HIGH 那一段看着像「丢了」；
* 射频线程不该等磁盘，历史也不需要把一个窗里的 60 帧原样留着——它们相位不同，
  功率码几乎一样，留**最大保持**的那一条信息量完全等价（与瀑布图口径一致）。

所以这里不改 ``core/``，只在外围收口：

* 同一个 ``(module, center_hz, bw_hz)`` 的连续帧按**最大保持**合并；
* 换窗（或同一窗攒够 ``max_hold_s``）才真正写库；
* ``frames`` / ``rows`` 两个计数如实上报，界面能说清「收了多少帧、落了多少条」。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from core.snapshot import Snapshot

#: 同一个窗最多攒这么久就必须落盘：防止某个窗被反复重扫、数据一直压在内存里不落地
MAX_HOLD_S = 1.0


class WindowStore:
    """``Store`` 的节流外壳：``Scheduler`` 照样一帧一调，写库按「窗」走。"""

    def __init__(self, store: Any, max_hold_s: float = MAX_HOLD_S) -> None:
        self.store = store
        self.max_hold_s = float(max_hold_s)
        #: 真正写进库的条数 / 收到的帧数（对不上是正常的：一窗多帧合并成一条）
        self.rows = 0
        self.frames = 0
        #: 射频线程写、界面线程读统计；只有 flush 会碰 _held，加把锁省心
        self._lock = threading.Lock()
        self._key: tuple[str, int, int] | None = None
        self._held: Snapshot | None = None
        self._held_at = 0.0

    # ---- 写入 ------------------------------------------------------------
    def add(self, snap: Snapshot, module: str | None = None) -> int:
        """收一帧。返回 0 表示「已收下（或已合并），但这次没写库」。"""
        with self._lock:
            self.frames += 1
            name = str(module or (snap.meta or {}).get("module", "") or "")
            key = (name, int(snap.center_hz), int(snap.bw_hz))
            if (
                key == self._key
                and self._held is not None
                and len(self._held.bins_db) == len(snap.bins_db)
            ):
                merged = tuple(a if a >= b else b for a, b in zip(self._held.bins_db, snap.bins_db))
                self._held = Snapshot(
                    ts=snap.ts,
                    center_hz=snap.center_hz,
                    bw_hz=snap.bw_hz,
                    bins_db=merged,
                    meta=dict(self._held.meta, **dict(snap.meta or {})),
                )
                if float(snap.ts) - self._held_at >= self.max_hold_s:
                    self._flush_locked()
                return 0
            self._flush_locked()
            self._key = key
            self._held = snap
            self._held_at = float(snap.ts)
            return 0

    def flush(self) -> int:
        """把当前攒着的那一条写进库（换窗时会自动调，这里给收尾用）。"""
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> int:
        held = self._held
        key = self._key
        if held is None:
            return 0
        self._held = None
        self._key = None
        module = (key[0] if key else "") or None
        rowid = int(self.store.add(held, module=module) or 0)
        self.rows += 1
        return rowid

    def state(self) -> dict[str, Any]:
        """给界面/探针看的计数：收了多少帧、落了多少条。"""
        return {"frames": self.frames, "rows": self.rows, "path": getattr(self.store, "path", "")}

    # ---- 其余属性透传给真 Store（count / iter_between / time_span / ...）----
    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)