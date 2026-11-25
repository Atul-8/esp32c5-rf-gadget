"""时间片轮转调度器：按插件 ``slice_weight`` 分配射频时间。

单射频硬件的物理边界：同一时刻只能有一个模块在扫。调度器负责
"按配比轮流让出射频"，并保证一个模块出错不拖死其它模块。

实现用平滑加权轮询（nginx smooth WRR）：每轮给活跃插件加自己的权重，
取权值最高者服务，服务后减去本轮活跃权重之和。这样配比准确且不会
出现"某个模块连占三档"的尖刺。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Protocol, Sequence

from .registry import ModePlugin, Registry, ScanRequest
from .snapshot import Snapshot


class Source(Protocol):
    """快照源接口：真实硬件（core.serial_link.EspSdrLink）与 mock 都实现它。"""

    def scan(self, request: ScanRequest) -> Iterable[Snapshot]:
        ...


@dataclass
class PluginStats:
    slices: int = 0
    snapshots: int = 0
    errors: int = 0
    last_error: str | None = None
    seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "slices": self.slices,
            "snapshots": self.snapshots,
            "errors": self.errors,
            "last_error": self.last_error,
            "seconds": round(self.seconds, 2),
        }


@dataclass
class SchedulerStats:
    slices: int = 0
    snapshots: int = 0
    elapsed_s: float = 0.0
    per_plugin: dict[str, PluginStats] = field(default_factory=dict)
    source_errors: int = 0
    last_source_error: str | None = None

    def share(self) -> dict[str, float]:
        total = self.slices or 1
        return {k: round(v.slices / total, 3) for k, v in self.per_plugin.items()}

    def as_dict(self) -> dict[str, object]:
        return {
            "slices": self.slices,
            "snapshots": self.snapshots,
            "elapsed_s": round(self.elapsed_s, 2),
            "share": self.share(),
            "per_plugin": {k: v.as_dict() for k, v in self.per_plugin.items()},
            "source_errors": self.source_errors,
            "last_source_error": self.last_source_error,
        }


class Scheduler:
    """按权重轮转：注册表 + 快照源 + 可选存储。"""

    def __init__(
        self,
        source: Source,
        registry: Registry,
        store: object | None = None,
        on_snapshot: Callable[[Snapshot], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.source = source
        self.registry = registry
        self.store = store
        self.on_snapshot = on_snapshot
        self._clock = clock
        self._sleep = sleep
        self._logger = logger or (lambda _msg: None)
        self.stats = SchedulerStats()
        self._credits: dict[str, float] = {}
        self._queues: dict[str, list[ScanRequest]] = {}
        for plugin in registry.plugins:
            self.stats.per_plugin[plugin.id] = PluginStats()
            self._credits[plugin.id] = 0.0
            self._queues[plugin.id] = []

    # ---- 内部 ------------------------------------------------------------
    def _refill(self, plugin: ModePlugin) -> None:
        try:
            requests = list(plugin.scan_requests())
        except Exception as exc:  # noqa: BLE001 - 单个模块的计划失败不该拖死全局
            self._record_error(plugin, exc, stage="scan_requests")
            requests = []
        self._queues[plugin.id] = [r for r in requests if isinstance(r, ScanRequest)]

    def _record_error(self, plugin: ModePlugin, exc: BaseException, stage: str) -> None:
        stats = self.stats.per_plugin.setdefault(plugin.id, PluginStats())
        stats.errors += 1
        stats.last_error = f"{stage}: {type(exc).__name__}: {exc}"
        self._logger(f"[warn] {plugin.id} {stats.last_error}")

    def _pick(self, active: Sequence[ModePlugin]) -> ModePlugin | None:
        if not active:
            return None
        total = sum(p.slice_weight for p in active) or float(len(active))
        for plugin in active:
            weight = plugin.slice_weight or 1.0
            self._credits[plugin.id] += weight
        chosen = max(active, key=lambda p: (self._credits[p.id], -active.index(p)))
        self._credits[chosen.id] -= total
        return chosen

    def _next_request(self, plugin: ModePlugin) -> ScanRequest | None:
        if not self._queues[plugin.id]:
            self._refill(plugin)
        queue = self._queues[plugin.id]
        return queue.pop(0) if queue else None

    # ---- 主循环 ----------------------------------------------------------
    def run(
        self,
        duration_s: float | None = None,
        max_slices: int | None = None,
        max_snapshots: int | None = None,
        max_idle_rounds: int = 3,
    ) -> SchedulerStats:
        """跑调度循环；``duration_s`` / ``max_slices`` / ``max_snapshots`` 任一先到即停。"""
        started = self._clock()
        idle_rounds = 0
        while True:
            if duration_s is not None and self._clock() - started >= duration_s:
                break
            if max_slices is not None and self.stats.slices >= max_slices:
                break
            if max_snapshots is not None and self.stats.snapshots >= max_snapshots:
                break
            active = []
            for plugin in self.registry.plugins:
                if not self._queues[plugin.id]:
                    self._refill(plugin)
                if self._queues[plugin.id]:
                    active.append(plugin)
            if not active:
                idle_rounds += 1
                if idle_rounds > max_idle_rounds:
                    self._logger("[info] 所有模块都没有扫描计划，调度退出")
                    break
                self._sleep(0.05)
                continue
            idle_rounds = 0
            plugin = self._pick(active)
            if plugin is None:  # pragma: no cover - active 非空时不会发生
                break
            request = self._next_request(plugin)
            if request is None:
                continue
            self._serve(plugin, request)
        self.stats.elapsed_s = self._clock() - started
        return self.stats

    def _serve(self, plugin: ModePlugin, request: ScanRequest) -> None:
        stats = self.stats.per_plugin.setdefault(plugin.id, PluginStats())
        slice_start = self._clock()
        stats.slices += 1
        self.stats.slices += 1
        try:
            for snap in self.source.scan(request):
                stats.snapshots += 1
                self.stats.snapshots += 1
                try:
                    plugin.on_snapshot(snap)
                except Exception as exc:  # noqa: BLE001
                    self._record_error(plugin, exc, stage="on_snapshot")
                if self.store is not None:
                    try:
                        self.store.add(snap, module=plugin.id)
                    except Exception as exc:  # noqa: BLE001
                        self.stats.source_errors += 1
                        self.stats.last_source_error = f"store: {type(exc).__name__}: {exc}"
                if self.on_snapshot is not None:
                    self.on_snapshot(snap)
        except Exception as exc:  # noqa: BLE001 - 射频层错误只影响本次时间片
            self.stats.source_errors += 1
            self.stats.last_source_error = f"{type(exc).__name__}: {exc}"
            self._record_error(plugin, exc, stage="source")
        finally:
            stats.seconds += self._clock() - slice_start

    # ---- 便捷入口 --------------------------------------------------------
    def run_iterations(self, iterations: int, **kwargs: object) -> SchedulerStats:
        return self.run(max_slices=iterations, **kwargs)  # type: ignore[arg-type]

    def iter_slices(self, max_slices: int) -> Iterator[tuple[ModePlugin, ScanRequest]]:
        """调试用：只产出"谁拿到了哪次扫描"，不真的扫。"""
        for _ in range(max_slices):
            active = []
            for plugin in self.registry.plugins:
                if not self._queues[plugin.id]:
                    self._refill(plugin)
                if self._queues[plugin.id]:
                    active.append(plugin)
            plugin = self._pick(active)
            if plugin is None:
                return
            request = self._next_request(plugin)
            if request is None:
                continue
            yield plugin, request


__all__ = ["PluginStats", "Scheduler", "SchedulerStats", "Source"]