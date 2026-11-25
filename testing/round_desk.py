#!/usr/bin/env python
"""圆屏上位机调试台：活数据交互仿真（合成采样 + 屏外控制面板）。

屏幕像素与固件同构 —— 渲染直接复用 testing/round_sim.py 的 render_ring /
render_chart（已被 build/_probe_unii.py、build/_verify_unii_board.py 对过真板）。
本文件补上真机之外的两块：

  1. 合成射频环境 FakeRadio：噪声底 + 可配置发射源（Wi-Fi AP 突发、
     5.8G 图传准连续、蓝牙跳频、手动注入突发），按 20 MHz 窗产 64 bin 功率码行；
  2. 固件行为镜像 DeskModel（逐条对齐 main/common/round_ui.c）：
     - 自扫调度：30 信道轮转，驻留 ~100 ms（固件 60 ms，2 帧一轮 = 跑满一圈约 3 s）
     - heat_absorb 双向结算：覆盖格有信号 +1 / 无信号 -1（round_ui.c:466 起同款）
     - s_occ EMA(alpha=1/8，固件同款不对称取整) + 时间占空比口径
     - s_lvl(>>5|1) / s_ring_lvl(>>6|1) 双速衰减
     - 瀑布 64 行环：每 3 帧推一行；换信道不清历史、起点打 0x8410 分隔线（方案 A）
     - 手势 FSM：左条 x<92 切视图 / 上下滑翻页换信道 / y>=200 小方块直选
     - 轮播每页独立秒数 view_sec（默认 8/25/8），到点强制切换、跳 0 秒页，
       无静默豁免（round_ui.c ③b，RD VIEWSEC 口径，用户 2026-11-25）
     - 演示导览（RD DEMO 同款节奏：页1 1.5 圈 -> 页2 每页 2.5 s -> 页3 10 s）

用法::
    python testing/round_desk.py                    # 交互台（默认"日常 WiFi"环境）
    python testing/round_desk.py --zoom 3           # 屏幕放大 3 倍
    python testing/round_desk.py --preset 5.8G图传
    python testing/round_desk.py --smoke            # 无头自检 + 出三页 PNG
"""
import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import round_sim as RS  # noqa: E402

RATE = 20_000_000          # 自扫窗采样率（固件 FSM START 512 1 0）
RD_HEAT_FULL = RS.RD_HEAT_FULL   # 96：瀑布/频谱满量程码值
NSCAN = len(RS.SCAN_CH)    # 30 个被扫信道
PAGES = (len(RS.CH_MHZ) + RS.RD_CH_PER_PAGE - 1) // RS.RD_CH_PER_PAGE  # 8 页
# 页 3 右栏滑块几何（round_ui.c:125-128，round_sim 里没有，这里本地镜像）
RD_WF_BAR_X, RD_WF_BAR_Y, RD_WF_BAR_H, RD_WF_BAR_W = 212, 54, 132, 2


# ---------------------------------------------------------------- 射频环境 --
class Emitter:
    """一个发射源：中心频率 / 宽度 / 峰值码 / 占空比（时间维）。

    duty/period/phase 决定「这阵子开没开机」：((t/period+phase) % 1) < duty 为开机。
    hop=True 时中心频率按 0.22 s 一跳在 2.4G ISM 段游走（模拟蓝牙）。"""

    def __init__(self, mhz, width, peak, duty, period, phase=0.0, hop=False):
        self.mhz, self.width, self.peak = mhz, width, peak
        self.duty, self.period, self.phase, self.hop = duty, period, phase, hop


HOP_TABLE = [2402, 2426, 2448, 2417, 2462, 2432, 2472, 2412]

PRESETS = {
    "安静环境": [],
    "日常 WiFi": [
        Emitter(2437, 18, 78, 0.75, 3.1, 0.00),          # ch5-ch10 重度（宽带 AP 突发）
        Emitter(2452, 14, 70, 0.60, 2.3, 0.37),          # ch9 中度
        Emitter(2412, 10, 52, 0.18, 2.0, 0.11),          # ch1 轻度
        Emitter(2462, 8, 58, 0.35, 4.0, 0.61),           # ch11 中度
        Emitter(5240, 12, 55, 0.30, 3.5, 0.20),          # UNII-1 AP 中度
        Emitter(5785, 8, 92, 0.97, 5.0, 0.00),           # 5.8G 图传准连续强信号
        Emitter(0, 1, 44, 0.55, 1.7, 0.5, hop=True),     # 蓝牙跳频
    ],
    "2.4G拥挤": [
        Emitter(2412, 12, 72, 0.55, 2.0, 0.05),
        Emitter(2437, 18, 80, 0.80, 2.6, 0.30),
        Emitter(2452, 14, 66, 0.70, 3.3, 0.52),
        Emitter(2462, 10, 74, 0.62, 2.1, 0.14),
        Emitter(0, 1, 50, 0.60, 1.3, 0.5, hop=True),
    ],
    "5.8G图传": [
        Emitter(5785, 8, 92, 0.97, 5.0, 0.00),           # 主图传（FPV 同款 4~8 MHz 窄带）
        Emitter(5745, 6, 70, 0.85, 4.0, 0.22),           # 第二路
        Emitter(5240, 12, 45, 0.20, 5.5, 0.10),          # 远处 UNII-1 AP 弱
    ],
    "全频段忙碌": [
        Emitter(2412, 12, 70, 0.50, 2.0, 0.03),
        Emitter(2437, 18, 82, 0.85, 2.2, 0.28),
        Emitter(2462, 10, 72, 0.60, 1.9, 0.55),
        Emitter(5180, 10, 58, 0.35, 3.0, 0.10),
        Emitter(5240, 12, 62, 0.45, 2.8, 0.41),
        Emitter(5500, 10, 56, 0.30, 3.6, 0.18),
        Emitter(5785, 8, 90, 0.97, 4.5, 0.00),
        Emitter(0, 1, 52, 0.55, 1.2, 0.5, hop=True),
    ],
}


