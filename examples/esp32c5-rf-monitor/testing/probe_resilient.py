"""真板验证：串口自保层（命令前排空 + 失效自愈）。

跑法（在 ``examples/esp32c5-rf-monitor`` 下，板子在 COM56）：

    python testing/probe_resilient.py            # 默认 COM56、20 个窗
    python testing/probe_resilient.py --windows 40

验证三件事：
1. 排空不会打坏正常扫描 —— 连续 N 个窗全部出帧、状态 0；
2. ``failed = True`` 之后 ``scan()`` 能自己重同步（真板重开串口 + 重握手）；
3. 重同步之后增益与频点仍然正确（重开不会把链路留下半死状态）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.append(ROOT)
sys.path.insert(0, os.path.join(ROOT, "host"))

from core.registry import ScanRequest  # noqa: E402
from core.serial_link import EspSdrLink  # noqa: E402

from link_guard import ResilientLink  # noqa: E402

CENTERS = [2412_000_000, 2437_000_000, 2462_000_000, 5180_000_000, 5745_000_000]


def scan_once(link, freq_hz: int, dwell_ms: int, bins: int) -> tuple[int, str]:
    frames = 0
    try:
        for _snap in link.scan(ScanRequest(freq_hz, 20_000_000, dwell_ms, bins)):
            frames += 1
    except Exception as exc:  # noqa: BLE001 - 探针要把失败原样报出来
        return frames, f"{type(exc).__name__}: {exc}"
    return frames, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM56")
    ap.add_argument("--windows", type=int, default=20)
    ap.add_argument("--dwell-ms", type=int, default=100)
    ap.add_argument("--bins", type=int, default=512)
    ap.add_argument("--gain-index", type=int, default=60)
    args = ap.parse_args()

    link = ResilientLink(args.port)
    link.open()
    ok = True
    try:
        link.set_gain("MANUAL", args.gain_index)
        print(f"[link] {link.identity}  caps={len(link.caps)}  transport={link.transport}")

        # 1) 排空不打坏正常扫描
        total = 0
        fails: list[str] = []
        t0 = time.time()
        for i in range(args.windows):
            freq = CENTERS[i % len(CENTERS)]
            frames, err = scan_once(link, freq, args.dwell_ms, args.bins)
            total += frames
            if err:
                fails.append(f"window {i} @{freq // 1_000_000}MHz: {err}")
        dt = time.time() - t0
        print(f"[1] {args.windows} 窗：{total} 帧 / {dt:.1f}s / 失败 {len(fails)}")
        for line in fails[:5]:
            print(f"    ! {line}")
        if fails:
            ok = False

        # 2) 失效自愈：手动把 failed 置位，scan() 应自己重开串口
        link.failed = True
        before = link.reopen_count
        frames, err = scan_once(link, 2437_000_000, args.dwell_ms, args.bins)
        print(
            f"[2] failed=True 之后 scan：{frames} 帧 / 重开次数 {before} → {link.reopen_count} / "
            f"failed={link.failed} / err={err or '-'}"
        )
        if frames == 0 or err:
            ok = False

        # 3) 重同步之后增益与频点仍然有效
        info = link.command("GAIN?", strict=False)
        print(f"[3] 重同步后 GAIN? → {info}")
        frames, err = scan_once(link, 5180_000_000, args.dwell_ms, args.bins)
        last = dict(getattr(link, "_last_scan", {}) or {})
        print(
            f"[3] 重同步后 5180MHz：{frames} 帧 / 表头 {last.get('freq_hz')} Hz / "
            f"bins {last.get('bins')} / status {last.get('status')} / err={err or '-'}"
        )
        if frames == 0 or err or int(last.get("freq_hz") or 0) != 5180_000_000:
            ok = False
    finally:
        try:
            link.set_gain("HARDWARE")
        except Exception:  # noqa: BLE001 - 收尾不掩盖原错误
            pass
        link.close()

    print("[OK]" if ok else "[FAIL]")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())