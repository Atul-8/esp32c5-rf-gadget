"""应用入口（demo = mock 全链路，live = 真机/假固件链路）。"""
from __future__ import annotations

from typing import Any


def bind_store(registry: Any, store: Any) -> list[str]:
    """把 ``store`` 交给「自己声明了 ``store`` 属性」的功能模块，返回绑定的模块 id。

    为什么要这一层：功能模块（``modes/``）不 import ``core.store``，只在自己的实例上
    留一个 ``store`` 槽位；由 app 层注入，模块就能 ``store.add_event(...)`` 落告警/事件。
    这样 ``modes/`` 与存储层解耦，也方便单测塞假 store。
    """
    bound: list[str] = []
    if store is None:
        return bound
    for plugin in getattr(registry, "plugins", []):
        if hasattr(plugin, "store"):
            try:
                if getattr(plugin, "store", None) is None:
                    plugin.store = store
                    bound.append(plugin.id)
            except Exception:  # noqa: BLE001 - 只读属性的模块跳过即可
                continue
    return bound


def bind_capture(
    registry: Any,
    capture_fn: Any,
    out_dir: str | None = None,
) -> list[str]:
    """把「真抓档函数」交给支持抓档的模块（有 ``capture_fn`` 槽位的），返回模块 id。

    ``capture_fn(freq_hz, samples, rate_code, bits) -> 抓档对象``：真机是
    ``link.capture_iq``，mock/demo 是 ``app.capture_link.synthetic_capture``。
    模块自己不发串口命令 —— 真正的抓档由 ``CaptureAwareSource`` 在两次扫描之间执行。
    """
    bound: list[str] = []
    if capture_fn is None:
        return bound
    for plugin in getattr(registry, "plugins", []):
        if not hasattr(plugin, "capture_fn"):
            continue
        try:
            plugin.capture_fn = capture_fn
            if out_dir is not None and hasattr(plugin, "out_dir"):
                plugin.out_dir = out_dir
            bound.append(plugin.id)
        except Exception:  # noqa: BLE001 - 只读属性的模块跳过
            continue
    return bound


__all__ = ["bind_capture", "bind_store"]