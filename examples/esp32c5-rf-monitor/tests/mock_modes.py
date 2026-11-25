"""测试用插件：把 ModePlugin 契约跑通，不实现任何真功能。

``ProbePlugin`` 用于阶段 0 的"注册→调度→收快照→入库"链路演示；
``make_plugins`` 用于验证调度器的权重配比。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from core.registry import ModePlugin, Output, ScanRequest
from core.snapshot import Snapshot


@dataclass
class ProbePlugin(ModePlugin):
    """按给定计划扫描并记录收到的快照。"""

    id: str = "probe"
    name: str = "演示探针"
    slice_weight: float = 1.0
    requests: Sequence[ScanRequest] = field(default_factory=lambda: (ScanRequest(2_442_000_000, 80_000_000, 100, 512),))
    snapshots: list[Snapshot] = field(default_factory=list)
    produced: int = 0

    def scan_requests(self) -> list[ScanRequest]:
        return list(self.requests)

    def on_snapshot(self, snap: Snapshot) -> None:
        self.snapshots.append(snap)

    def reset(self) -> None:
        self.snapshots.clear()

    def summary(self) -> dict[str, object]:
        peak = max(self.snapshots, key=lambda s: s.peak()[1]).peak() if self.snapshots else (0.0, 0.0)
        return {
            "id": self.id,
            "name": self.name,
            "slice_weight": self.slice_weight,
            "snapshots": len(self.snapshots),
            "peak_db": peak[1],
            "peak_hz": peak[0],
        }

    def produce(self) -> Output:
        self.produced += 1
        rows = []
        for snap in self.snapshots[-12:]:
            peak_hz, peak_db = snap.peak()
            rows.append(
                [
                    snap.ts_iso,
                    f"{snap.center_hz / 1e6:.1f}",
                    f"{snap.bw_hz / 1e6:.0f}",
                    snap.bin_count,
                    f"{snap.noise_floor_db:.0f}",
                    f"{peak_db:.0f}",
                    f"{peak_hz / 1e6:.2f}",
                ]
            )
        return Output(
            kind="table",
            title=f"{self.name} 最近 {len(rows)} 帧（演示探针，非真功能）",
            columns=["时间", "中心MHz", "带宽MHz", "bins", "噪声底", "峰值", "峰值MHz"],
            rows=rows,
            notes=[
                "阶段 0 演示用插件：只证明 注册→调度→收快照→入库 链路通，不代表任何功能已实现",
                "固件为单射频分时扫描：帧与帧之间有接收空档，不是连续实时",
            ],
        )


def make_plugin(plugin_id: str, weight: float, freq_hz: int = 2_442_000_000, bw_hz: int = 80_000_000) -> ProbePlugin:
    return ProbePlugin(
        id=plugin_id,
        name=f"探针-{plugin_id}",
        slice_weight=weight,
        requests=(ScanRequest(freq_hz, bw_hz, 100, 512),),
    )


def make_plugins(weights: dict[str, float]) -> list[ProbePlugin]:
    return [make_plugin(pid, weight) for pid, weight in weights.items()]


__all__ = ["ProbePlugin", "make_plugin", "make_plugins"]