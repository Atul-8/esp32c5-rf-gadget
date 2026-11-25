"""输出基础件：纯文本表格与常用格式化（无第三方依赖）。

各功能模块自带的输出实现（信道表、PNG/HTML 瀑布、告警日志、抓档清单）
都复用这里的表格/单位格式化，避免每处重复手写对齐逻辑。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def fmt_hz(hz: float) -> str:
    if hz >= 1e9:
        return f"{hz / 1e9:.3f} GHz"
    if hz >= 1e6:
        return f"{hz / 1e6:.3f} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:.1f} kHz"
    return f"{hz:.0f} Hz"


def fmt_hz_short(hz: float) -> str:
    """紧凑写法：2442MHz / 5785MHz / 2402MHz。"""
    return f"{hz / 1e6:.0f}MHz" if hz >= 1e6 else f"{hz:.0f}Hz"


def fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def fmt_duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.2f}h"


def bar(value: float, maximum: float = 100.0, width: int = 20) -> str:
    """把 0..maximum 的量画成等宽方块条（表格里一眼看出强弱）。"""
    if width <= 0 or not math.isfinite(maximum) or maximum <= 0:
        return ""
    filled = int(round(max(0.0, min(1.0, value / maximum)) * width))
    return "█" * filled + "·" * (width - filled)


def _cell(value: object) -> str:
    if isinstance(value, float):
        if math.isfinite(value):
            return f"{value:.1f}"
        return "n/a"
    return str(value)


def _display_width(text: str) -> int:
    """中文/全角算 2 列，保证表格对齐。"""
    width = 0
    for char in text:
        code = ord(char)
        wide = (
            0x1100 <= code <= 0x115F
            or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE6F
            or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6
            or 0x1F300 <= code <= 0x1FAFF
            # 方块条与常见符号按 1 列处理，避免对齐错位
        )
        width += 2 if wide else 1
    return width


def _pad(text: str, width: int, align: str) -> str:
    deficit = width - _display_width(text)
    if deficit <= 0:
        return text
    if align == "right":
        return " " * deficit + text
    if align == "center":
        left = deficit // 2
        return " " * left + text + " " * (deficit - left)
    return text + " " * deficit


def ascii_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    aligns: Sequence[str] | None = None,
    title: str | None = None,
) -> str:
    """生成等宽字符表格，中文按双列宽度对齐。"""
    headers = [str(h) for h in headers]
    body = [[_cell(v) for v in row] for row in rows]
    if aligns is None:
        aligns = ["left"] + ["right"] * (len(headers) - 1)
    if len(aligns) != len(headers):
        aligns = ["left"] * len(headers)
    widths = [_display_width(h) for h in headers]
    for row in body:
        for index, cell in enumerate(row[: len(widths)]):
            widths[index] = max(widths[index], _display_width(cell))
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines: list[str] = []
    if title:
        lines.append(title)
    lines.append(sep)
    lines.append("| " + " | ".join(_pad(h, w, "center") for h, w in zip(headers, widths)) + " |")
    lines.append(sep)
    for row in body:
        cells = [
            _pad(row[index] if index < len(row) else "", width, aligns[index])
            for index, width in enumerate(widths)
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append(sep)
    return "\n".join(lines)


def kv_block(title: str, pairs: Sequence[tuple[str, object]]) -> str:
    """简单的“键: 值”块，用于验收包里的摘要输出。"""
    width = max((_display_width(str(k)) for k, _ in pairs), default=0)
    lines = [f"== {title} =="]
    lines.extend(f"{_pad(str(k), width, 'left')} : {v}" for k, v in pairs)
    return "\n".join(lines)


__all__ = [
    "ascii_table",
    "bar",
    "fmt_duration",
    "fmt_hz",
    "fmt_hz_short",
    "fmt_pct",
    "kv_block",
]