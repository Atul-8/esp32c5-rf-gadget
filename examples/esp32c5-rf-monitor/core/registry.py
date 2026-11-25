"""模块契约与自动注册。

``ModePlugin`` 是全项目唯一的接口契约：五个功能各自实现自己的子类，
放在 ``modes/`` 下即可被 ``discover()`` 自动发现，**不需要改任何共享文件**。
这也是五个功能分支合并时零冲突的保证。
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .snapshot import Snapshot


@dataclass(frozen=True)
class ScanRequest:
    """一次扫描请求：驻留某个频点/带宽一段时间。

    ``dwell_ms`` 由固件 ``SPEC <ms> ...`` 解释；固件只接受 100..60000。
    """

    freq_hz: int
    bw_hz: int = 20_000_000
    dwell_ms: int = 100
    bins: int = 512
    detector: int = 0  # 0=均值功率，1=最大保持

    def __post_init__(self) -> None:
        if not 100 <= self.dwell_ms <= 60_000:
            raise ValueError(f"dwell_ms 超出固件范围：{self.dwell_ms}")
        if not 100_000_000 <= self.freq_hz <= 6_000_000_000:
            raise ValueError(f"频点超出固件范围：{self.freq_hz}")
        if self.detector not in (0, 1):
            raise ValueError("detector 只能是 0 或 1")

    def key(self) -> tuple[int, int, int, int, int]:
        return (self.freq_hz, self.bw_hz, self.dwell_ms, self.bins, self.detector)

    def describe(self) -> str:
        return f"{self.freq_hz/1e6:.0f}MHz/{self.bw_hz/1e6:.0f}MHz/{self.dwell_ms}ms"


@dataclass
class Output:
    """``produce()`` 的统一产出结构（STOP 门禁时人类调用的就是它）。"""

    kind: str  # table / figure / report / archive / alert
    title: str
    columns: Sequence[str] = ()
    rows: Sequence[Sequence[Any]] = ()
    files: Sequence[str] = ()
    notes: Sequence[str] = ()
    payload: Any = None
    created_at: float | None = None

    def render(self) -> str:
        from output.format import ascii_table

        lines: list[str] = [f"[{self.kind}] {self.title}"]
        if self.columns and self.rows:
            lines.append(ascii_table(self.columns, self.rows))
        for note in self.notes:
            lines.append(f"! {note}")
        for path in self.files:
            lines.append(f"-> {path}")
        return "\n".join(lines)


class ModePlugin:
    """功能模块契约（core/registry.py 定义，功能分支只实现、不修改）。"""

    id: str = ""
    name: str = ""
    slice_weight: float = 0.0

    # ---- 必须实现 --------------------------------------------------------
    def scan_requests(self) -> list[ScanRequest]:
        """本模块当前想要的扫描计划。返回空列表 = 本模块暂时不占用射频。"""
        raise NotImplementedError

    def on_snapshot(self, snap: Snapshot) -> None:
        """收到快照时的增量处理（统计/检测/存储）。"""
        raise NotImplementedError

    def produce(self) -> Output:
        """产出物：表格/图/告警/抓档。"""
        raise NotImplementedError

    # ---- 可选覆盖 --------------------------------------------------------
    def reset(self) -> None:
        """清空本模块的累积状态（重复演示/多次 produce 之间调用）。"""

    def summary(self) -> dict[str, Any]:
        """调度统计用的简短描述，默认给 id/名字/权重。"""
        return {"id": self.id, "name": self.name, "slice_weight": self.slice_weight}


class RegistryError(RuntimeError):
    pass


class Registry:
    """按 id 稳定排序的插件表；``discover()`` 负责扫描 ``modes/``。"""

    def __init__(self) -> None:
        self._plugins: dict[str, ModePlugin] = {}
        self.errors: list[str] = []

    # ---- 注册 ------------------------------------------------------------
    def register(self, plugin: ModePlugin) -> ModePlugin:
        if not isinstance(plugin, ModePlugin):
            raise RegistryError(f"不是 ModePlugin：{plugin!r}")
        if not plugin.id:
            raise RegistryError(f"{type(plugin).__name__} 缺少 id")
        if plugin.slice_weight < 0:
            raise RegistryError(f"{plugin.id} 的 slice_weight 不能为负")
        if plugin.id in self._plugins:
            raise RegistryError(f"重复的模块 id：{plugin.id}")
        self._plugins[plugin.id] = plugin
        return plugin

    def register_class(self, cls: type[ModePlugin]) -> ModePlugin:
        return self.register(cls())

    # ---- 查询 ------------------------------------------------------------
    @property
    def plugins(self) -> list[ModePlugin]:
        return [self._plugins[k] for k in sorted(self._plugins)]

    def get(self, plugin_id: str) -> ModePlugin:
        return self._plugins[plugin_id]

    def __len__(self) -> int:
        return len(self._plugins)

    def __iter__(self) -> Iterable[ModePlugin]:
        return iter(self.plugins)

    def weights(self) -> dict[str, float]:
        return {p.id: p.slice_weight for p in self.plugins}

    def total_weight(self) -> float:
        return sum(p.slice_weight for p in self.plugins)


def _all_subclasses(cls: type) -> list[type]:
    found: list[type] = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_all_subclasses(sub))
    return found


def discover(
    package: str = "modes",
    registry: Registry | None = None,
    strict_module_errors: bool = False,
) -> Registry:
    """导入 ``modes/`` 下所有可导入模块，注册其中的 ModePlugin 子类。

    找不到 ``modes`` 包时返回空表（阶段 0 就是这种状态），不抛异常。
    单个模块导入失败会记入 ``registry.errors``，不阻塞其它模块。
    """
    reg = registry if registry is not None else Registry()
    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        reg.errors.append(f"未找到模块包 {package}（阶段 0 允许为空）")
        return reg
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{package}.{info.name}")
        except Exception as exc:  # noqa: BLE001 - 隔离坏模块，保证其它功能可用
            message = f"{package}.{info.name} 导入失败：{type(exc).__name__}: {exc}"
            reg.errors.append(message)
            if strict_module_errors:
                raise RegistryError(message) from exc
    prefix = f"{package}."
    for cls in _all_subclasses(ModePlugin):
        if not getattr(cls, "id", "") or inspect.isabstract(cls):
            continue
        if not cls.__module__.startswith(prefix):
            continue  # 只收 modes/ 下的实现，测试替身不参与自动注册
        try:
            reg.register_class(cls)
        except (RegistryError, TypeError) as exc:
            reg.errors.append(f"{cls.__module__}.{cls.__name__} 注册失败：{exc}")
    return reg


__all__ = [
    "ModePlugin",
    "Output",
    "Registry",
    "RegistryError",
    "ScanRequest",
    "discover",
]