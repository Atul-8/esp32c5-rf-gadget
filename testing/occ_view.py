#!/usr/bin/env python
"""上位机「通道 / 频率占用」视图：把圆屏第二页（OCCUPY）那张表搬到电脑上，方便统计。

为什么单独做一个：
  圆屏只有 3 页、每页 5 行，看「哪个信道有东西」够用，但要做**其他协议**的统计
  （BLE 广播、图传、无绳电话、无人机……它们不在 WiFi 信道栅格上）就需要
  ① 全部 38 个信道一次看全 ② 连续采一段时间看起伏 ③ 数字能落到 CSV 里。

数据源：固件 `RD OCC?`（只读命令，不影响板上正在跑的自扫）
  回包 `RDOCC 2412:0 2427:12 … 5825:3`
  值 = **时间占空比**（%）：一个窗采若干帧，(中位−25%分位)>15 码 判为「有传输」，
  占空比 = 命中帧 / 总帧；固件端是 α=1/8 的 EMA。`-1` = 这个格子还没扫到。
  ⚠️ 是相对功率码，不是 dBm（本机没有绝对校准）；横向比信道必须固定增益，
  板上自扫已固定 RD_SCAN_GAIN=60，别再发 GAIN 覆盖它。

用法：
    python testing/occ_view.py --port COM56                       # 一直刷，Ctrl+C 停
    python testing/occ_view.py --port COM56 --duration 60         # 采 60 s 后打总结
    python testing/occ_view.py --port COM56 --duration 300 --csv build/occ_log.csv
    python testing/occ_view.py --port COM56 --band 24             # 只看 2.4G

读表要点：
    OCC%  当前占空比（板端 EMA，τ≈24 s，跟"立刻"没关系，慢变量）
    MAX%  本次会话见过的最高值 —— 突发协议（BLE 广播占空比极低但偶尔打满）看这列
    AVG%  会话平均
    条形  OCC% 的 20 格可视化；`!` 标 OCC>0 但 MAX 远高于 AVG（突发/间歇）
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:                                        # pragma: no cover
    sys.exit("需要 pyserial：pip install pyserial")

# 与固件 s_ch_mhz[]（38 项）同序，用来给"这个格子到底扫没扫"提供参照。
# 注意 s_scan_ch[]（真正自扫的 30 个）是它的子集：不在 s_scan_ch 里的信道
# 永远回 -1（没被扫过），不是"空闲"。
SCAN_CH = (
    2412, 2427, 2442, 2457, 2472,
    5180, 5200, 5220, 5240, 5260, 5280, 5300, 5320,
    5500, 5520, 5540, 5560, 5580, 5600, 5620, 5640, 5660, 5680, 5700, 5720,
    5745, 5765, 5785, 5805, 5825,
)

SWEPT = frozenset(SCAN_CH)      # 只有这些格子会被板端自扫更新，其余信道读到的是残留 0

BANDS = (("2.4G", 0, 3000), ("5G", 3000, 5470), ("5.8G", 5470, 7000))


def band_of(mhz: int) -> str:
    for name, lo, hi in BANDS:
        if lo <= mhz < hi:
            return name
    return "?"


def wifi_ch(mhz: int) -> str:
    """WiFi 信道号（2.4G: (f-2407)/5；5G: (f-5000)/5）。非栅格点回 '--'。"""
    if 2407 <= mhz <= 2484:
        n = (mhz - 2407) / 5
    elif 5000 < mhz <= 5925:
        n = (mhz - 5000) / 5
    else:
        return "--"
    return "%d" % round(n) if abs(n - round(n)) < 1e-6 else "--"


def bar(pct: float, width: int = 20) -> str:
    if pct < 0:
        return " " * width
    n = int(round(max(0.0, min(100.0, pct)) * width / 100.0))
    return "#" * n + "." * (width - n)


class Serial:
    def __init__(self, port: str, baud: int) -> None:
        self.ser = serial.Serial(port, baud, timeout=0.05)

    def ask(self, line: str, wait: float = 0.35) -> list[str]:
        self.ser.reset_input_buffer()
        self.ser.write((line + "\n").encode("ascii"))
        time.sleep(wait)
        raw = self.ser.read(self.ser.in_waiting or 1).decode("ascii", "replace")
        return [l for l in raw.replace("\r", "").split("\n") if l.strip()]

    def close(self) -> None:
        self.ser.close()


def wait_ready(dev: Serial, timeout_s: float = 60.0) -> int:
    """打开串口会复位板子（frames=0），必须等它自扫起来再取数。"""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(1.0)
        for l in dev.ask("RD?", 0.5):
            m = re.search(r"frames=(\d+)", l)
            if m and int(m.group(1)) > 30:
                return int(m.group(1))
    raise SystemExit("板子 60 s 内没起来（frames 一直 <=30）：先确认是不是停在 ROM 下载模式")


def read_occ(dev: Serial) -> dict[int, int]:
    out: dict[int, int] = {}
    for l in reversed(dev.ask("RD OCC?", 0.4)):
        if not l.startswith("RDOCC"):
            continue
        for tok in l.split()[1:]:
            m = re.fullmatch(r"(\d+):(-?\d+)", tok)
            if m:
                out[int(m.group(1))] = int(m.group(2))
        break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="上位机通道/频率占用表（数据源 RD OCC?）")
    ap.add_argument("--port", default="COM56", help="只允许本机约定的板口（默认 COM56）")
    ap.add_argument("--baud", type=int, default=2_000_000)
    ap.add_argument("--interval", type=float, default=2.0, help="采样间隔 s（默认 2）")
    ap.add_argument("--duration", type=float, default=0.0, help="采多久后打总结（0 = 一直刷）")
    ap.add_argument("--band", default="", choices=("", "24", "5", "58"), help="只看某个频段")
    ap.add_argument("--csv", default="", help="把每次采样追加写进 CSV")
    ap.add_argument("--quiet", action="store_true", help="只打总结，不刷表")
    args = ap.parse_args()

    want = {"": None, "24": "2.4G", "5": "5G", "58": "5.8G"}[args.band]

    dev = Serial(args.port, args.baud)
    try:
        frames = wait_ready(dev)
        info = [l for l in dev.ask("RD?", 0.4) if l.startswith("RD ")]
        print("# 板子就绪 frames=%d  %s" % (frames, info[-1] if info else ""))
        print("# 板端自扫 %d/38 个信道；其余标「未自扫」——那格永远没人写，读数是残留 0，"
              "不是空闲。" % len(SCAN_CH))
        print("# 口径：占空比 %，相对功率码（非 dBm）；增益固定 60")
        print()

        cur: dict[int, int] = {}
        hi: dict[int, int] = {}
        n: dict[int, int] = {}
        tot: dict[int, int] = {}
        t_start = time.time()
        samples = 0
        csv_rows: list[str] = []
        next_tick = t_start

        while True:
            occ = read_occ(dev)
            if occ:
                samples += 1
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                for mhz, v in occ.items():
                    cur[mhz] = v
                    if v >= 0:
                        hi[mhz] = max(hi.get(mhz, 0), v)
                        n[mhz] = n.get(mhz, 0) + 1
                        tot[mhz] = tot.get(mhz, 0) + v
                    csv_rows.append("%s,%d,%s,%d,%d"
                                    % (stamp, mhz, wifi_ch(mhz), v, 1 if mhz in SWEPT else 0))
                if not args.quiet:
                    print("\n=== t=%5.1fs  样本 %d ===" % (time.time() - t_start, samples))
                    print("  %-6s %-4s %5s %5s %5s  %s" % ("MHZ", "CH", "OCC%", "MAX%", "AVG%", "占用（20 格）"))
                    for name, lo, hi_f in BANDS:
                        if want and want != name:
                            continue
                        rows = [m for m in sorted(cur) if lo <= m < hi_f]
                        if not rows:
                            continue
                        print("  -- %s --" % name)
                        for m in rows:
                            v = cur[m]
                            if m not in SWEPT:
                                print("  %-6d %-4s %5s %5s %5s  %s"
                                      % (m, wifi_ch(m), "--", "--", "--", "(未自扫，别当空闲看)"))
                                continue
                            if v < 0:
                                print("  %-6d %-4s %5s %5s %5s  %s" % (m, wifi_ch(m), "--", "--", "--",
                                                                       "(未扫：不在自扫表里)"))
                                continue
                            avg = tot[m] / max(1, n[m])
                            mark = "!" if (v > 0 and hi.get(m, 0) >= max(4, 3 * max(1, v))) else " "
                            print("  %-6d %-4s %5d %5d %5.1f %s %s"
                                  % (m, wifi_ch(m), v, hi.get(m, 0), avg, bar(v), mark))
            if args.duration and time.time() - t_start >= args.duration:
                break
            next_tick += args.interval
            time.sleep(max(0.0, next_tick - time.time()))
    except KeyboardInterrupt:
        print("\n# Ctrl+C 结束")
    finally:
        dev.close()

    if not cur:
        print("没读到任何 RD OCC? 回包")
        return 1

    print("\n" + "=" * 62)
    print("总结（按会话最高值排序；OCC/AVG 为会话均值口径）")
    print("=" * 62)
    ranked = sorted((m for m in cur if m in SWEPT and cur[m] > 0), key=lambda m: -hi.get(m, 0))
    for m in ranked:
        avg = tot[m] / max(1, n[m])
        print("  %-6d %-4s %-5s OCC %3d%%  MAX %3d%%  AVG %5.1f%%  %s"
              % (m, wifi_ch(m), band_of(m), cur[m], hi[m], avg, bar(avg)))
    quiet = [m for m in cur if m in SWEPT and cur[m] == 0]
    unscanned = [m for m in cur if m not in SWEPT]
    print("\n  0%%（扫过、没检测到传输）：%d 个 %s" % (len(quiet), quiet))
    print("  未自扫（没有数据，不是空闲）：%d 个 %s" % (len(unscanned), unscanned))
    print("  样本 %d 次，间隔 %.1f s" % (samples, args.interval))

    if args.csv:
        p = Path(args.csv)
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with p.open("a", encoding="utf-8", newline="") as f:
            if new:
                f.write("time,mhz,wifi_ch,occ_pct,swept\n")
            f.write("\n".join(csv_rows) + "\n")
        print("  已追加 %d 行到 %s" % (len(csv_rows), p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())