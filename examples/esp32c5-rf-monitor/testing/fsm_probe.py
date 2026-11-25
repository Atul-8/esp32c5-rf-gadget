# -*- coding: utf-8 -*-
"""真板探针：验设备端扫频状态机（FSM）是不是真的在自扫。

只走 COM56（303A:1001）。跑法（在本目录 ``testing/`` 下）::

    python fsm_probe.py                # 默认 3 个 2.4G 窗、512 bin、20 MS/s
    python fsm_probe.py --windows 6    # 多扫几个窗

验的是四件事（哪条不成立就说明状态机没通）：

1. ``CAPS`` 里有 ``FSM`` 标记；
2. ``FSMPLAN ADD`` 能存计划，``FSM PLAN?`` 能读回来；
3. ``FSM START`` 之后**不用主机再发 FREQ/SPEC**，串口上会自己冒出
   ``SPEC <bins> <fs> <bins> <mhz>`` 头行 + ``SPC1`` 帧，且 mhz 按计划轮转；
4. ``FSM STOP`` 之后帧流停止，``FSMEND`` 收尾。

本脚本只读串口、不改任何存证数据。**它不是单测**（依赖真板），别塞进 tests/。
"""
from __future__ import annotations

import argparse
import struct
import sys
import time

import serial

PORT = "COM56"
BAUD = 2_000_000

SPECTRUM_MAGIC = b"SPC1"
HEADER_BYTES = 28
TRAILER_BYTES = 4


def open_port() -> serial.Serial:
    port = serial.Serial(PORT, BAUD, timeout=2.0)
    time.sleep(0.3)
    port.reset_input_buffer()
    # 真板开机首帧偶尔是半个中文字符：对齐到第一个 ASCII 命令之前
    for _ in range(3):
        try:
            port.write(b"SYNC 1\n")
            port.flush()
            line = port.readline()
            if b"SYNC" in line:
                return port
        except UnicodeEncodeError:  # pragma: no cover - 只在真板异常帧出现
            time.sleep(1.0)
    raise SystemExit("串口没对上（先确认没有别的进程占着 COM56）")


def command(port: serial.Serial, text: str, expect: str = "", tries: int = 6) -> str:
    port.write((text + "\n").encode("ascii"))
    port.flush()
    for _ in range(tries):
        line = port.readline().decode("ascii", "replace").strip()
        if not line:
            continue
        if not expect or line.startswith(expect):
            return line
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--bins", type=int, default=512)
    parser.add_argument("--rate", type=int, default=2, help="采样率码（2 = 20 MS/s）")
    parser.add_argument("--dwell", type=int, default=60, help="每窗驻留毫秒（真板一轮 ≈4 窗/秒）")
    parser.add_argument("--seconds", type=float, default=6.0)
    args = parser.parse_args()

    port = open_port()
    checks: dict[str, bool] = {}
    try:
        caps = command(port, "CAPS", "CAPS")
        checks["caps_has_fsm"] = " FSM" in caps
        print(f"[CAPS] {'FSM' if checks['caps_has_fsm'] else '缺 FSM'}")

        command(port, "FSMPLAN CLR", "OK")
        freqs = [2405 + 5 * j for j in range(args.windows)]
        for mhz in freqs:
            got = command(port, f"FSMPLAN ADD {mhz} {args.dwell} 0", "OK")
            if got != "OK":
                raise SystemExit(f"FSMPLAN ADD {mhz} 失败：{got!r}")
        head = command(port, "FSM PLAN?", "FSMPLAN")
        rows = []
        for _ in range(args.windows):
            rows.append(port.readline().decode("ascii", "replace").strip())
        command(port, "FSMPLAN?", "FSMPLAN")  # 再把尾巴读干净
        stored = [int(r.split()[2]) for r in rows if r.startswith("W ")]
        checks["plan_roundtrip"] = stored == freqs
        print(f"[PLAN] {head} 存回 = {stored}")

        started = command(port, f"FSM START {args.bins} {args.rate} 0 0", "FSM START")
        checks["start_ack"] = started.startswith("FSM START")
        print(f"[START] {started}")

        frames: list[tuple[int, int, float]] = []
        deadline = time.time() + args.seconds
        pending_freq = 0
        while time.time() < deadline:
            magic = port.read(4)
            if len(magic) < 4:
                continue
            if magic == b"SPEC":
                line = port.readline().decode("ascii", "replace").split()
                if len(line) >= 4:
                    pending_freq = int(line[3])
                continue
            if magic != SPECTRUM_MAGIC:
                continue
            body = port.read(HEADER_BYTES - 4 + args.bins + TRAILER_BYTES)
            if len(body) < HEADER_BYTES - 4 + args.bins:
                continue
            frames = frames
            n_bins = struct.unpack_from("<I", body, 12)[0]
            frames.append((pending_freq, n_bins, time.time()))

        checks["frames_flowed"] = len(frames) >= max(2, args.windows)
        seen = sorted({f[0] for f in frames})
        checks["freq_rotated"] = seen == sorted(freqs)
        print(f"[FRAMES] {len(frames)} 帧 / 频点 {seen} / bins {sorted({f[1] for f in frames})}")
        if len(frames) >= 2:
            span = frames[-1][2] - frames[0][2]
            print(f"[RATE] {len(frames) / span:.2f} 窗/秒")

        # 停止时串口里还压着若干帧（设备不会因为主机不读就丢帧）→ 必须边排空边找 FSMEND，
        # 直接 readline 只会读到二进制帧的碎片。
        port.write(b"FSM STOP" + b"\n")
        port.flush()
        stopped = ""
        end = time.time() + 3.0
        while time.time() < end and not stopped:
            magic = port.read(4)
            if len(magic) < 4:
                continue
            if magic == SPECTRUM_MAGIC:
                port.read(HEADER_BYTES - 4 + args.bins + TRAILER_BYTES)
                continue
            tail = magic + port.readline()
            text = tail.decode("ascii", "replace").strip()
            if text.startswith("FSMEND"):
                stopped = text
        checks["stop_ack"] = stopped.startswith("FSMEND")
        print(f"[STOP] {stopped}")
        status = command(port, "FSM?", "FSM")
        print(f"[STATUS] {status}")
        checks["idle_after_stop"] = " idle " in f" {status} "

        # 停稳之后帧流必须真的停：再读 1 s，不该再有 SPC1
        leftover = 0
        end = time.time() + 1.0
        while time.time() < end:
            junk = port.read(4)
            if junk == SPECTRUM_MAGIC:
                leftover += 1
        checks["no_frames_after_stop"] = leftover == 0
    finally:
        port.close()

    print("[probe] " + " ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in checks.items()))
    ok = all(checks.values())
    print("全部通过" if ok else "有失败项：" + ", ".join(k for k, v in checks.items() if not v))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())