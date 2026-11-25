"""地基层：串口协议、快照 schema、调度器、存储、分类器接口、模块注册表。

功能分支（modes/*.py）只调用本包，不修改本包任何文件。
"""
from .classifier import Classification, SignalFeatures, classify
from .registry import ModePlugin, Output, Registry, ScanRequest, discover
from .scheduler import Scheduler, SchedulerStats, Source
from .snapshot import Snapshot, SnapshotError, decode_spectrum_frame
from .store import Store

__all__ = [
    "Classification",
    "ModePlugin",
    "Output",
    "Registry",
    "ScanRequest",
    "Scheduler",
    "SchedulerStats",
    "SignalFeatures",
    "Snapshot",
    "SnapshotError",
    "Source",
    "Store",
    "classify",
    "decode_spectrum_frame",
    "discover",
]