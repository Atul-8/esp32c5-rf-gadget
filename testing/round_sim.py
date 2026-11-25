#!/usr/bin/env python
"""240x240 圆屏像素同构仿真器（真板样本重放）。

与固件同构，不是“看起来像”：
  * 调色板直接解析 ``main/common/rd_pal.c``（256 条 RGB565）
  * 三档字体位图直接解析 ``rd_font2.c`` / ``rd_font3.c``
  * 角度映射与 ``draw_radial_k`` 一致（``sn[]/cs[]``，θ=2πb/176，Q14 +8192）
  * 填充走 Bresenham 直线（``isqrt_i`` 行填充的等价直线示意）

真板没有整屏读回能力（``RD DUMP`` 只回文本、``RD ROW`` 只回一行），
所以预览只能在电脑侧复刻渲染；为可核对，提供 ``--shot`` 出图与
``RD ROW`` 逐行比对脚本配合使用。

用法::

    python testing/round_sim.py                     # GUI，新皮肤
    python testing/round_sim.py                     # GUI，默认就是固件那套
    python testing/round_sim.py --skin new          # GUI，未上固件的重设计原型
    python testing/round_sim.py --shot build/sim    # 离屏出三页 PNG 后退出
    python testing/round_sim.py --view 1 --zoom 2   # 指定首页与缩放

GUI 按键：1/2/3 切页、N 换皮、B 换频段、D 内环开关、←/→ 换帧、
↑/↓ 翻 CHART 分页、空格暂停轮播、S 存 PNG。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "main" / "common"
TRACE = ROOT / "build" / "board_trace3.json"
# 占空比 trace（testing/record_occ_trace.py 产出）：board_trace3 里只有 24 列
# 聚合值，算不出「帧内 p50−p25」，所以圆心那道读数必须另采一份逐帧原始码。
OCC_TRACE = ROOT / "build" / "occ_trace.json"
# 与固件 round_ui.c 的 RD_OCC_LIGHT / RD_OCC_CROWD / RD_OCC_HEAVY 同值（%）。
OCC_LIGHT = 5
OCC_CROWD = 15
OCC_HEAVY = 35

RD_W = 240
RD_H = 240
RD_GRID = 352
RD_SEG_24G = 44
RD_SEG_U1 = 53    # UNII-1 5150~5250
RD_SEG_U2A = 53   # UNII-2 前半 5250~5350（2A）
RD_SEG_U2C = 136  # UNII-2 后半 5470~5725（2C）
RD_SEG_U3 = 66    # UNII-3 5725~5850
RD_R_OUT = 116
RD_HEAT_HI = 108
RD_HEAT_LO = 88
RD_TICK_LO = 110
RD_TICK_HI = 115
RD_R_CORE = 74
RD_TICK_STEP = 22
RD_TICK_MAJOR = 88
RD_BLOCKS = 176
RD_HEAT_FULL = 96
RD_CH_PER_PAGE = 5                # 6 -> 5：给表头(y=50)与页脚(y=172)让位
RD_WF_N = 64
RD_SPEC_N = 64

VIEW_RING = 0
VIEW_CHART = 1
VIEW_WFALL = 2
VIEW_COUNT = 3

CH_MHZ = [2412, 2417, 2422, 2427, 2432, 2437, 2442, 2447, 2452, 2457, 2462,
          2467, 2472, 5180, 5200, 5220, 5240, 5260, 5280, 5300, 5320, 5500,
          5520, 5540, 5560, 5580, 5600, 5620, 5640, 5660, 5680, 5700, 5720,
          5745, 5765, 5785, 5805, 5825]

# 8 级梯子（ΔdB 每级 6 码）
LVL = [0, 0x4277, 0x43DC, 0x453D, 0x2E1A, 0x26B5, 0x3710, 0x6F2A, 0xCF26]

BAND_24G = 0
BAND_5GL = 1   # 仅供 render_ring_new / _ring_band_floor（dev 沙盒皮肤）使用
BAND_5GH = 2

BAND2_NAME = ("2.4G", "5G")
BAND2_COLOR = (0x2F10, 0x453D)
# 五段频段色环用的颜色/名字（2.4G + FCC 四块 UNII-1/2A/2C/3，2026-11-25 起
# 2A/2C 拆开异色），与固件 s_band_name/s_band_col 同值
BAND_NAME = ("2.4G", "UNII-1", "UNII-2A", "UNII-2C", "UNII-3")
BAND_COL = (0x2F10, 0x453D, 0xA17E, 0xB060, 0xAB40)
BAND_COLOR = (0x2F10, 0x453D, 0xA17E, 0xFB60, 0xF3C5)  # new 皮肤标签亮色（2C 亮橙红）
GRP_NAME = ("2.4G", "5G-LOW", "5G-HIGH")  # 页 2 信道组标签（U1+2A / 2C+U3），不是法规块名

C_BG = 0x0000
C_RING_BG = 0x0841
C_TRACK = 0x18C3
C_FRAME = 0x4208
C_MINOR = 0x5AEB
C_DIM = 0x8410
C_LABEL = 0xC618
C_MAIN = 0xFFFF

GRP_START = (0, 13, 21)
GRP_LEN = (13, 8, 17)


# --------------------------------------------------------------------------
# 资源解析（与固件同一份源文件）
# --------------------------------------------------------------------------
def load_palette() -> np.ndarray:
    txt = (SRC / "rd_pal.c").read_text(encoding="utf-8")
    body = txt[txt.index("{") + 1: txt.rindex("}")]
    vals = [int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]{4})", body)]
    assert len(vals) == 256, f"调色板条目数 {len(vals)}"
    return np.array(vals, dtype=np.uint16)


def _parse_font(path: Path, name: str, rows: int) -> list[list[int]]:
    txt = path.read_text(encoding="utf-8")
    m0 = re.search(re.escape(name) + r"\s*\[", txt)
    if not m0:
        raise SystemExit(f"{path.name} 里找不到数组 {name}")
    start = txt.index("{", m0.end())
    end = txt.index("\n};", start)
    chunk = txt[start:end]
    out = []
    for m in re.finditer(r"\{\s*([^{}]*)\}", chunk):
        vals = re.findall(r"0x([0-9A-Fa-f]+)", m.group(1))
        if len(vals) != rows:
            continue
        out.append([int(v, 16) for v in vals])
    return out


class Font:
    def __init__(self, glyphs: list[list[int]], w: int, h: int):
        self.g = glyphs
        self.w = w
        self.h = h

    def draw(self, scr: "Screen", x: int, y: int, s: str, color: int) -> None:
        for i, ch in enumerate(s):
            ci = ord(ch) - 32
            if not 0 <= ci < len(self.g):
                continue            # 固件同样静默丢弃，但笔位照前进
            g = self.g[ci]
            for cy in range(self.h):
                bits = g[cy]
                if not bits:
                    continue
                for cx in range(self.w):
                    if bits & (1 << (self.w - 1 - cx)):
                        scr.px(x + i * (self.w + 1) + cx, y + cy, color)

    def width(self, s: str) -> int:
        return len(s) * (self.w + 1) - 1


def load_fonts() -> tuple[Font, Font, Font]:
    f2 = SRC / "rd_font2.c"
    f3 = SRC / "rd_font3.c"
    small = Font(_parse_font(f2, "rd_font2_small", 18), 10, 18)
    big = Font(_parse_font(f2, "rd_font2_big", 30), 16, 30)
    huge = Font(_parse_font(f3, "rd_font3_huge", 26), 22, 26)
    return small, big, huge


PAL = load_palette()
F_SMALL, F_BIG, F_HUGE = load_fonts()


def pal(i: int) -> int:
    return int(PAL[max(0, min(255, i))])


def view_color(v: int, hue: int, gain: int) -> int:
    x = min(255, v * gain)
    return pal((x + hue) & 255)


# --------------------------------------------------------------------------
# 画布：与固件 draw_radial_k / 字体绘制一一对应
# --------------------------------------------------------------------------
class Screen:
    """一块 240x240 RGB565 缓冲。"""

    def __init__(self) -> None:
        self.buf = np.zeros((RD_H, RD_W), dtype=np.uint16)
        self._sn = np.array([int(math.sin(2 * math.pi * b / RD_BLOCKS) * 16384)
                             for b in range(RD_BLOCKS)], dtype=np.int32)
        self._cs = np.array([int(math.cos(2 * math.pi * b / RD_BLOCKS) * 16384)
                             for b in range(RD_BLOCKS)], dtype=np.int32)

    def clear(self, c: int = 0) -> None:
        self.buf[:, :] = c

    def px(self, x: int, y: int, c: int) -> None:
        if 0 <= x < RD_W and 0 <= y < RD_H:
            self.buf[y, x] = c

    def rect(self, x: int, y: int, w: int, h: int, c: int) -> None:
        x0 = max(0, x)
        x1 = min(RD_W, x + w)
        y0 = max(0, y)
        y1 = min(RD_H, y + h)
        if x0 < x1 and y0 < y1:
            self.buf[y0:y1, x0:x1] = c

    def _line(self, x1: int, y1: int, x2: int, y2: int, c: int) -> None:
        dx = abs(x2 - x1)
        dy = -abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx + dy
        while True:
            self.px(x1, y1, c)
            if x1 == x2 and y1 == y2:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x1 += sx
            if e2 <= dx:
                err += dx
                y1 += sy

    def radial(self, b: int, r1: int, r2: int, k: int, c: int) -> None:
        b %= RD_BLOCKS
        cx = RD_W // 2
        cy = RD_H // 2
        cs = int(self._cs[b])
        sn = int(self._sn[b])
        x1 = cx + ((sn * r1 + cs * k + 8192) >> 14)
        y1 = cy - ((cs * r1 - sn * k + 8192) >> 14)
        x2 = cx + ((sn * r2 + cs * k + 8192) >> 14)
        y2 = cy - ((cs * r2 - sn * k + 8192) >> 14)
        self._line(x1, y1, x2, y2, c)

    def text(self, x: int, y: int, s: str, c: int, sc: int = 1) -> None:
        if sc >= 3:
            font = F_HUGE
        elif sc >= 2:
            font = F_BIG
        else:
            font = F_SMALL
        font.draw(self, x, y, s, c)

    def text_w(self, s: str, sc: int = 1) -> int:
        if sc >= 3:
            font = F_HUGE
        elif sc >= 2:
            font = F_BIG
        else:
            font = F_SMALL
        return font.width(s)

    def circle_band(self, r_out: int, r_in: int, c: int) -> None:
        yy, xx = np.mgrid[0:RD_H, 0:RD_W]
        d2 = (xx - RD_W // 2) ** 2 + (yy - RD_H // 2) ** 2
        m = (d2 < r_out * r_out) & (d2 >= r_in * r_in)
        self.buf[m] = c

    def rgb888(self) -> np.ndarray:
        v = self.buf.astype(np.uint16)
        r = (v >> 11 & 31) * 255 // 31
        g = (v >> 5 & 63) * 255 // 63
        b = (v & 31) * 255 // 31
        return np.dstack([r, g, b]).astype(np.uint8)


_ink_scratch: Screen | None = None


def _ink_span(text: str, c: int, sc: int) -> tuple[int, int]:
    """返回这段文字实际着墨的 x 起止列（相对绘制原点）。

    字体位图左右各有留白，按“步进宽”居中会让 4 位数字和 3 字母标签
    差出 4~6 px（240 px 屏上肉眼可见），所以按真实墨迹居中。
    """
    global _ink_scratch
    if _ink_scratch is None:
        _ink_scratch = Screen()
    _ink_scratch.clear()
    _ink_scratch.text(0, 0, text, c, sc)
    cols = np.nonzero(_ink_scratch.buf.any(axis=0))[0]
    if len(cols):
        return int(cols[0]), int(cols[-1])
    return 0, 0


def _attext(scr: Screen, y: int, text: str, c: int, sc: int) -> None:
    """按字宽 advance 居中（固件 rd_text_s 是逐字符推进，不做墨迹居中）。"""
    scr.text((RD_W - scr.text_w(text, sc)) // 2, y, text, c, sc)


def _half(c: int) -> int:
    """RGB565 整体折半（固件用 (c >> 1) & 0x7BEF）。"""
    return (c >> 1) & 0x7BEF


def _ctext(scr: Screen, y: int, text: str, c: int, sc: int,
           right: int | None = None) -> None:
    lo, hi = _ink_span(text, c, sc)
    if right is not None:
        x = right - hi
    else:
        x = (RD_W - (hi - lo + 1)) // 2 - lo
    scr.text(x, y, text, c, sc)


# --------------------------------------------------------------------------
# 占空比（圆心口径）：读 record_occ_trace.py 采回来的真板逐帧码
# --------------------------------------------------------------------------
OCC: dict[int, dict] | None = None     # None = 还没加载过


def load_occ(path: Path | None = None) -> dict[int, dict]:
    """读逐帧 trace → {mhz: window}。缺文件时返回空表（不致命，圆心显示 --）。"""
    global OCC
    p = path or OCC_TRACE
    if not p.exists():
        OCC = {}
        return OCC
    raw = json.loads(p.read_text(encoding="utf-8"))
    OCC = {int(w["mhz"]): w for w in raw.get("windows") or []}
    return OCC


def occ_of(mhz: int) -> int | None:
    """该信道的占空比（%）；trace 里没有就返回 None。"""
    tbl = OCC if OCC is not None else load_occ()
    w = tbl.get(int(mhz))
    if not w:
        return None
    return max(0, min(100, int(w["duty"])))


def occ_word(occ: int | None, light: int | None = None,
             crowd: int | None = None, heavy: int | None = None) -> str:
    """百分比 → 人话等级（字库只有 ASCII，所以是英文单词）。

    阀值默认取模块常量（= 固件 ``RD_OCC_LIGHT/CROWD/HEAVY``），调试台改过之后
    直接改常量即可同时影响两个皮肤。"""
    if light is None:
        light = OCC_LIGHT
    if crowd is None:
        crowd = OCC_CROWD
    if heavy is None:
        heavy = OCC_HEAVY
    if occ is None:
        return "NO DATA"
    if occ >= heavy:
        return "HEAVY"
    if occ >= crowd:
        return "CROWD"
    if occ >= light:
        return "LIGHT"
    return "QUIET"


def draw_occ_readout(scr: Screen, fr: Frame) -> None:
    """圆心读数：频率 + 频段行 + 大字百分比 + '%'（与固件 draw_core_readout 同构）。

    等级词（QUIET/LIGHT/CROWD/HEAVY）已按用户 2026-11-24 要求删除。

    ``fr.occ_override`` 非 None 时用它（调试台手调），否则查真板 trace。"""
    occ = getattr(fr, "occ_override", None)
    if occ is None:
        occ = occ_of(fr.win_mhz)
    # 频率行 = 这一次测量的窗中心（定稿 2026-11-24）
    _ctext(scr, 82, str(fr.win_mhz), pal(88), 3)
    # 圆心数字区同时告诉你"现在滚的是哪个大区"，环上不再写文字。
    # 写 MHZ 不写 MHz：字库只有 ASCII 32..95，小写 z 越界会被静默跳过
    # （固件 rd_char_s / 这里 Font.draw 都是同一规则），那样只会画出 "MH  "。
    # 与固件同款：MHz 单位在数字上方 y62（正确大小写，字库已扩 z），
    # 频段名 y112 居中（2.4G 段 "2.4G"、UNII 段 "5G-UNII-x"）
    _bn = BAND_NAME[band_of_mhz(fr.win_mhz)]
    _attext(scr, 62, "MHz", pal(72), 1)
    _attext(scr, 112, _bn if _bn == "2.4G" else "5G-" + _bn, pal(72), 1)
    if occ is None:
        # 等级词（QUIET/LIGHT/CROWD/HEAVY）已按用户 2026-11-24 要求删除。
        _ctext(scr, 134, "--", pal(184), 3)
        return
    a = str(occ)
    tw = scr.text_w(a, 3)
    scr.text((RD_W - tw) // 2 - 8, 134, a, pal(184), 3)
    scr.text((RD_W + tw) // 2 - 2, 146, "%", pal(184), 1)


def freq_to_grid(f_hz: int) -> int:
    if 2400000000 <= f_hz <= 2483500000:
        return (f_hz - 2400000000) * RD_SEG_24G // 83500000
    if 5150000000 <= f_hz <= 5250000000:
        return RD_SEG_24G + (f_hz - 5150000000) * RD_SEG_U1 // 100000000
    if 5250000000 <= f_hz <= 5350000000:
        return (RD_SEG_24G + RD_SEG_U1
                + (f_hz - 5250000000) * RD_SEG_U2A // 100000000)
    if 5470000000 <= f_hz <= 5725000000:
        return (RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A
                + (f_hz - 5470000000) * RD_SEG_U2C // 255000000)
    if 5725000000 <= f_hz <= 5850000000:
        return (RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A + RD_SEG_U2C
                + (f_hz - 5725000000) * RD_SEG_U3 // 125000000)
    return -1


# --------------------------------------------------------------------------
# 数据帧：把真板样本还原成固件内部数组形状
# --------------------------------------------------------------------------
class Frame:
    """一帧要画的数据。由真板样本重建成固件内部数组的形状。"""

    def __init__(self, sample: dict):
        self.view = int(sample.get("view", 0))
        self.cols = list(sample.get("cols") or [0] * 24)
        self.win_mhz = int(sample.get("win_mhz", 2442))
        self.win_rate = int(sample.get("win_rate", 20000000))
        st = sample.get("stats") or {}
        rg = sample.get("ring") or {}
        self.noise = int(st.get("noise", 36))
        self.lvl_max = int(st.get("lvl_max", max(self.cols)))
        self.lvl_avg = int(st.get("lvl_avg",
                                   int(sum(self.cols) / max(1, len(self.cols)))))
        self.cells = int(rg.get("cells", 300))
        self.pmax = int(rg.get("pmax", 56))
        self.rl_max = int(rg.get("rl_max", 120))
        self.rl_mid = int(rg.get("rl_mid", 82))
        self.warm = int(rg.get("warm", 150))
        self.heat = self._expand_heat()
        self.lvl = self._expand_lvl()
        self.ch_val = self._expand_chart()
        self.spec = self._expand_spec()
        # 占空比：默认走真板 trace（按 win_mhz 查），调试台可改成手调值
        self.occ_override: int | None = None

    def _expand_heat(self) -> np.ndarray:
        c = np.array(self.cols, dtype=np.float64)
        xp = np.linspace(0, RD_GRID - 1, len(c))
        v = np.interp(np.arange(RD_GRID), xp, c)
        top = max(1, int(c.max()))
        return np.clip(v / top * max(8, self.pmax), 0, 255).astype(np.uint8)

    def _expand_lvl(self) -> np.ndarray:
        c = np.array(self.cols, dtype=np.float64)
        xp = np.linspace(0, RD_GRID - 1, len(c))
        return np.clip(np.interp(np.arange(RD_GRID), xp, c),
                       0, 255).astype(np.uint8)

    def _expand_chart(self) -> np.ndarray:
        n = len(CH_MHZ)
        c = np.array(self.cols, dtype=np.float64)
        if self.view == VIEW_CHART:
            head = c[0:6]
            out = np.zeros(n)
            out[0:min(n, len(head))] = head[0:min(n, len(head))]
            rest = c[6:]
            if len(rest):
                idx = np.linspace(6, n - 1, len(rest)).astype(int)
                out[idx] = rest
            return np.clip(out, 0, 255).astype(np.uint8)
        xp = np.linspace(0, n - 1, len(c))
        return np.clip(np.interp(np.arange(n), xp, c),
                       0, 255).astype(np.uint8)

    def _expand_spec(self) -> np.ndarray:
        c = np.array(self.cols, dtype=np.float64)
        xp = np.linspace(0, RD_SPEC_N - 1, len(c))
        return np.clip(np.interp(np.arange(RD_SPEC_N), xp, c),
                       0, 255).astype(np.uint8)


# --------------------------------------------------------------------------
# 旧皮肤（对照用，保留原渲染路径）
# --------------------------------------------------------------------------
def draw_head(scr: Screen, title: str) -> None:
    sc = 2
    tw = scr.text_w(title, sc)
    if tw > 124:
        tw = scr.text_w(title, 1)
        sc = 1
    tx = max(26, (RD_W - tw) // 2)
    step = 16 + 1 if sc >= 2 else 10 + 1
    for i, ch in enumerate(title):
        k = min(216, 56 + i * 22)
        font = F_BIG if sc >= 2 else F_SMALL
        font.draw(scr, tx + i * step, 18, ch, pal(k))


def draw_nav(scr: Screen, view: int) -> None:
    xs = (104, 120, 136)
    for i in range(VIEW_COUNT):
        c = 0xFFFF if i == view else 0x39E7
        w = 4 if i == view else 2
        scr.rect(xs[i] - w // 2, 224, w, 2, c)


def draw_core_readout(scr: Screen, fr: Frame) -> None:
    if fr.cells == 0:
        scr.text((RD_W - scr.text_w("NO SCAN")) // 2, 106, "NO SCAN", 31727)
        return
    # ③ 旧皮肤也跟随定稿口径：圆心报「时间占空比」，不再报峰值强弱。
    draw_occ_readout(scr, fr)


def render_ring(scr: Screen, fr: Frame) -> None:
    scr.clear(0)
    scr.circle_band(RD_R_OUT, RD_HEAT_LO, 0x0841)
    for b in range(RD_BLOCKS):
        scr.radial(b, RD_R_OUT - 1, RD_R_OUT, 0, 0x18E3)
    for b in range(0, RD_BLOCKS, RD_TICK_STEP // 2):
        if b % (RD_TICK_MAJOR // 2) == 0:
            scr.radial(b, RD_TICK_LO, RD_TICK_HI, 0, 0xC618)
            scr.radial(b, RD_TICK_LO, RD_TICK_HI, 1, 0xC618)   # k 是像素，不是 16384
        else:
            scr.radial(b, RD_TICK_LO, RD_TICK_HI - 2, 0, 0x5AEB)
    span = RD_HEAT_HI - RD_HEAT_LO
    pmax = max(8, fr.pmax)
    for b in range(RD_BLOCKS):
        v = max(int(fr.heat[2 * b]), int(fr.heat[2 * b + 1]))
        rl = max(int(fr.lvl[2 * b]), int(fr.lvl[2 * b + 1]))
        if not rl:
            continue
        vv = min(v, pmax)
        length = span * int(math.isqrt(vv * 1024 // pmax)) // 32
        length = max(3, length)
        c = view_color(rl, 0, 2)
        # k 的单位是像素：写 k*16384 等于把两条平行线甩到屏幕外，每格只剩 1 px，
        # 而 176 格在 r≈98 处间距 3.5 px -> 整圈是"栅栏"（固件同款 bug，同批修）
        for k in (-1, 0, 1):
            scr.radial(b, RD_HEAT_HI - length, RD_HEAT_HI, k, c)
    # 光标必须跟着**当前窗口**走（固件用 s_win_mhz/s_win_rate）；这里写死
    # 2437/20 MHz 会让光标永远停在同一处，和真屏对不上。
    win_mhz, win_rate = fr.win_mhz, fr.win_rate or 20000000
    half = win_rate // 2
    g_lo = freq_to_grid(win_mhz * 1000000 - half)
    g_hi = freq_to_grid(win_mhz * 1000000 + half)
    if g_lo >= 0 and g_hi >= 0:
        for g in range(min(g_lo, g_hi), min(max(g_lo, g_hi), RD_GRID - 1) + 1):
            scr.radial(g // 2, RD_R_OUT - 8, RD_R_OUT - 5, 0, 0x02CF)
        gc = max(0, freq_to_grid(win_mhz * 1000000))
        for k in range(-2, 3):
            scr.radial((gc + k + RD_GRID) % RD_GRID // 2,
                       RD_R_OUT - 10, RD_R_OUT - 3, 0, 0xFFFF)
    draw_core_readout(scr, fr)
    for sep in (RD_SEG_24G, RD_SEG_24G + RD_SEG_U1,
                RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A,
                RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A + RD_SEG_U2C):
        for k in (-1, 0, 1):
            scr.radial(sep // 2, RD_HEAT_LO, RD_HEAT_HI, k, 0)
    # ⑦ 频段色环（半径 78..84，夹在圆心功能区 74 与热力环内径 88 之间）：
    #    五段弧各一个颜色 = 2.4G / UNII-1 / 2A / 2C / UNII-3，当前窗所在频段高亮。
    cur = band_of_mhz(fr.win_mhz)
    s24, s1 = RD_SEG_24G // 2, (RD_SEG_24G + RD_SEG_U1) // 2
    s2a = (RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A) // 2
    s2c = (RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A + RD_SEG_U2C) // 2
    for b in range(RD_BLOCKS):
        seg = 0 if b < s24 else (1 if b < s1 else (2 if b < s2a else (3 if b < s2c else 4)))
        c = BAND_COL[seg] if seg == cur else _half(BAND_COL[seg])
        # 三条并排（k 单位是像素）：单条 1 px 线在 r≈81 处间距 2.9 px，覆盖率只有 31%。
        for k in (-1, 0, 1):
            scr.radial(b, 78, 84, k, c)
    scr.radial(0, RD_R_CORE, RD_R_OUT, 0, 0x8410)
    # 本页名字：这一页是「每个信道一格、颜色/长度 = 记忆强度」的环带 -> HEATMAP。
    # y=164 原来放 QUIET/LIGHT/CROWD/HEAVY 等级词，删掉后空出来。
    _ctext(scr, 164, "HEATMAP", pal(72), 1)
    draw_nav(scr, VIEW_RING)


def render_chart(scr: Screen, fr: Frame, page: int = 0) -> None:
    """页 2：38 个信道 × 8 页。**必须支持 page**：原来把 idx 写死成 k，
    仿真永远只画第 1 页 5 个信道，后 33 个从没在 PC 上出过图（真板能滑到）。"""
    scr.clear(0)
    draw_head(scr, "OCCUPY")            # 名字要一眼看出是「信道占用」
    per = RD_CH_PER_PAGE
    pages = (len(CH_MHZ) + per - 1) // per
    page = max(0, min(page, pages - 1))
    top, lh, x_lbl, x_bar, bar_w, bar_h, x_rt = 70, 20, 26, 128, 44, 12, 212
    # 表头：左列 = 中心频率(信道号)、单位 MHz；右列 = 时间占空比 %
    scr.text(28, 50, "MHz CH", pal(72), 1)
    scr.text(x_rt - scr.text_w("OCC%"), 50, "OCC%", pal(72), 1)
    # 与固件 view_chart 同口径：这一列显示的是**时间占空比**（%），
    # 取数与圆心 draw_occ_readout() 完全同源（occ_of(mhz)），不再用电平 %。
    for k in range(per):
        idx = page * per + k
        if idx >= len(CH_MHZ):
            break
        y = top + k * lh
        mhz = CH_MHZ[idx]
        chn = (mhz - 2407) // 5 if mhz < 3000 else (mhz - 5000) // 5
        scr.text(x_lbl, y, f"{mhz}({chn})", pal(72), 1)
        v = occ_of(mhz)                     # None = 该信道没有测量记录
        pct = -1 if v is None else min(100, int(v))
        tc = (0x8410 if pct <= 0 else
              0xC618 if pct < 5 else
              0xFFE0 if pct < 15 else
              0xFD20 if pct < 35 else 0xF800)
        scr.rect(x_bar, y + 3, bar_w, bar_h, 0x18C3)
        w = 0 if pct <= 0 else pct * bar_w // 100
        if w:
            scr.rect(x_bar, y + 3, w, bar_h, tc)
        num = "--" if pct < 0 else f"{pct}%"
        scr.text(x_rt - scr.text_w(num), y, num, tc, 1)      # 右对齐到表头
    # 页脚：一句话说清 OCC% 是什么量（占空比 = 判定有信号的帧占比）
    _ctext(scr, 172, "OCC = BUSY TIME%", pal(72), 1)
    th = 186 - 54
    bh = max(5, th // pages)
    # 页码条：thumb 随页号下移（与固件 view_chart 同一条公式）。
    # 原来固定画在 54，翻页时 thumb 不动，和真板对不上。
    by = 54 + (th - bh) * page // (pages - 1) if pages > 1 else 54
    scr.rect(212, 54, 2, th, 0x2124)
    scr.rect(212, by, 2, bh, 0xCE79)
    draw_nav(scr, VIEW_CHART)


SCAN_CH = (2412, 2427, 2442, 2457, 2472,
           5180, 5200, 5220, 5240, 5260, 5280, 5300, 5320,
           5500, 5520, 5540, 5560, 5580, 5600, 5620, 5640, 5660, 5680, 5700, 5720,
           5745, 5765, 5785, 5805, 5825)


def wf_color(v: int) -> int:
    """页 3 专用：0..RD_HEAT_FULL -> 调色板 0..128，单增、不回卷。

    原来用 view_color(v, 150, 2)（索引 v*2+150）：落在调色板下降支（越高越暗），
    且 v>52 越 255 回卷 -> 同图硬跳变。"""
    return pal(min(128, int(v) * 128 // RD_HEAT_FULL))


def render_wfall(scr: Screen, fr: Frame) -> None:
    scr.clear(0)
    draw_head(scr, "WATERFALL")
    wx, bw = 56, 2
    spec_y, spec_h = 72, 26
    base_y = spec_y + spec_h + 2
    wf_rows, wf_h = 41, 2
    peak_i = int(np.argmax(fr.spec))
    peak_v = int(fr.spec[peak_i])
    for ci in range(RD_SPEC_N):
        v = int(fr.spec[ci])
        h = min(spec_h, v * spec_h // RD_HEAT_FULL)
        if h > 0:
            scr.rect(wx + ci * bw, spec_y + spec_h - h, bw - 1, h,
                     wf_color(v))
    if peak_v > 0:
        scr.rect(wx + peak_i * bw, spec_y, 2, spec_h, 0xFFFF)
    rate = 20000000
    mhz = int(round(fr.win_mhz))
    hi = mhz + rate // 2000000
    lo = mhz - rate // 2000000
    scr.text(28, 52, str(lo), pal(72), 1)
    hw = scr.text_w(str(hi))
    scr.text(RD_W - 28 - hw, 52, str(hi), pal(72), 1)
    scr.text(104, 52, "MHz", pal(72), 1)      # 两个裸数字看不出单位
    scr.rect(wx, base_y, RD_WF_N * bw, 1, 0x4208)
    # 高低颜色状态轴：8 档色块取自与柱体相同的 view_color()，HI 上 / LO 下
    for i in range(8):
        v = (i + 1) * RD_HEAT_FULL // 8
        scr.rect(190, 78 + (7 - i) * 3, 6, 3, wf_color(v))
    scr.text(199, 74, "HI", pal(72), 1)
    scr.text(199, 104, "LO", pal(72), 1)
    # 钉住的信道（与固件 view_wfall 的 PIN 行同位置/同格式）
    if mhz in SCAN_CH:
        ci = SCAN_CH.index(mhz)
        tag = "PIN %d/%d %d" % (ci + 1, len(SCAN_CH), mhz)
        scr.text((RD_W - scr.text_w(tag)) // 2, 188, tag, pal(72), 1)
        # 右栏页码条已删（用户 2026-11-25，与固件同步）：PIN 文字行已表达钉住的信道。
    # 颜色只表示幅度，不掺"年龄"：固件端也没有 fade（对齐后两边一致）
    for r in range(wf_rows):
        y = base_y + 2 + r * wf_h
        for c in range(RD_WF_N):
            v = int(fr.spec[c])
            if v <= 0:
                continue
            scr.rect(wx + c * bw, y, bw - 1, wf_h, wf_color(v))
    draw_nav(scr, VIEW_WFALL)


# --------------------------------------------------------------------------
# 新皮肤
# --------------------------------------------------------------------------
def lvl_step(code: int, ref: int) -> int:
    d = code - ref
    if d <= 0:
        return 0
    return min(8, (d + 3) // 6)


def lvl_color(st: int) -> int:
    return LVL[max(0, min(8, st))]


def band_of_mhz(mhz: int) -> int:
    """0=2.4G 1=UNII-1 2=UNII-2A 3=UNII-2C 4=UNII-3；与固件 band_of_mhz 同值。"""
    if mhz < 3000:
        return 0
    if mhz < 5250:
        return 1
    if mhz < 5350:
        return 2
    if mhz < 5725:
        return 3
    return 4


def band2_of_mhz(mhz: int) -> int:
    return 0 if mhz < 3000 else 1


def draw_head_new(scr: Screen, title: str) -> None:
    sc = 2 if scr.text_w(title, 2) <= 124 else 1
    _ctext(scr, 22, title, C_LABEL, sc)


def draw_nav_new(scr: Screen, view: int) -> None:
    xs = (104, 120, 136)
    for i in range(VIEW_COUNT):
        if i == view:
            scr.rect(xs[i] - 2, 197, 4, 4, C_MAIN)
        else:
            scr.rect(xs[i] - 1, 198, 2, 2, C_DIM)


def _dim(c: int, num: int = 1, den: int = 4) -> int:
    b = c & 31
    g = (c >> 5) & 63
    r = (c >> 11) & 31
    return (r * num // den) << 11 | (g * num // den) << 5 | (b * num // den)


def draw_band_ring(scr: Screen, b2: int) -> None:
    """内圈频段示意：0–45° = 2.4G，其余 = 5G；b=22 是分界留缝。"""
    for b in range(RD_BLOCKS):
        if b == 22:
            continue
        seg = 0 if b < 22 else 1
        c = BAND2_COLOR[seg] if seg == b2 else _dim(BAND2_COLOR[seg])
        for rr in (RD_HEAT_LO - 4, RD_HEAT_LO - 3,
                   RD_HEAT_LO - 2, RD_HEAT_LO - 1):
            scr.radial(b, rr, rr + 1, 0, c)


def _ring_band_floor(lvl: np.ndarray) -> list[int]:
    fl = [255, 255, 255]
    for b in range(RD_BLOCKS):
        rl = max(int(lvl[2 * b]), int(lvl[2 * b + 1]))
        if not rl:
            continue
        bd = BAND_24G if b < 22 else (BAND_5GL if b < 75 else BAND_5GH)
        fl[bd] = min(fl[bd], rl)
    return [0 if v == 255 else v for v in fl]


def render_ring_new(scr: Screen, fr: Frame, band_ring: bool = True) -> None:
    scr.clear(C_BG)
    scr.circle_band(RD_R_OUT, RD_HEAT_LO, C_RING_BG)
    for b in range(RD_BLOCKS):
        scr.radial(b, RD_R_OUT - 1, RD_R_OUT, 0, C_FRAME)
    for b in range(RD_BLOCKS):
        if b % 44 == 0:
            scr.radial(b, RD_TICK_LO, RD_TICK_HI, 0, C_LABEL)
            scr.radial(b, RD_TICK_LO, RD_TICK_HI, 1, C_LABEL)   # k 是像素
        elif b % 11 == 0:
            scr.radial(b, RD_TICK_LO, RD_TICK_HI - 2, 0, C_MINOR)

    lvl = fr.lvl
    fl = _ring_band_floor(lvl)
    for b in range(RD_BLOCKS):
        rl = max(int(lvl[2 * b]), int(lvl[2 * b + 1]))
        if not rl:
            continue
        bd = BAND_24G if b < 22 else (BAND_5GL if b < 75 else BAND_5GH)
        st = lvl_step(rl, fl[bd])
        if not st:
            continue
        for k in (-16384, 0, 16384):
            scr.radial(b, RD_HEAT_LO, RD_HEAT_HI, k, lvl_color(st))

    scr.radial(0, RD_HEAT_LO, RD_R_OUT, 0, C_DIM)

    b2 = band2_of_mhz(fr.win_mhz)
    bc = BAND2_COLOR[b2]
    if band_ring:
        draw_band_ring(scr, b2)

    half = fr.win_rate // 2
    g_lo = freq_to_grid(fr.win_mhz * 1000000 - half)
    g_hi = freq_to_grid(fr.win_mhz * 1000000 + half)
    if g_lo >= 0 and g_hi >= 0:
        for g in range(min(g_lo, g_hi), min(max(g_lo, g_hi), RD_GRID - 1) + 1):
            scr.radial(g // 2, RD_R_OUT - 8, RD_R_OUT - 5, 0, bc)
    gc = freq_to_grid(fr.win_mhz * 1000000)
    if gc >= 0:
        for k in range(-2, 3):
            scr.radial((gc // 2 + k) % RD_BLOCKS,
                       RD_R_OUT - 10, RD_R_OUT - 3, 0, C_MAIN)
    for sep, nxt in ((22, BAND_5GL), (75, BAND_5GH)):
        scr.radial(sep, RD_HEAT_LO, RD_HEAT_HI, -16384, C_BG)
        scr.radial(sep, RD_HEAT_LO, RD_HEAT_HI, 16384, C_BG)
        scr.radial(sep, RD_HEAT_LO, RD_HEAT_HI, 0, BAND_COLOR[nxt])

    if fr.cells == 0:
        _ctext(scr, 94, "WAIT", C_LABEL, 2)
        _ctext(scr, 134, "FIRST SCAN", C_LABEL, 1)
        draw_nav_new(scr, VIEW_RING)
        return

    # 圆心信息优先级：当前频率 → 占用率（与固件 draw_core_readout 同布局）
    draw_occ_readout(scr, fr)
    draw_nav_new(scr, VIEW_RING)


def chart_pages() -> int:
    return sum((GRP_LEN[g] + RD_CH_PER_PAGE - 1) // RD_CH_PER_PAGE
               for g in range(3))


def chart_page_slot(page: int) -> tuple[int, int, int]:
    for g in range(3):
        np_ = (GRP_LEN[g] + RD_CH_PER_PAGE - 1) // RD_CH_PER_PAGE
        if page < np_:
            per, extra = GRP_LEN[g] // np_, GRP_LEN[g] % np_
            f = GRP_START[g]
            for p in range(page):
                f += per + (1 if p < extra else 0)
            return g, f, per + (1 if page < extra else 0)
        page -= np_
    return 0, 0, 0


def render_chart_new(scr: Screen, fr: Frame, page: int = 0) -> None:
    scr.clear(C_BG)
    draw_head_new(scr, "CHANNEL")
    grp, first, rows = chart_page_slot(page)
    scr.text(31, 46, f"{GRP_NAME[grp]} MHZ", C_LABEL, 1)
    _ctext(scr, 46, f"{page + 1}/{chart_pages()}", C_DIM, 1, right=208)

    seg = np.array([int(fr.ch_val[GRP_START[g] + i])
                    for g in range(3) for i in range(GRP_LEN[g])],
                   dtype=np.int16)
    floors = []
    for g in range(3):
        vals = [int(x) for x in seg[GRP_START[g]:GRP_START[g] + GRP_LEN[g]]
                if x > 0]
        floors.append(min(vals) if vals else 0)
    ref = floors[grp]

    for k in range(rows):
        idx = first + k
        if idx >= len(seg):
            break
        y = 62 + 20 * k
        v = int(seg[idx])
        mhz = CH_MHZ[idx] if idx < len(CH_MHZ) else 0
        chn = idx + 1
        lc = C_LABEL if v else C_DIM
        scr.text(25, y, f"{mhz} CH{chn}", lc, 1)
        scr.rect(120, y + 3, 46, 11, C_TRACK)
        if v:
            st = lvl_step(v, ref)
            d = max(0, v - ref)
            if st:
                scr.rect(120, y + 3, (46 * st + 4) // 8, 11, lvl_color(st))
            _ctext(scr, y, f"{d}DB", C_MAIN if d else C_DIM, 1, right=215)
        else:
            _ctext(scr, y, "--", C_DIM, 1, right=215)
        for db in (8, 15, 25):
            scr.rect(120 + (46 * db + 24) // 48, y + 3, 1, 11, C_BG)

    _ctext(scr, 178, "OVER QUIET", C_DIM, 1)
    draw_nav_new(scr, VIEW_CHART)


def _spec_floor_q(v: np.ndarray) -> int:
    nz = v[v > 0]
    if len(nz):
        return int(np.percentile(nz, 25))
    return 0


def render_wfall_new(scr: Screen, fr: Frame) -> None:
    scr.clear(C_BG)
    draw_head_new(scr, "WFALL")
    wx, bw = 56, 2
    spec_y, spec_h = 80, 24
    lo = fr.win_mhz - fr.win_rate // 2000000
    hi = fr.win_mhz + fr.win_rate // 2000000
    scr.text(30, 48, str(lo), C_LABEL, 1)
    scr.text(104, 48, "MHZ", C_LABEL, 1)
    _ctext(scr, 48, str(hi), C_LABEL, 1, right=210)
    band = band_of_mhz(fr.win_mhz)
    scr.text(26, 62, BAND_NAME[band], BAND_COLOR[band], 1)
    peak_i = int(np.argmax(fr.spec))
    pk = (fr.win_mhz - fr.win_rate // 2000000
          + peak_i * fr.win_rate // 1000000 // (RD_SPEC_N - 1))
    _ctext(scr, 62, f"PK {pk}", C_MAIN, 1, right=212)
    scr.text(198, 78, "HI", C_LABEL, 1)
    scr.text(198, 92, "LO", C_LABEL, 1)
    for i in range(8):
        scr.rect(190, 80 + 3 * i, 5, 3, lvl_color(8 - i))
    floor = _spec_floor_q(fr.spec)
    for c in range(RD_SPEC_N):
        st = lvl_step(int(fr.spec[c]), floor)
        if not st:
            continue
        h = st * spec_h // 8
        scr.rect(wx + c * bw, spec_y + spec_h - h, bw - 1, h, lvl_color(st))
    scr.rect(wx + peak_i * bw, spec_y, 2, spec_h, C_MAIN)
    scr.rect(wx, spec_y + spec_h + 1, RD_WF_N * bw, 1, C_MINOR)
    for r in range(36):
        y = 107 + r * 2
        fade = 0.55 + 0.45 * (r / 35)
        for c in range(RD_WF_N):
            v = int(fr.spec[c] * fade)
            st = lvl_step(v, floor)
            if not st:
                continue
            scr.rect(wx + c * bw, y, bw - 1, 2, lvl_color(st))
    scr.text(22, 107, "-5S", C_DIM, 1)
    scr.text(22, 163, "NOW", C_LABEL, 1)
    _ctext(scr, 182, "SNAPSHOT", C_DIM, 1)
    draw_nav_new(scr, VIEW_WFALL)


RENDER_OLD = {VIEW_RING: render_ring, VIEW_CHART: render_chart,
              VIEW_WFALL: render_wfall}
RENDER_NEW = {VIEW_RING: render_ring_new, VIEW_CHART: render_chart_new,
              VIEW_WFALL: render_wfall_new}
SKINS = {"current": (RENDER_OLD, draw_nav),
         "new": (RENDER_NEW, draw_nav_new)}


def render(scr: Screen, fr: Frame, view: int | None = None,
           skin: str = "current", page: int = 0,
           band_ring: bool = True) -> None:
    v = int(fr.view) if view is None else int(view)
    tbl = SKINS[skin][0]
    if skin == "new":
        if v == VIEW_CHART:
            tbl[v](scr, fr, page)
            return
        if v == VIEW_RING:
            tbl[v](scr, fr, band_ring)
            return
    if v == VIEW_CHART:
        tbl[v](scr, fr, page)      # 旧皮肤（= 对齐固件的那套）也要能翻页
        return
    tbl[v](scr, fr)


def load_frames() -> dict[int, list[Frame]]:
    if not TRACE.exists():
        raise SystemExit(f"缺样本 {TRACE}\n先跑采集脚本或参考 docstring 说明。")
    raw = json.loads(TRACE.read_text(encoding="utf-8"))
    out: dict[int, list[Frame]] = {}
    for v, recs in raw.items():
        frames = [Frame(r) for r in recs if r.get("cols")]
        if not frames:
            continue
        out[int(v)] = frames
    if not out:
        raise SystemExit("样本里没有可用帧")
    return out


# --------------------------------------------------------------------------
# GUI / 出图
# --------------------------------------------------------------------------
def run_gui(frames: dict[int, list[Frame]], zoom: int = 3, start: int = 0,
            skin: str = "current", shot_on_start: str = "", page: int = 0) -> int:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import (QApplication, QLabel, QVBoxLayout, QWidget)

    views = sorted(frames)
    state = {"view": start if start in frames else views[0], "i": 0,
             "paused": False, "skin": skin, "page": int(page), "ring2": True,
             "win": 2442}
    scr = Screen()

    app = QApplication(sys.argv)

    class Canvas(QWidget):
        """必须子类重写 keyPressEvent —— 给实例赋 ``win.keyPressEvent=...``
        在 PySide6 里不生效（C++ 虚函数表不查实例 __dict__），键盘会全哑。"""

        def keyPressEvent(self, e):
            keyPress(e)

    win = Canvas()
    win.setFocusPolicy(Qt.StrongFocus)
    win.setWindowTitle("圆屏仿真 240x240 · 真板样本重放")
    win.setStyleSheet("background:#111;")
    lay = QVBoxLayout(win)
    lay.setContentsMargins(10, 10, 10, 10)
    img = QLabel()
    img.setAlignment(Qt.AlignCenter)
    hud = QLabel()
    hud.setStyleSheet("color:#9cf; font:12px Consolas,monospace;")
    hud.setAlignment(Qt.AlignCenter)
    lay.addWidget(img)
    lay.addWidget(hud)

    def cur() -> Frame:
        flist = frames.get(state["view"]) or next(iter(frames.values()))
        fr = flist[state["i"] % len(flist)]
        fr.view = state["view"]
        fr.win_mhz = state["win"]
        return fr

    def draw() -> None:
        fr = cur()
        render(scr, fr, state["view"], state["skin"], state["page"],
               state["ring2"])
        rgb = scr.rgb888()
        h, w, _ = rgb.shape
        qim = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        img.setPixmap(QPixmap.fromImage(qim).scaled(
            w * zoom, h * zoom, Qt.KeepAspectRatio, Qt.FastTransformation))
        n = len(frames.get(state["view"]) or [])
        extra = (f"  page {state['page'] + 1}/{chart_pages()}"
                 if state["view"] == VIEW_CHART else "")
        band = band_of_mhz(state["win"])
        occ = occ_of(state["win"])
        hud.setText(
            f"skin={state['skin']}  view={state['view']}  帧 "
            f"{state['i'] % max(1, n) + 1}/{max(1, n)}{extra}"
            f"  win={state['win']}MHZ({BAND_NAME[band]})  noise={fr.noise}"
            f"  占用={'--' if occ is None else str(occ) + '%/' + occ_word(occ)}"
            f"  内环={'ON' if state['ring2'] else 'OFF'}"
            "  [1/2/3 页 N 换皮 B 换频段 D 内环 ←/→ 帧 ↑/↓ 翻页 空格暂停 S 存图]")

    def keyPress(e) -> None:
        k = e.key()
        if k in (Qt.Key_1, Qt.Key_2, Qt.Key_3):
            want = int(chr(e.key()))
            if want in frames:
                state["view"] = want
                state["i"] = 0
        elif k == Qt.Key_Left:
            state["i"] -= 1
        elif k == Qt.Key_Right:
            state["i"] += 1
        elif k == Qt.Key_Up:
            state["page"] = (state["page"] + 1) % chart_pages()
        elif k == Qt.Key_Down:
            state["page"] = (state["page"] - 1) % chart_pages()
        elif k == Qt.Key_N:
            state["skin"] = "new" if state["skin"] == "current" else "current"
        elif k == Qt.Key_B:
            op = (2442, 5240, 5745)
            state["win"] = (op[(op.index(state["win"]) + 1) % 3]
                            if state["win"] in op else 2442)
        elif k == Qt.Key_D:
            state["ring2"] = not state["ring2"]
        elif k == Qt.Key_Space:
            state["paused"] = not state["paused"]
        elif k == Qt.Key_S:
            draw()
            out = ROOT / "build" / f"sim_{state['skin']}_v{state['view']}.png"
            img.pixmap().save(str(out))
            hud.setText(hud.text() + f"   已存 {out.name}")
        draw()

    timer = QTimer()

    def tick() -> None:
        if not state["paused"]:
            flist = frames.get(state["view"]) or []
            if flist:
                state["i"] = (state["i"] + 1) % len(flist)
        draw()

    timer.timeout.connect(tick)
    timer.start(900)
    draw()
    win.resize(RD_W * zoom + 40, RD_H * zoom + 80)
    win.show()
    win.raise_()
    win.activateWindow()
    win.setFocus(Qt.OtherFocusReason)

    log = ROOT / "build" / "sim_gui.log"

    def _save(path: str) -> None:
        draw()
        img.pixmap().save(path)
        np.save(str(ROOT / "build" / "sim_gui_buf.npy"), scr.buf)
        log.write_text(
            f"started skin={state['skin']} view={state['view']} "
            f"win={state['win']} ring2={state['ring2']} shot={path}\n",
            encoding="utf-8")

    if shot_on_start:
        QTimer.singleShot(900, lambda: _save(shot_on_start))
    else:
        log.write_text(f"started skin={state['skin']} view={state['view']}\n",
                       encoding="utf-8")
    return app.exec()


def shot_all(frames: dict[int, list[Frame]], prefix: str,
             skin: str = "current", page: int = 0) -> list[Path]:
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    scr = Screen()
    out = []
    if not QApplication.instance():
        _ = QApplication(sys.argv)
    for v in sorted(frames):
        fr = frames[v][len(frames[v]) // 2]
        fr.view = v
        render(scr, fr, v, skin, page)
        rgb = scr.rgb888()
        h, w, _ = rgb.shape
        qi = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        p = ROOT / "build" / f"{prefix}_view{v}.png"
        qi.scaled(w * 3, h * 3).save(str(p))
        out.append(p)
        print(f"[shot] {p}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skin", default="current", choices=["current", "new"],
                    help="current = 固件实际渲染的这一套（默认）；new = 未上固件的"
                         "重设计原型（内环频段色带 + 新导航点），只做对照用")
    ap.add_argument("--shot", nargs="?", const="sim",
                    help="截三页图后退出（前缀）")
    ap.add_argument("--shot-on-start", default="",
                    help="GUI 起来 0.9 s 后自动存一张（验证 GUI 渲染路径）")
    ap.add_argument("--zoom", type=int, default=3)
    ap.add_argument("--view", type=int, default=0)
    ap.add_argument("--page", type=int, default=0,
                    help="页 2（OCCUPY）显示第几页，0 起；38 信道 / 每页 5 = 8 页。"
                         "不加就是第 1 页，后 7 页以前根本出不了图")
    args = ap.parse_args()

    frames = load_frames()
    print(f"样本页: {sorted(frames)}  每页帧数: "
          f"{ {k: len(v) for k, v in frames.items()} }  skin={args.skin}")
    occ = load_occ()
    if occ:
        busy = sorted(k for k, w in occ.items() if int(w["duty"]) > 0)
        print(f"占空比 trace: {OCC_TRACE.name}  {len(occ)} 窗，非零: "
              f"{[(m, occ[m]['duty']) for m in busy] or '全部 0%'}")
    else:
        print(f"占空比 trace 缺失（{OCC_TRACE.name}）→ 圆心会画 '--' NO DATA\n"
              "  跑一遍：python testing/record_occ_trace.py --port COM56")
    if args.shot:
        shot_all(frames, args.shot, args.skin, args.page)
        return 0
    return run_gui(frames, args.zoom, args.view, args.skin, args.shot_on_start,
                   args.page)


if __name__ == "__main__":
    sys.exit(main())