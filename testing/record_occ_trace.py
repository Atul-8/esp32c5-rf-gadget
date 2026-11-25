#!/usr/bin/env python
"""采一份「信道占用率」真板 trace，供 testing/round_sim.py 重放。

为什么需要它：圆屏圆心的口径是**时间占空比** —— 一个窗采若干帧，帧内
`(中位 − 25%分位) > 15 码`（只取窗内中间一半 bin）判为「有传输」，
占空比 = 命中帧/总帧。上位机仿真屏要画出同样的数字，就必须拿到**逐帧原始
功率码**；`build/board_trace3.json` 里只有 24 列聚合值，算不出来。

副产品：这个脚本同时是板端口径的**独立对账**。
  * 板端算：`RD OCC?`（设备自扫，8 帧/窗，α=1/8 EMA，一轮 ~3 s）
  * 主机算：本脚本用同一判据、同一配置逐信道现算
两边口径相同但**统计窗口差两个量级**（主机 8 帧≈0.4 s 快照 vs 板端 τ≈24 s），
实测同一信道 25 s 内板端就能从 33% 飘到 0%、主机单窗在 0~12% 之间跳 ——
所以不逐位比，只比「同一次采样里哪个信道更高」，并单独量板端自漂：
板端起止两次差 >20 百分点 ⇒ 标 volatile（时变，不是口径错）。

    python testing/record_occ_trace.py --port COM56
    python testing/record_occ_trace.py --port COM56 --frames 8 --dwell 600

产出：
    build/occ_trace.json   逐信道逐帧原始码（hex）+ p25/p50/命中/占空比 + 板端读数
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples" / "esp32c5-rf-monitor"))

from core.registry import ScanRequest                        # noqa: E402
from core.serial_link import EspSdrLink                       # noqa: E402

OUT = ROOT / "build" / "occ_trace.json"

# 与固件 rd_auto_scan()（main/common/round_ui.c）的信道表保持一致，
# 否则仿真屏会画出一个板子根本没扫的信道。
CH_MHZ = [
    2412, 2427, 2442, 2457, 2472,
    5180, 5200, 5220, 5240, 5260, 5280, 5300, 5320,
    5500, 5520, 5540, 5560, 5580, 5600, 5620, 5640, 5660, 5680, 5700, 5720,
    5745, 5765, 5785, 5805, 5825,
]

# 与固件一致：512 bins @ 40 MS/s、det 0、门限 15 码、只取中间一半。
BINS = 512
RATE_HZ = 40_000_000
THR = 15        # OCC_DB，见 main/common/spectrum.c


def quantile(vals, num: int, den: int) -> int:
    """与固件 hist_quantile() 同构：整数直方图分位（升序第 bins*num/den 个样本）。"""
    hist = [0] * 256
    for v in vals:
        hist[max(0, min(255, int(v)))] += 1
    target = max(1, len(vals) * num // den)
    acc = 0
    for code, n in enumerate(hist):
        acc += n
        if acc >= target:
            return code
    return 255


def judge(codes) -> tuple[int, int, int]:
    """一帧原始码 → (p25, p50, 是否判为被占)。只取窗内中间一半。"""
    n = len(codes)
    mid = codes[n // 4: n - n // 4]
    p25 = quantile(mid, 1, 4)
    p50 = quantile(mid, 1, 2)
    return p25, p50, 1 if (p50 - p25) > THR else 0


def board_occ(link: EspSdrLink) -> dict[int, int]:
    """读板端自己的占空比读数（需要设备自扫在跑）。"""
    line = link.command("RD OCC?").strip()
    out: dict[int, int] = {}
    for tok in line.split()[1:]:
        mhz, _, pct = tok.partition(":")
        if mhz.isdigit() and pct.lstrip("-").isdigit():
            out[int(mhz)] = int(pct)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM56", help="只允许板子自己的口，默认 COM56")
    ap.add_argument("--gain", type=int, default=60, help="固定增益档（PHY 表索引）")
    ap.add_argument("--frames", type=int, default=8, help="每窗采几帧（固件是 8）")
    ap.add_argument("--dwell", type=int, default=900, help="主机驻留 ms（够采满 --frames）")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--no-exec", action="store_true", help="只算不写盘")
    ap.add_argument("--only", default="", help="只采这些信道（逗号分隔 MHz），调试用")
    ap.add_argument("--warmup", type=float, default=30,
                    help="开串口后等多少秒再读板端 EMA（复位会让 s_occ 从 0 爬）")
    args = ap.parse_args()

    with EspSdrLink(args.port) as link:
        ident = link.command("RD?").strip()
        if not ident.startswith("RD state="):
            print(f"警告：RD? 回复不像圆屏固件：{ident!r}", file=sys.stderr)
        print("identity:", link.command("RD?").strip())
        # 板端读数要在**停自扫之前**取：自扫一停，EMA 就不再更新。
        # 更关键的是**先等 EMA 暖机**：开串口会复位板子，`s_occ` 是 BSS，一复位就
        # 全零，然后按 α=1/8（每轮 1.8 s 一次，τ≈24 s）从 0 爬上来
        # —— 实测开串口后 10 s 读到的是稳定值的一半左右。不等就取，必然得出
        # 「主机 25% / 板端 0%」的假不一致。
        if args.warmup > 0:
            print(f"等板端 EMA 暖机 {args.warmup:.0f} s"
                  "（开串口会复位，s_occ 从 0 爬）…")
            t_w = time.time()
            while time.time() - t_w < args.warmup:
                time.sleep(3)
                print(f"  +{time.time() - t_w:4.0f}s  "
                      f"{link.command('FSM?').strip()}")
        board = board_occ(link)
        print(f"板端 RD OCC? 读到 {len(board)} 个信道（EMA 过了的值）")

        gain_line = link.command("GAIN?").strip()
        print("GAIN?", gain_line)
        if f"MANUAL {args.gain} " not in gain_line + " ":
            link.command(f"GAIN MANUAL {args.gain}")
            print("已切到", link.command("GAIN?").strip())

        # 停掉设备自扫，避免它和主机逐窗驱动抢射频（两边同时调谐会互相打断）。
        link.command("FSM STOP")
        time.sleep(0.5)

        chans = CH_MHZ
        if args.only:
            want = {int(x) for x in args.only.split(",") if x.strip()}
            chans = [c for c in CH_MHZ if c in want]

        win_rows = []
        t0 = time.time()
        for mhz in chans:
            req = ScanRequest(mhz * 1_000_000, RATE_HZ, args.dwell, BINS, detector=0)
            got = []                     # 整段驻留收下来的帧，(p25, p50, hit, hex)
            try:
                # 必须把整段驻留**读完**：中途 break 会把剩余帧留在串口缓冲里，
                # 下一条命令的应答就会读到残留帧 → 「SPEC 启动回复异常」并全线失控。
                for snap in link.scan(req):
                    codes = [int(x) for x in snap.bins_db]
                    if len(codes) != BINS:
                        continue
                    a, b, hit = judge(codes)
                    got.append((a, b, hit,
                                bytes(max(0, min(255, c)) for c in codes).hex()))
            except Exception as exc:                      # noqa: BLE001
                print(f"  {mhz} 采集失败：{exc}", file=sys.stderr)
                continue
            # 只取**驻留末尾**的 frames 帧：刚调谐的头几帧增益/DC 都没稳
            # （实测 5.2G 前 4 帧整段抬到 110~124 码，差分成 10 ⇒ 判「没占用」），
            # 而驻留末段是稳态。多出来的帧照样读完，只为不把残帧留给下一条命令。
            got = got[-args.frames:]
            if not got:
                continue
            raw = [g[3] for g in got]
            p25s = [g[0] for g in got]
            p50s = [g[1] for g in got]
            hits = sum(g[2] for g in got)
            duty = round(100.0 * hits / len(raw))
            win_rows.append({
                "mhz": mhz, "frames": len(raw), "hits": hits, "duty": duty,
                "p25": p25s, "p50": p50s, "raw": raw,
            })
            print(f"  {mhz:5d} 帧 {len(raw)} 命中 {hits} → 占空比 {duty:3d}%"
                  f"   板端 {board.get(mhz, '-')}%")
            print("        p25/p50 逐帧：" + " ".join(
                f"{a}/{b}{'*' if b - a > THR else ''}"
                for a, b in zip(p25s, p50s)))
        dt = time.time() - t0

        # 恢复设备自扫，别把演示状态留在「停扫」上。
        link.command("FSM START 512 1 0")
        # 再读一次板端：整个采集要 28 s，EMA（τ≈24 s）自己就会漂 ——
        # 不把「板端前后两次的差」量出来，就无法判断对账差到底是口径错还是时变。
        time.sleep(4)
        board2 = board_occ(link)
        print(f"板端 RD OCC? 收尾再读一次（{len(board2)} 个信道）")

    if not win_rows:
        print("没采到任何窗口，放弃写盘", file=sys.stderr)
        return 1

    doc = {
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "port": args.port,
        "gain": args.gain,
        "bins": BINS,
        "rate_hz": RATE_HZ,
        "thr": THR,
        "frames_per_window": args.frames,
        "windows": win_rows,
        "board_occ": {str(k): v for k, v in board.items()},
        "board_occ_end": {str(k): v for k, v in board2.items()},
    }
    if not args.no_exec:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        print(f"\n写入 {args.out.relative_to(ROOT)}  "
              f"（{len(win_rows)} 窗 × {args.frames} 帧，{dt:.1f}s，"
              f"{args.out.stat().st_size / 1024:.0f} KB）")

    # 对账：主机算 vs 板端算。
    # 两边统计窗口不同：主机 = 8 帧 ≈ 0.4 s 快照；板端 = α=1/8 EMA，τ ≈ 24 s。
    # 所以只判「板端前后两次都不像主机快照、且板端自己没漂」才算真不一致；
    # 板端自己漂 >20 的信道属于时变，直接标 volatile，不算 bug（实测同一信道 25 s
    # 内板端从 33% 掉到 0%、主机单窗在 0~12% 之间跳）。
    print("\n对账（主机 8 帧快照 / 板端 EMA 起止两次）：")
    bad = 0
    for row in sorted(win_rows, key=lambda r: -r["duty"]):
        b0 = board.get(row["mhz"])
        b1 = board2.get(row["mhz"])
        flag = ""
        if b0 is not None and b1 is not None and abs(b1 - b0) > 20:
            flag = f"  ~ 板端自漂 {b1 - b0:+d} 个百分点（时变，不可逐位比）"
        elif (b0 is not None and b1 is not None
              and abs(b0 - row["duty"]) > 20 and abs(b1 - row["duty"]) > 20):
            flag = "  <-- 两次都差 >20 个百分点（真不一致，要查）"
            bad += 1
        print(f"  {row['mhz']:5d}  主机 {row['duty']:3d}%   板端 "
              f"{'-' if b0 is None else f'{b0:2d}'}→{'-' if b1 is None else f'{b1:2d}'}%"
              f"{flag}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())