class FakeRadio:
    """合成 64 bin 功率码行（口径与真板一致：0..~96 码，噪声底 ~9-14）。"""

    def __init__(self, preset="日常 WiFi", seed=7):
        self.emitters = list(PRESETS[preset])
        self.burst = None                    # (center_mhz, until_t)
        self.rng = np.random.default_rng(seed)

    def row(self, win_mhz, t, gain_scale):
        """产一个 20 MHz 窗的 64 bin 行（uint8 码值）。"""
        lo = win_mhz - RATE / 2e6
        f = lo + RATE / 1e6 * (2 * np.arange(RS.RD_SPEC_N) + 1) / (2 * RS.RD_SPEC_N)  # bin 中心 MHz
        base = 12.0 if win_mhz < 3000 else 9.0
        floor = base * gain_scale * (1.0 + 0.10 * math.sin(t * 0.7 + win_mhz * 0.01))
        v = floor + self.rng.random(RS.RD_SPEC_N) * 2.5
        for em in self.emitters:
            c = em.mhz
            if em.hop:
                c = HOP_TABLE[int(t / 0.22) % len(HOP_TABLE)]
            if ((t / em.period + em.phase) % 1.0) >= em.duty:
                continue
            amp = em.peak * gain_scale * (0.85 + 0.15 * float(self.rng.random()))
            half = em.width / 2.0
            prof = np.clip(1.0 - np.abs(f - c) / (half + 0.5), 0.0, 1.0)
            v = np.maximum(v, amp * prof)
        if self.burst and t < self.burst[1]:
            c, half = self.burst[0], 2.0
            prof = np.clip(1.0 - np.abs(f - c) / (half + 0.5), 0.0, 1.0)
            v = np.maximum(v, 88.0 * gain_scale * prof)
        return np.clip(v, 0, 127).astype(np.uint8)

    def inject(self, mhz, dur=2.0, t=0.0):
        self.burst = (mhz, t + dur)


