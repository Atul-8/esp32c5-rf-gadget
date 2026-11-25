"""`output/png.py` 的测试：PNG 结构合法、点阵文字、像素可回读。"""
from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from output.png import GLYPHS, SIGNATURE, Canvas, encode_png, read_png_header, text_width, write_png


def _decode_rgb(path: Path) -> tuple[int, int, bytes]:
    """最小 PNG 解码（只认 filter 0 / RGB8），把像素摊出来给测试比对。"""
    data = path.read_bytes()
    assert data.startswith(SIGNATURE)
    at = 8
    width = height = 0
    idat = bytearray()
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        tag = data[at + 4 : at + 8]
        body = data[at + 8 : at + 8 + length]
        if tag == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
        elif tag == b"IDAT":
            idat += body
        at += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = width * 3 + 1
    pixels = bytearray()
    for y in range(height):
        line = raw[y * stride : (y + 1) * stride]
        assert line[0] == 0, f"只支持 filter 0，第 {y} 行是 {line[0]}"
        pixels += line[1:]
    return width, height, bytes(pixels)


class PngEncodeTest(unittest.TestCase):
    def test_signature_and_header(self) -> None:
        data = encode_png(3, 2, bytes([0, 0, 0] * 6))
        self.assertTrue(data.startswith(SIGNATURE))
        self.assertEqual(struct.unpack(">II", data[16:24]), (3, 2))
        self.assertEqual(data[24], 8)  # bit depth
        self.assertEqual(data[25], 2)  # color type RGB
        self.assertTrue(data.endswith(b"IEND\xaeB`\x82"))

    def test_chunk_crc_is_valid(self) -> None:
        data = encode_png(2, 2, bytes(range(12)))
        at = 8
        seen = 0
        while at < len(data):
            (length,) = struct.unpack(">I", data[at : at + 4])
            tag = data[at + 4 : at + 8]
            body = data[at + 8 : at + 8 + length]
            (crc,) = struct.unpack(">I", data[at + 8 + length : at + 12 + length])
            self.assertEqual(crc, zlib.crc32(tag + body) & 0xFFFFFFFF, tag)
            at += 12 + length
            seen += 1
        self.assertEqual(seen, 3)  # IHDR + IDAT + IEND

    def test_size_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            encode_png(4, 4, bytes(10))

    def test_pixels_roundtrip(self) -> None:
        rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.png"
            write_png(path, 2, 2, rgb)
            width, height, pixels = _decode_rgb(path)
            self.assertEqual((width, height), (2, 2))
            self.assertEqual(pixels, rgb)
            self.assertEqual(read_png_header(path)["color_type"], 2)


class CanvasTest(unittest.TestCase):
    def test_fill_and_read_back(self) -> None:
        canvas = Canvas(4, 3, bg=(0, 0, 0))
        canvas.fill_rect(1, 1, 2, 1, (10, 20, 30))
        with tempfile.TemporaryDirectory() as tmp:
            path = canvas.save(Path(tmp) / "c.png")
            _w, _h, pixels = _decode_rgb(Path(path))
        self.assertEqual(pixels[0:3], b"\x00\x00\x00")
        self.assertEqual(pixels[(1 * 4 + 1) * 3 : (1 * 4 + 1) * 3 + 3], b"\x0a\x14\x1e")
        self.assertEqual(pixels[(1 * 4 + 3) * 3 : (1 * 4 + 3) * 3 + 3], b"\x00\x00\x00")

    def test_fill_rect_clips(self) -> None:
        canvas = Canvas(3, 3, bg=(0, 0, 0))
        canvas.fill_rect(-5, -5, 100, 100, (1, 2, 3))
        self.assertEqual(canvas.pixels[:3], b"\x01\x02\x03")
        canvas.fill_rect(10, 10, 5, 5, (9, 9, 9))  # 完全越界，不该抛错

    def test_text_draws_pixels(self) -> None:
        canvas = Canvas(40, 10, bg=(0, 0, 0))
        end = canvas.text(1, 1, "2.4G", (255, 255, 255))
        self.assertGreater(end, 1)
        self.assertEqual(end, 1 + text_width("2.4G") + 1)  # 末字符后留一个 gap
        self.assertGreater(sum(1 for i in range(0, len(canvas.pixels), 3) if canvas.pixels[i]), 10)

    def test_text_unknown_char_falls_back_to_space(self) -> None:
        canvas = Canvas(20, 10, bg=(0, 0, 0))
        canvas.text(0, 0, "中", (255, 255, 255))  # 未收录 → 不抛错
        self.assertEqual(sum(1 for i in range(0, len(canvas.pixels), 3) if canvas.pixels[i]), 0)

    def test_glyphs_are_5x7(self) -> None:
        for char, rows in GLYPHS.items():
            self.assertEqual(len(rows), 7, char)
            for row in rows:
                self.assertEqual(len(row), 5, char)
                self.assertTrue(set(row) <= {"0", "1"}, char)

    def test_bad_size(self) -> None:
        with self.assertRaises(ValueError):
            Canvas(0, 10)


if __name__ == "__main__":
    unittest.main()