"""最小 PNG 写出器 + 5x7 点阵字（纯标准库）。

为什么自己写：本机没有 numpy / matplotlib / PIL（见 ``todo.md`` 环境约束），
而任务 2 的产出物要求是 PNG。所以这里按 PNG 规范直接拼字节：

    签名 + IHDR + IDAT(zlib) + IEND   ，颜色类型 2（RGB8），filter 恒为 0

用法::

    canvas = Canvas(720, 400)
    canvas.fill_rect(0, 0, 720, 20, (20, 24, 32))
    canvas.text(6, 6, "2.4G ISM", (230, 230, 230))
    write_png("out.png", canvas.width, canvas.height, canvas.pixels)

字模只有 5x7 大小，够画坐标刻度和标题；要中文请落到 HTML 里（见 ``heatmap.py``）。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIGNATURE = b"\x89PNG\r\n\x1a\n"
COLOR_RGB = 2
BIT_DEPTH = 8

Color = tuple[int, int, int]

# 5x7 点阵（每行 5 位，共 7 行）；只收 ASCII 里画刻度/标题真正用到的字符。
GLYPHS: dict[str, tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    ".": ("00000", "00000", "00000", "01100", "01100", "00000", "00000"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "%": ("10001", "10010", "00100", "01000", "10001", "00000", "00000"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "a": ("00000", "00000", "01110", "00001", "01111", "10001", "01111"),
    "d": ("00001", "00001", "01101", "10011", "10001", "10011", "01101"),
    "e": ("00000", "00000", "01110", "10001", "11111", "10000", "01110"),
    "g": ("00000", "01111", "10001", "10001", "01111", "00001", "01110"),
    "i": ("00100", "00000", "01100", "00100", "00100", "00100", "01110"),
    "k": ("10000", "10000", "10010", "10100", "11000", "10100", "10010"),
    "m": ("00000", "00000", "11010", "10101", "10101", "10101", "10101"),
    "n": ("00000", "00000", "11110", "10001", "10001", "10001", "10001"),
    "o": ("00000", "00000", "01110", "10001", "10001", "10001", "01110"),
    "p": ("00000", "00000", "11110", "10001", "11110", "10000", "10000"),
    "r": ("00000", "00000", "10110", "11001", "10000", "10000", "10000"),
    "s": ("00000", "00000", "01111", "10000", "01110", "00001", "11110"),
    "t": ("01000", "01000", "11100", "01000", "01000", "01010", "00100"),
    "u": ("00000", "00000", "10001", "10001", "10001", "10011", "01101"),
    "w": ("00000", "00000", "10001", "10001", "10101", "10101", "01010"),
    "z": ("00000", "00000", "11111", "00010", "00100", "01000", "11111"),
}

GLYPH_W = 5
GLYPH_H = 7
GLYPH_GAP = 1


def text_width(text: str, scale: int = 1) -> int:
    """文本渲染宽度（像素）；空串为 0。"""
    if not text:
        return 0
    return (len(text) * (GLYPH_W + GLYPH_GAP) - GLYPH_GAP) * scale


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def encode_png(width: int, height: int, rgb: bytes | bytearray) -> bytes:
    """把逐行 RGB8 像素压成完整 PNG 字节流。"""
    stride = width * 3
    if len(rgb) != stride * height:
        raise ValueError(f"像素长度不符：期望 {stride * height}，收到 {len(rgb)}")
    raw = bytearray()
    view = memoryview(bytes(rgb))
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += view[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, BIT_DEPTH, COLOR_RGB, 0, 0, 0)
    return b"".join(
        (
            SIGNATURE,
            _chunk(b"IHDR", ihdr),
            _chunk(b"IDAT", zlib.compress(bytes(raw), 6)),
            _chunk(b"IEND", b""),
        )
    )


def write_png(path: str | Path, width: int, height: int, rgb: bytes | bytearray) -> str:
    """写 PNG 文件，返回路径字符串。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode_png(width, height, rgb))
    return str(target)


def read_png_header(path: str | Path) -> dict[str, int]:
    """只读 IHDR（给测试用：确认图片真是 PNG 且尺寸对）。"""
    data = Path(path).read_bytes()
    if not data.startswith(SIGNATURE):
        raise ValueError("不是 PNG 文件")
    width, height, depth, ctype = struct.unpack(">IIBB", data[16:26])
    return {"width": width, "height": height, "bit_depth": depth, "color_type": ctype}


class Canvas:
    """极简 RGB 画布：填充矩形 + 点阵文字，够拼瀑布图。"""

    def __init__(self, width: int, height: int, bg: Color = (8, 10, 16)) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("画布尺寸必须为正")
        self.width = int(width)
        self.height = int(height)
        self.pixels = bytearray(bytes(bg) * (self.width * self.height))

    # ---- 基本图元 --------------------------------------------------------
    def set(self, x: int, y: int, color: Color) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            at = (y * self.width + x) * 3
            self.pixels[at : at + 3] = bytes(color)

    def fill_rect(self, x: int, y: int, w: int, h: int, color: Color) -> None:
        if w <= 0 or h <= 0:
            return
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            at = (yy * self.width + x0) * 3
            self.pixels[at : at + len(row)] = row

    def frame_rect(self, x: int, y: int, w: int, h: int, color: Color) -> None:
        self.fill_rect(x, y, w, 1, color)
        self.fill_rect(x, y + h - 1, w, 1, color)
        self.fill_rect(x, y, 1, h, color)
        self.fill_rect(x + w - 1, y, 1, h, color)

    def text(self, x: int, y: int, text: str, color: Color = (225, 228, 232), scale: int = 1) -> int:
        """画点阵文字，返回结束 x 坐标。未收录字符按空格处理（不抛错）。"""
        cursor = x
        for ch in text:
            glyph = GLYPHS.get(ch) or GLYPHS.get(ch.upper()) or GLYPHS[" "]
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit != "1":
                        continue
                    self.fill_rect(
                        cursor + col * scale,
                        y + row * scale,
                        scale,
                        scale,
                        color,
                    )
            cursor += (GLYPH_W + GLYPH_GAP) * scale
        return cursor

    # ---- 输出 ------------------------------------------------------------
    def save(self, path: str | Path) -> str:
        return write_png(path, self.width, self.height, self.pixels)


__all__ = [
    "Canvas",
    "Color",
    "GLYPHS",
    "encode_png",
    "read_png_header",
    "text_width",
    "write_png",
]