# ------------------------------------------------------------ 固件行为镜像 --
class DeskModel:
    """round_ui.c 行为镜像：自扫 / heat_absorb / occ EMA / 瀑布环 / 手势 / 轮播。"""

    def __init__(self, preset="日常 WiFi", gain=60, fps=20.0):
        self.preset = preset
        self.radio = FakeRadio(preset)
        self.gain = gain
        self.fps = fps
        self.dwell_ticks = 2                 # 1x：每窗 2 帧（一圈约 3 s，固件同量级）
        # ---- 显示状态（固件 s_view / s_page / s_wf_ch ...）----
        self.view = 0
        self.page = 0
        self.wf_ch = 0
        self.wf_pinned = False
        self.demo = False
        self.view_sec = [8.0, 25.0, 8.0]     # 各页驻留秒数（0=不停留，全0=关）
        # ---- 测量数组（固件 s_heat / s_lvl / s_ring_lvl / s_occ / s_wf）----
        self.heat = np.zeros(RS.RD_GRID, dtype=np.uint8)
        self.lvl = np.zeros(RS.RD_GRID, dtype=np.uint8)
        self.ring_lvl = np.zeros(RS.RD_GRID, dtype=np.uint8)
        self.occ = np.zeros(RS.RD_GRID, dtype=np.uint8)
        self.wf = np.zeros((RS.RD_WF_N, RS.RD_WF_N), dtype=np.uint8)
        self.wf_head = 0
        self.wf_sep = np.zeros((RS.RD_WF_N + 7) // 8, dtype=np.uint8)
        self.wf_tick = 0
        # ---- 调度 / 批次（固件 FSMPLAN 60 ms 驻留 + spectrum 批次占用）----
        self.scan_i = 0
        self.win_mhz = RS.SCAN_CH[0]
        self.dwell_left = self.dwell_ticks
        self.batch_hits = 0
        self.batch_frames = 0
        self.last_absorb_mhz = 0
        self.rounds = 0                      # 自扫整圈数（演示页 1 的 1.5 圈判据）
        # ---- 时间轴 / 频谱 ----
        self.t = 0.0
        self.frame_no = 0
        self.spec = np.zeros(RS.RD_SPEC_N, dtype=np.uint8)
        self.noise = 0
        self.pmax = 8
        # ---- 演示导览状态 ----
        self.demo_phase = 0
        self.demo_t0 = 0.0
        self.demo_page = 0
        self.log = []                        # 事件日志（GUI 控制台 + smoke 断言）
        RS.OCC = {}                          # 活占用表：occ_of() 从这里读

    # ---------------- 事件日志 ----------------
    def say(self, msg):
        line = f"[{self.t:7.1f}s] {msg}"
        self.log.append(line)

    # ---------------- 自扫调度 ----------------
    def _next_window(self):
        self.settle_occ()
        self.scan_i = (self.scan_i + 1) % NSCAN
        if self.scan_i == 0:
            self.rounds += 1
        self.win_mhz = RS.SCAN_CH[self.scan_i]
        self.dwell_left = self.dwell_ticks

    def tick(self):
        """一帧 = 固件 ui_task 一轮（调度 -> 采样 -> 结算 -> 瀑布 -> 轮播/演示）。"""
        if self.pause:
            return
        self.t += 1.0 / self.fps
        # ① 窗口调度（页 3 钉住单信道，其余整表轮转）
        if self.wf_pinned:
            self.win_mhz = RS.SCAN_CH[self.wf_ch]
        else:
            self.dwell_left -= 1
            if self.dwell_left <= 0:
                self._next_window()
        # ② 采样一行（真板 = spectrum_last_row 拿最近一帧）
        self.spec = self.radio.row(self.win_mhz, self.t, self.gain_scale())
        self.batch_frames += 1
        if int((self.spec > self.noise + 8).any()):
            self.batch_hits += 1
        # ③ heat_absorb：同窗只结算一次（round_ui.c:494 同窗早退），换窗才衰减+并入
        if self.win_mhz != self.last_absorb_mhz:
            self._absorb(self.spec)
            self.last_absorb_mhz = self.win_mhz
        # ④ 瀑布推进：只在页 3、每 3 帧推一行（round_ui.c:1401）
        if self.view == 2 and self.wf_pinned:
            self.wf_tick += 1
            if self.wf_tick >= 3:
                self.wf_tick = 0
                self.wf_head = (self.wf_head + 1) % RS.RD_WF_N
                self.wf[self.wf_head] = self.spec
        # ⑤ 自动轮播（round_ui.c:1953）与演示导览
        self._carousel()
        if self.demo:
            self._demo_tick()
        # ⑥ 统计
        self.frame_no += 1
        self.pmax = max(8, int(self.heat.max()))

    def gain_scale(self):
        return min(1.35, max(0.4, self.gain / 60.0))

    # ---------------- heat_absorb（round_ui.c:466 逐句镜像）----------------
    def _absorb(self, row):
        nz = row[row > 0]
        self.noise = int(np.sort(nz)[len(nz) // 4]) if len(nz) else 0   # 25% 分位底噪
        thr = self.noise + 8
        lo = self.win_mhz - RATE / 2e6
        cov, hit = [], []
        for i in range(RS.RD_SPEC_N):
            f = lo + RATE / 1e6 * (2 * i + 1) / (2 * RS.RD_SPEC_N)
            g = RS.freq_to_grid(int(round(f * 1_000_000)))
            if g < 0 or g >= RS.RD_GRID or g in cov:
                continue
            cov.append(g)
            if row[i] > thr and g not in hit:
                hit.append(g)
        # 双速衰减：s_lvl 每结算 -3%（半衰 4.5 s）、s_ring_lvl -1.5%（半衰 ~9 s）
        for arr, shift in ((self.lvl, 5), (self.ring_lvl, 6)):
            dec = (arr.astype(np.uint16) >> shift) | 1
            arr[:] = np.maximum(arr.astype(np.int16) - dec, 0).astype(np.uint8)
        for i in range(RS.RD_SPEC_N):
            f = lo + RATE / 1e6 * (2 * i + 1) / (2 * RS.RD_SPEC_N)
            g = RS.freq_to_grid(int(round(f * 1_000_000)))
            if 0 <= g < RS.RD_GRID:
                if row[i] > self.lvl[g]:
                    self.lvl[g] = row[i]
                if row[i] > self.ring_lvl[g]:
                    self.ring_lvl[g] = row[i]
        for g in cov:                    # 双向结算：有信号 +1 / 无信号 -1
            if g in hit:
                if self.heat[g] < 255:
                    self.heat[g] += 1
            elif self.heat[g]:
                self.heat[g] -= 1

    # ---------------- 占用结算（round_ui.c:483，EMA alpha=1/8 不对称取整）----
    def settle_occ(self):
        if not self.batch_frames:
            return
        pct = self.batch_hits * 100 // self.batch_frames
        og = RS.freq_to_grid(self.win_mhz * 1_000_000)
        if 0 <= og < RS.RD_GRID:
            cur, d = int(self.occ[og]), pct - int(self.occ[og])
            self.occ[og] = cur + (d + 7) // 8 if d > 0 else cur + d // 8
            RS.OCC[self.win_mhz] = {"duty": int(self.occ[og])}
        self.batch_hits = self.batch_frames = 0

    # ---------------- 手势 FSM（round_ui.c:1888 抬手判手势，逐句镜像）----
    def gesture(self, x0, y0, x1, y1):
        """按下(x0,y0) 抬起(x1,y1)；返回手势描述或 None。"""
        dx, dy = x1 - x0, y1 - y0
        ady = abs(dy)
        adx = abs(dx)
        if self.view == 1 and ady > 30 and ady > adx:      # CHART 上下滑 = 翻页
            acted = None
            if dy < 0 and self.page + 1 < PAGES:
                self.page += 1
                acted = f"CHART 翻到第 {self.page + 1}/{PAGES} 页"
            elif dy > 0 and self.page > 0:
                self.page -= 1
                acted = f"CHART 回到第 {self.page + 1}/{PAGES} 页"
            self._touch()
            if acted:
                self.say(f"手势:上/下滑 -> {acted}")
            return acted
        if self.view == 2 and ady > 30 and ady > adx:      # 页3 上下滑 = 换信道
            self.pin_channel(self.wf_ch + (1 if dy < 0 else -1))
            self._touch()
            return "WFALL 换信道"
        if x0 < 92:                                        # 左条 = 下一视图
            nv = (self.view + 1) % 3
            self.set_view(nv)
            return f"左条点击 -> 视图 {nv + 1}"
        if y0 >= 200:                                      # 底部小方块直选
            k = min(2, max(0, (x0 - 96) // 16))
            self.set_view(k)
            return f"小方块直选 -> 视图 {k + 1}"
        return None

    def tap_left(self):
        return self.gesture(30, 120, 30, 120)

    def swipe(self, up=True):
        y0, y1 = (150, 105) if up else (105, 150)
        return self.gesture(120, y0, 120, y1)

    # ---------------- 视图副作用（round_ui.c:1975 ③d）----------------
    def set_view(self, v):
        prev = self.view
        if v == prev:
            return
        self.view = v
        self.page = 0
        self.wf_tick = 0
        if v == 2:
            self.pin_channel(self.wf_ch)
        elif prev == 2:
            self.wf_pinned = False            # 回轮播：恢复整表自扫，瀑布冻结不清
        self._touch()
        self.say(f"视图 -> {v + 1}")

    # ---------------- 钉信道（round_ui.c:2047 rd_pin_channel 镜像）----
    # ---------------- 钉信道（round_ui.c:2047 rd_pin_channel 镜像）----
    def pin_channel(self, idx):
        idx %= NSCAN
        self.wf_ch = idx
        self.wf_pinned = True
        self.wf_tick = 0
        sep_row = (self.wf_head + 1) % RS.RD_WF_N     # 方案 A：不清历史，起点打分隔线
        self.wf_sep[sep_row >> 3] |= np.uint8(1 << (sep_row & 7))
        self.say(f"钉信道 PIN {idx + 1}/{NSCAN} {RS.SCAN_CH[idx]} MHz")

    def reset_occ(self):
        self.heat[:] = 0
        self.lvl[:] = 0
        self.ring_lvl[:] = 0
        self.occ[:] = 0
        self.last_absorb_mhz = 0
        for mhz in RS.SCAN_CH:
            RS.OCC[mhz] = {"duty": 0}
        self.say("重置占用/热力/电平")

    # ---------------- 自动轮播（round_ui.c ③b：每页秒数+强切）----------------
    def _touch(self):
        """本页重新驻留（固件 s_view_ms = now；无静默豁免）。"""
        self._view_ms = self.t

    def _carousel(self):
        if self.demo or not any(self.view_sec):
            return
        if not hasattr(self, "_view_ms"):
            self._view_ms = self.t
        if self.t - self._view_ms < self.view_sec[self.view]:
            return
        nv = self.view
        for _ in range(3):                       # 跳过 0 秒页；全零不进到这里
            nv = (nv + 1) % 3
            if self.view_sec[nv]:
                break
        if nv != self.view:
            self.set_view(nv)                    # set_view 里已 _touch 重置
        else:
            self._view_ms = self.t

    # ---------------- 演示导览（RD DEMO 节奏）----------------
    def set_demo(self, on):
        self.demo = bool(on)
        if self.demo:
            self.demo_phase = 0
            self.demo_t0 = self.t
            self.demo_page = 0
            self.set_view(0)
            self.say("演示导览开始（页1 1.5圈 -> 页2 每页2.5s -> 页3 10s 循环）")
        else:
            self.say("演示导览结束")

    def _demo_tick(self):
        if self.demo_phase == 0:                       # 页 1：扫满 1.5 圈
            if self.rounds >= 1.5 or self.t - self.demo_t0 > 30.0:
                self.demo_phase = 1
                self.demo_t0 = self.t
                self.set_view(1)
        elif self.demo_phase == 1:                     # 页 2：每页停 2.5 s
            if self.t - self.demo_t0 >= 2.5:
                self.demo_t0 = self.t
                if self.page + 1 < PAGES:
                    self.page += 1
                else:
                    self.demo_phase = 2
                    self.demo_t0 = self.t
                    self.set_view(2)
        elif self.demo_phase == 2:                     # 页 3：钉默认信道 10 s
            if self.t - self.demo_t0 >= 10.0:
                self.demo_phase = 0
                self.demo_t0 = self.t
                self.set_view(0)

    pause = False


# ------------------------------------------------------------ 渲染出口 --
def render_wfall_hist(scr, m):
    """页 3 带真历史环的瀑布（round_ui.c view_wfall 逐句镜像）。

    与 round_sim.render_wfall 的差别只有一处：下半的 41 行不再全部取当前帧，
    而是从 DeskModel 的 64 行环里取（新行在下、向上变旧），换信道处画 0x8410
    全宽分隔线（方案 A：不清历史）。"""
    RS.draw_head(scr, "WATERFALL")
    peak, peak_bin = 0, 0
    for c in range(min(RS.RD_SPEC_N, RS.RD_WF_N)):
        if m.spec[c] > peak:
            peak, peak_bin = int(m.spec[c]), c
    wx, bw = 56, 2
    spec_y, spec_h = 72, 26
    base_y = spec_y + spec_h + 2
    wf_rows, wf_h = 41, 2
    for c in range(min(RS.RD_SPEC_N, RS.RD_WF_N)):
        h = int(m.spec[c]) * spec_h // RD_HEAT_FULL
        if h > spec_h:
            h = spec_h
        if h > 0:
            scr.rect(wx + c * bw, spec_y + spec_h - h, bw - 1, h, RS.wf_color(int(m.spec[c])))
    if peak > 0:
        scr.rect(wx + peak_bin * bw, spec_y, 2, spec_h, 0xFFFF)
    if m.win_mhz:
        lo_mhz = int(m.win_mhz - RATE / 2e6)
        hi_mhz = int(m.win_mhz + RATE / 2e6)
        scr.text(28, 52, str(lo_mhz), RS.pal(72))
        scr.text(RS.RD_W - 28 - scr.text_w(str(hi_mhz)), 52, str(hi_mhz), RS.pal(72))
        scr.text(104, 52, "MHz", RS.pal(72))
    scr.rect(wx, base_y, RS.RD_WF_N * bw, 1, 0x4208)
    for i in range(8):                                  # 高低色轴（与柱体同款颜色）
        v = (i + 1) * RD_HEAT_FULL // 8
        scr.rect(190, 78 + (7 - i) * 3, 6, 3, RS.wf_color(v))
    scr.text(199, 74, "HI", RS.pal(72))
    scr.text(199, 104, "LO", RS.pal(72))
    if m.wf_pinned:                                     # 右栏 30 格滑块（与页 2 同款）
        bh = max(4, RD_WF_BAR_H // NSCAN)
        by = RD_WF_BAR_Y + (RD_WF_BAR_H - bh) * m.wf_ch // (NSCAN - 1)
        scr.rect(RD_WF_BAR_X, RD_WF_BAR_Y, RD_WF_BAR_W, RD_WF_BAR_H, 0x2124)
        scr.rect(RD_WF_BAR_X, by, RD_WF_BAR_W, bh, 0xCE79)
        tag = f"PIN {m.wf_ch + 1}/{NSCAN} {RS.SCAN_CH[m.wf_ch]}"
        scr.text((RS.RD_W - scr.text_w(tag)) // 2, 188, tag, RS.pal(72))
    for r in range(wf_rows):                            # 时间瀑布（新行在下）
        y = base_y + 2 + r * wf_h
        src = (m.wf_head - (wf_rows - 1 - r) + RS.RD_WF_N * 2) % RS.RD_WF_N
        if m.wf_sep[src >> 3] & (1 << (src & 7)):
            scr.rect(wx, y, RS.RD_WF_N * bw, wf_h, 0x8410)
            continue
        for c in range(RS.RD_WF_N):
            v = int(m.wf[src][c])
            if v:
                scr.rect(wx + c * bw, y, bw - 1, wf_h, RS.wf_color(v))


def frame_of(m):
    """把 DeskModel 的测量数组打包成 round_sim.Frame（渲染入口）。"""
    fr = RS.Frame({
        "view": m.view,
        "cols": [0] * 24,                      # 数组随后整体覆盖，不走 24 列插值
        "win_mhz": int(m.win_mhz),
        "win_rate": RATE,
        "stats": {"noise": int(m.noise),
                  "lvl_max": int(m.ring_lvl.max()),
                  "lvl_avg": float(m.ring_lvl.mean())},
        "ring": {"cells": RS.RD_GRID, "pmax": int(m.pmax),
                 "rl_max": int(m.ring_lvl.max()),
                 "rl_mid": int(np.sort(m.ring_lvl)[len(m.ring_lvl) // 2]),
                 "warm": min(255, m.frame_no)},
    })
    fr.heat = m.heat
    fr.lvl = m.ring_lvl
    fr.spec = m.spec
    fr.win_mhz = int(m.win_mhz)
    fr.win_rate = RATE
    return fr


def draw_view(scr, m):
    """按模型当前视图画整屏（复用 round_sim 已过真板的渲染函数）。"""
    if m.view == 0:
        RS.render_ring(scr, frame_of(m))
    elif m.view == 1:
        RS.render_chart(scr, frame_of(m), m.page)
    else:
        render_wfall_hist(scr, m)


# ------------------------------------------------------------ 无头自检 --
def _save_png(m, path, zoom=2):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from PIL import Image
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    scr = RS.Screen()
    draw_view(scr, m)
    Image.fromarray(scr.rgb888()).resize((240 * zoom, 240 * zoom), Image.NEAREST).save(path)


def smoke():
    """无头跑两个环境 + 手势/瀑布断言，出三页 PNG。"""
    import tempfile
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name} {detail}")
        ok = ok and cond

    # ---- ① 日常 WiFi：占用收敛 + 热力 + 手势 ----
    m = DeskModel(preset="日常 WiFi")
    for _ in range(900):                       # 45 s 仿真 ≈ 15 圈自扫
        m.tick()
    occ5785 = RS.occ_of(5785) or 0
    occ2442 = RS.occ_of(2442) or 0    # 重度 AP 在 2437，被 2442 窗（2432~2452）覆盖
    occ2412 = RS.occ_of(2412) or 0
    check("5785 图传占用 >35%(HEAVY)", occ5785 > 35, f"={occ5785}%")
    check("2442 窗(含 2437 重度 AP)占用 >15%(CROWD)", occ2442 > 15, f"={occ2442}%")
    check("2412 轻度 AP 占用 >3%(LIGHT)", occ2412 > 3, f"={occ2412}%")
    g5785 = RS.freq_to_grid(5785 * 1_000_000)
    check("5785 热力有记忆(>=2)", int(m.heat[g5785]) >= 2, f"={int(m.heat[g5785])}")
    g5180 = RS.freq_to_grid(5180 * 1_000_000)
    check("5180 无人热力归 0", int(m.heat[g5180]) == 0, f"={int(m.heat[g5180])}")
    check("30 个被扫信道进占用表", sum(1 for mh in RS.SCAN_CH if mh in RS.OCC) == NSCAN,
          f"={sum(1 for mh in RS.SCAN_CH if mh in RS.OCC)}")
    # ---- ② 安静环境：占用全归零 ----
    q = DeskModel(preset="安静环境")
    for _ in range(900):
        q.tick()
    q.settle_occ()
    mx = max((RS.occ_of(mhz) or 0) for mhz in RS.SCAN_CH)
    check("安静环境占用 <=2%", mx <= 2, f"max={mx}%")
    check("安静环境热力 <=2", int(q.heat.max()) <= 2, f"max={int(q.heat.max())}")

    # ---- ③ 手势 FSM ----
    g = DeskModel(preset="安静环境")
    g.tap_left()
    check("左条点击 0->1", g.view == 1)
    g.swipe(up=True)
    check("页2 上滑翻页 1->2", g.page == 1)
    g.swipe(up=False)
    check("页2 下滑回页 2->1", g.page == 0)
    g.tap_left()
    check("左条点击 1->2(自动钉信道)", g.view == 2 and g.wf_pinned)
    ch0 = g.wf_ch
    g.swipe(up=True)
    check("页3 上滑换后一信道", g.wf_ch == (ch0 + 1) % NSCAN)
    check("换信道打分隔线", int(g.wf_sep.any()))
    g.tap_left()
    check("左条点击 2->0(恢复整表自扫)", g.view == 0 and not g.wf_pinned)
    # 小方块直选（y>=200, x=104/120/136 -> 视图 1/2/3）
    g.gesture(136, 224, 136, 224)
    check("小方块直选第 3 块 -> 视图 3", g.view == 2)

    # ---- ④ 瀑布历史环 ----
    w = DeskModel(preset="日常 WiFi")
    w.set_view(2)
    for _ in range(300):                       # 15 s -> 应推 ~100 行（环 64 行回卷）
        w.tick()
    check("瀑布环推进(回卷后 head 覆盖)", int(w.wf[w.wf_head].max()) > 0)
    check("瀑布历史行非空", int(w.wf.max()) > 0)
    # ---- ⑤ 注入突发 ----
    b = DeskModel(preset="安静环境")
    b.radio.inject(5520, dur=3.0, t=b.t)
    for _ in range(120):
        b.tick()
    check("注入突发 -> 5520 占用上升", (RS.occ_of(5520) or 0) > 5, f"={RS.occ_of(5520)}%")

    # ---- ⑥ 轮播每页秒数 + 强切（RD VIEWSEC 口径，2026-11-25）----
    m.view = 0
    m.demo = False
    m.view_sec = [8.0, 25.0, 8.0]
    m._touch()
    m.t += 3.0
    m._carousel()
    check("未到点不切", m.view == 0)
    m.t += 6.0                       # 页 0 驻留满 8 s
    m._carousel()
    check("到点强切 -> 页 2", m.view == 1)      # 从 0 转，第一个非零页是 1
    m.view_sec = [8.0, 0.0, 1.0]     # 停在页 2（0 秒）：到点立即跳到页 3
    m._touch()
    m.t += 2.0
    m._carousel()
    check("跳 0 秒页 -> 页 3", m.view == 2)
    m.view_sec = [0.0, 0.0, 0.0]
    m.t += 100.0
    m._carousel()
    check("全 0 = 轮播关（停在页 3）", m.view == 2)
    m.view_sec = [8.0, 25.0, 8.0]

    # ---- ⑦ 三页 PNG ----
    out = Path(tempfile.mkdtemp(prefix="desk_smoke_"))
    for v in range(3):
        m.view = v
        if v == 2:
            m.wf_pinned = True
        _save_png(m, out / f"desk_smoke_view{v}.png")
    print(f"  PNG -> {out}")
    print("ALL OK" if ok else "FAILED")
    return 0 if ok else 1


# ------------------------------------------------------------ 交互台 GUI --
def run_gui(preset="日常 WiFi", zoom=2):
    import os
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ.pop("QT_QPA_PLATFORM", None)   # --smoke 的无头变量别带进真窗口
    from PySide6 import QtCore, QtGui, QtWidgets

    class Canvas(QtWidgets.QLabel):
        """240x240 屏：鼠标按下/抬起 -> 固件同款手势 FSM；1/2/3、方向键、空格、S。"""

        def __init__(self, win):
            super().__init__()
            self.win = win
            self._p = None
            self.setFocusPolicy(QtCore.Qt.StrongFocus)

        def mousePressEvent(self, ev):
            self._p = (int(ev.position().x() / self.win.zoom),
                       int(ev.position().y() / self.win.zoom))

        def mouseReleaseEvent(self, ev):
            if self._p is None:
                return
            q = (int(ev.position().x() / self.win.zoom),
                 int(ev.position().y() / self.win.zoom))
            r = self.win.m.gesture(*self._p, *q)
            if r:
                self.win.console(r)
            self._p = None

        def keyPressEvent(self, ev):        # PySide6 必须子类化重写（实例赋值无效）
            k = ev.key()
            w = self.win
            if k in (QtCore.Qt.Key_1, QtCore.Qt.Key_2, QtCore.Qt.Key_3):
                w.m.set_view(k - QtCore.Qt.Key_1)
            elif k == QtCore.Qt.Key_Up:
                w.m.swipe(up=True)
            elif k == QtCore.Qt.Key_Down:
                w.m.swipe(up=False)
            elif k == QtCore.Qt.Key_Left:
                w.m.tap_left()
            elif k == QtCore.Qt.Key_Space:
                w.chk_pause.setChecked(not w.chk_pause.isChecked())
            elif k == QtCore.Qt.Key_S:
                w.save_pngs()
            else:
                super().keyPressEvent(ev)

    class Desk(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.m = DeskModel(preset=preset)
            self.zoom = zoom
            self.setWindowTitle(f"圆屏上位机调试台 · 活数据仿真 · 环境[{preset}] 240x240")
            self.setStyleSheet(
                "QPushButton{padding:4px 10px;} QLabel#hud{font-family:Consolas,monospace;"
                "font-size:12px;} QPlainTextEdit{font-family:Consolas,monospace;font-size:11px;}")
            self.cv = Canvas(self)
            self.cv.setFixedSize(240 * zoom, 240 * zoom)
            self.hud = QtWidgets.QLabel("...")
            self.hud.setObjectName("hud")
            left = QtWidgets.QVBoxLayout()
            left.addWidget(self.cv)
            left.addWidget(self.hud)
            # ---------------- 右侧控制面板 ----------------
            panel = QtWidgets.QVBoxLayout()
            gb1 = QtWidgets.QGroupBox("视图（=固件手势）")
            h1 = QtWidgets.QHBoxLayout()
            self.btn_view = [QtWidgets.QPushButton(f"页{i + 1}{n}") for i, n in
                             enumerate(("热力图", "占用榜", "瀑布"))]
            for i, b in enumerate(self.btn_view):
                b.clicked.connect(lambda _=False, i=i: self.act(self.m.set_view, i))
                h1.addWidget(b)
            b = QtWidgets.QPushButton("点左条(下一页)")
            b.clicked.connect(lambda: self.act(self.m.tap_left))
            h1.addWidget(b)
            gb1.setLayout(h1)
            panel.addWidget(gb1)

            gb2 = QtWidgets.QGroupBox("手势模拟（页2=翻页 页3=换信道）")
            h2 = QtWidgets.QHBoxLayout()
            bu = QtWidgets.QPushButton("上滑")
            bd = QtWidgets.QPushButton("下滑")
            bu.clicked.connect(lambda: self.act(self.m.swipe, True))
            bd.clicked.connect(lambda: self.act(self.m.swipe, False))
            h2.addWidget(bu)
            h2.addWidget(bd)
            gb2.setLayout(h2)
            panel.addWidget(gb2)

            gb3 = QtWidgets.QGroupBox("页2 翻页 / 页3 钉信道")
            h3 = QtWidgets.QHBoxLayout()
            h3.addWidget(QtWidgets.QLabel("页:"))
            self.sp_page = QtWidgets.QSpinBox()
            self.sp_page.setRange(1, PAGES)
            self.sp_page.valueChanged.connect(
                lambda v: setattr(self.m, "page", v - 1))
            h3.addWidget(self.sp_page)
            h3.addWidget(QtWidgets.QLabel("信道:"))
            self.sp_ch = QtWidgets.QSpinBox()
            self.sp_ch.setRange(1, NSCAN)
            h3.addWidget(self.sp_ch)
            bp = QtWidgets.QPushButton("钉住并看瀑布")
            bp.clicked.connect(lambda: self.act(self.m.pin_channel, self.sp_ch.value() - 1))
            h3.addWidget(bp)
            gb3.setLayout(h3)
            panel.addWidget(gb3)

            gb4 = QtWidgets.QGroupBox("射频环境（合成采样源）")
            v4 = QtWidgets.QVBoxLayout()
            h4a = QtWidgets.QHBoxLayout()
            self.cb_preset = QtWidgets.QComboBox()
            self.cb_preset.addItems(list(PRESETS))
            self.cb_preset.setCurrentText(preset)
            self.cb_preset.currentTextChanged.connect(self.represet)
            h4a.addWidget(QtWidgets.QLabel("环境:"))
            h4a.addWidget(self.cb_preset)
            v4.addLayout(h4a)
            h4b = QtWidgets.QHBoxLayout()
            h4b.addWidget(QtWidgets.QLabel("增益(60=标定):"))
            self.sl_gain = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            self.sl_gain.setRange(30, 81)
            self.sl_gain.setValue(60)
            self.sl_gain.valueChanged.connect(
                lambda v: setattr(self.m, "gain", v))
            h4b.addWidget(self.sl_gain)
            self.lb_gain = QtWidgets.QLabel("60")
            h4b.addWidget(self.lb_gain)
            v4.addLayout(h4b)
            h4c = QtWidgets.QHBoxLayout()
            bi = QtWidgets.QPushButton("在当前窗注入突发(2s)")
            bi.clicked.connect(self.inject)
            h4c.addWidget(bi)
            v4.addLayout(h4c)
            gb4.setLayout(v4)
            panel.addWidget(gb4)

            gb5 = QtWidgets.QGroupBox("运行")
            v5 = QtWidgets.QVBoxLayout()
            h5a = QtWidgets.QHBoxLayout()
            h5a.addWidget(QtWidgets.QLabel("速度:"))
            self.cb_speed = QtWidgets.QComboBox()
            self.cb_speed.addItems(["1x", "2x", "4x"])
            self.cb_speed.currentTextChanged.connect(self.respeed)
            h5a.addWidget(self.cb_speed)
            self.sp_sec = []
            for i in range(3):
                sp = QtWidgets.QSpinBox()
                sp.setRange(0, 255)
                sp.setValue(int(self.m.view_sec[i]))
                sp.setSuffix(" s")
                sp.setToolTip(f"页 {i + 1} 轮播驻留秒数；0=不停留，全 0=轮播关")
                sp.valueChanged.connect(
                    lambda val, ix=i: (setattr(self.m.view_sec, ix, 0) or True)
                    and None)
                sp.valueChanged.connect(lambda val, ix=i: self.set_viewsec(ix, val))
                self.sp_sec.append(sp)
                h5a.addWidget(QtWidgets.QLabel(f"页{i + 1}"))
                h5a.addWidget(sp)
            self.chk_demo = QtWidgets.QCheckBox("演示导览")
            self.chk_demo.toggled.connect(self.act_set_demo)
            h5a.addWidget(self.chk_demo)
            v5.addLayout(h5a)
            h5b = QtWidgets.QHBoxLayout()
            self.chk_pause = QtWidgets.QCheckBox("暂停")
            self.chk_pause.toggled.connect(self.act_pause)
            h5b.addWidget(self.chk_pause)
            br = QtWidgets.QPushButton("重置占用")
            br.clicked.connect(lambda: self.act(self.m.reset_occ))
            bs = QtWidgets.QPushButton("保存三页PNG")
            bs.clicked.connect(self.save_pngs)
            h5b.addWidget(br)
            h5b.addWidget(bs)
            v5.addLayout(h5b)
            gb5.setLayout(v5)
            panel.addWidget(gb5)

            self.txt = QtWidgets.QPlainTextEdit()
            self.txt.setReadOnly(True)
            self.txt.setMaximumBlockCount(200)
            panel.addWidget(self.txt)
            panel.addStretch(1)

            root = QtWidgets.QHBoxLayout(self)
            root.addLayout(left, 0)
            root.addLayout(panel, 1)

            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.respeed("1x")

        # ---------------- GUI 行为 ----------------
        def act(self, fn, *a):
            r = fn(*a)
            if isinstance(r, str):
                self.console(r)

        def set_viewsec(self, ix, val):
            self.m.view_sec[ix] = float(val)
            self.m._touch()                      # 改设定后当前页重新驻留
            self.say(f"页 {ix + 1} 轮播 {val} s"
                     + ("（轮播关）" if val == 0 and not any(self.m.view_sec) else ""))

        def act_set_demo(self, on):
            self.m.set_demo(on)

        def act_pause(self, on):
            self.m.pause = bool(on)

        def represet(self, name):
            self.m.preset = name
            self.m.radio = FakeRadio(name)
            self.m.say(f"射频环境 -> {name}")

        def respeed(self, name):
            fps, dwell = {"1x": (20.0, 2), "2x": (20.0, 1), "4x": (40.0, 1)}[name]
            self.m.fps = fps
            self.m.dwell_ticks = dwell
            self.timer.start(int(1000 / fps))

        def inject(self):
            self.m.radio.inject(self.m.win_mhz, dur=2.0, t=self.m.t)
            self.console(f"注入突发 @ {self.m.win_mhz} MHz（2 s）")

        def save_pngs(self):
            from PIL import Image
            base = Path(__file__).resolve().parent.parent / "build"
            base.mkdir(exist_ok=True)
            scr = RS.Screen()
            for v in range(3):
                saved = (self.m.view, self.m.page, self.m.wf_pinned)
                self.m.view = v
                if v == 2:
                    self.m.wf_pinned = True
                draw_view(scr, self.m)
                Image.fromarray(scr.rgb888()).resize(
                    (240 * self.zoom, 240 * self.zoom), Image.NEAREST
                ).save(base / f"desk_view{v}.png")
                self.m.view, self.m.page, self.m.wf_pinned = saved
            self.console(f"已存 build/desk_view0..2.png")

        def console(self, msg):
            self.txt.appendPlainText(f"[{self.m.t:7.1f}s] {msg}")

        def tick(self):
            m = self.m
            n0 = len(m.log)
            m.tick()
            for line in m.log[n0:]:
                self.txt.appendPlainText(line)
            scr = RS.Screen()
            t0 = time.perf_counter()
            draw_view(scr, m)
            dt_ms = (time.perf_counter() - t0) * 1000
            img = scr.rgb888()
            from PySide6 import QtGui
            qim = QtGui.QImage(img.data, 240, 240, 240 * 3,
                                QtGui.QImage.Format_RGB888)
            self.cv.setPixmap(QtGui.QPixmap.fromImage(qim).scaled(
                240 * self.zoom, 240 * self.zoom,
                QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.FastTransformation))
            # HUD：镜像 RD? 能看到的量 + 本机渲染耗时
            occ_now = RS.occ_of(int(m.win_mhz))
            word = RS.occ_word(occ_now) if occ_now is not None else "--"
            band = ("2.4G" if m.win_mhz < 3000 else
                    "5G-UNII-1" if m.win_mhz < 5250 else
                    "5G-UNII-2A" if m.win_mhz < 5350 else
                    "5G-UNII-2C" if m.win_mhz < 5725 else "5G-UNII-3")
            self.hud.setText(
                f"帧 {m.frame_no}  {m.fps:.0f}fps  sim渲染 {dt_ms:.1f}ms\n"
                f"窗 {m.win_mhz} MHz [{band}]  第 {m.scan_i + 1}/{NSCAN} 窗"
                f"  底噪 {m.noise}  pmax {m.pmax}\n"
                f"圆心占用 {occ_now if occ_now is not None else '--'}% {word}"
                f"   视图 {m.view + 1}/3  页 {m.page + 1}/{PAGES}\n"
                f"[1/2/3]切页 [←]左条 [↑↓]滑 [空格]暂停 [S]存图")

    desk = Desk()
    desk.resize(240 * zoom + 480, 240 * zoom + 140)
    desk.show()
    return desk


# ---------------------------------------------------------------- 入口 --
def main():
    ap = argparse.ArgumentParser(description="圆屏上位机调试台（活数据交互仿真）")
    ap.add_argument("--preset", default="日常 WiFi", choices=list(PRESETS))
    ap.add_argument("--zoom", type=int, default=2)
    ap.add_argument("--smoke", action="store_true", help="无头自检 + 出三页 PNG")
    args = ap.parse_args()
    if args.smoke:
        sys.exit(smoke())
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication(sys.argv)
    run_gui(preset=args.preset, zoom=args.zoom)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
