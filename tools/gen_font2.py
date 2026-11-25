# -*- coding: utf-8 -*-
"""rd_font2.c / rd_font2.h 生成器（2026-11-25 重建入库；原 gen_font2.py 已丢失）。

字形来源：解析仓库里现存的 rd_font2.c（64 字符 ASCII 32..95），
在其后扩充小写 'z'(122) —— 用户要求圆心行写正确大小写 "MHz"。
表改为连续 32..122（RD_FONT2_CHARS=91），96..121 填空字形（永不绘制）。

小写 z 设计（与大写 Z 同风格：顶/底横杠 + 右上→左下 2px 均匀阶梯斜线，
底部对齐大写基线）：
  small 10x18：基线 r13，x-height 8 行 r06..r13
  big   16x30：基线 r20，x-height 12 行 r09..r20

用法：python tools/gen_font2.py   （在仓库根执行；勿手改 rd_font2.c）
"""
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "main/common/rd_font2.c"
HDR = ROOT / "main/common/rd_font2.h"

SMALL_W, SMALL_H = 10, 18
BIG_W, BIG_H = 16, 30
FIRST = 32
LAST = 122                      # 'z'
CHARS = LAST - FIRST + 1        # 91

Z_SMALL = [0] * SMALL_H
for r, v in ((6, 0x00FC), (7, 0x0006), (8, 0x000C), (9, 0x0018),
             (10, 0x0030), (11, 0x0060), (12, 0x00C0), (13, 0x00FC)):
    Z_SMALL[r] = v

Z_BIG = [0] * BIG_H
for r, v in ((9, 0x3FF8), (10, 0x001C), (11, 0x0038), (12, 0x0070),
             (13, 0x00E0), (14, 0x01C0), (15, 0x0380), (16, 0x0700),
             (17, 0x0E00), (18, 0x1C00), (19, 0x3800), (20, 0x3FF8)):
    Z_BIG[r] = v


def parse_table(src: str, name: str, rows: int) -> list[list[int]]:
    m = re.search(rf"const uint16_t {name}\[RD_FONT2_CHARS\]\[{rows}\] = \{{(.*?)\n\}};", src, re.S)
    assert m, f"{name} 表没解析到"
    out = []
    for line in m.group(1).splitlines():
        vals = re.findall(r"0x[0-9A-Fa-f]{4}", line)
        if vals:
            assert len(vals) == rows, f"{name} 行字段数 {len(vals)} != {rows}: {line[:60]}"
            out.append([int(v, 16) for v in vals])
    assert len(out) == 64, f"{name} 解析到 {len(out)} 字形 != 64"
    return out


def fmt_table(name: str, glyphs: list[list[int]], w: int) -> str:
    lines = [f"const uint16_t {name}[RD_FONT2_CHARS][{'RD_FONT2_SH' if 'small' in name else 'RD_FONT2_BH'}] = {{"]
    for i, g in enumerate(glyphs):
        ch = FIRST + i
        tag = f"'\\x{ch:02x}'" if ch < 0x7F else "'z'"
        body = ", ".join(f"0x{v:04X}" for v in g)
        lines.append(f"    {{ {body} }},  /* {tag} */")
    lines.append("};")
    return "\n".join(lines)


def main() -> None:
    src = io.open(SRC, encoding="utf-8", newline="").read()
    small = parse_table(src, "rd_font2_small", SMALL_H)
    big = parse_table(src, "rd_font2_big", BIG_H)

    # 扩表：32..95 原样 + 96..121 空 + 122 = z
    blank_s, blank_b = [0] * SMALL_H, [0] * BIG_H
    small += [blank_s] * (LAST - 96 + 1)
    big += [blank_b] * (LAST - 96 + 1)
    small[-1], big[-1] = Z_SMALL, Z_BIG
    assert len(small) == CHARS and len(big) == CHARS

    banner = ("/* 自动生成，勿手改（generator: tools/gen_font2.py）。\n"
              " * 真实字形点阵：8x16 正文、16x30 标题。\n"
              " * 旧方案是把 5x7 逐像素用 rd_rect 放大，实物上是马赛克，已废弃。\n"
              " * 2026-11-25 扩充小写 z（圆心行 \"MHz\" 用）：表 32..122 连续，\n"
              " * 96..121 为空字形；z 与大写 Z 同风格（横杠 + 2px 阶梯斜线），\n"
              " * 底部对齐大写基线。 */")
    out = (banner + "\n#include \"rd_font2.h\"\n\n"
           + fmt_table("rd_font2_small", small, SMALL_W) + "\n\n"
           + fmt_table("rd_font2_big", big, BIG_W) + "\n")
    nl = "\r\n" if "\r\n" in src else "\n"
    io.open(SRC, "w", encoding="utf-8", newline="").write(out.replace("\n", nl))

    hdr_src = io.open(HDR, encoding="utf-8", newline="").read()
    hdr_new = hdr_src.replace("#define RD_FONT2_CHARS 64", f"#define RD_FONT2_CHARS {CHARS}")
    assert hdr_new != hdr_src, "rd_font2.h 的 RD_FONT2_CHARS 没改到"
    io.open(HDR, "w", encoding="utf-8", newline="").write(hdr_new)
    print(f"OK: rd_font2.c 重建 {CHARS} 字形（+z）、rd_font2.h RD_FONT2_CHARS={CHARS}")

    # 自证：z 与相邻字形墨迹行范围
    def ink_rows(g, h):
        return [i for i, v in enumerate(g) if v]
    print(f"  z small 墨迹行 {ink_rows(Z_SMALL, SMALL_H)[0]}..{ink_rows(Z_SMALL, SMALL_H)[-1]}"
          f"（大写 Z 为 3..13）")
    print(f"  z big   墨迹行 {ink_rows(Z_BIG, BIG_H)[0]}..{ink_rows(Z_BIG, BIG_H)[-1]}"
          f"（大写 G 为 3..20）")


if __name__ == "__main__":
    sys.exit(main())
