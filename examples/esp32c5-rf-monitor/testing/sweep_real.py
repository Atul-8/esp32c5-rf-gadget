# -*- coding: utf-8 -*-
"""真板实测：设备自扫引擎（方案 A）整条链路。

跟 fsm_probe.py 的区别：fsm_probe 只验固件协议（发命令、看字节），
这个脚本验的是**上位机这一侧**——计划下发 → 设备自己吐帧 → 主机解帧 →
五个模块各自吃到自己的窗 → 表格出内容。

跑法（仓库根）：

    python examples/esp32c5-rf-monitor/testing/sweep_real.py --port COM56 --seconds 12
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
HOST = os.path.join(SRC, "host")
for path in (SRC, HOST):
    if path not in sys.path:
        # 分析层（含 app/ 包）要排在 host/ 前面：否则 session.py 里的
        # ``from app import bind_capture`` 会撞上 host/app.py 造成循环导入
        sys.path.append(path)

import paths  # noqa: E402

paths.ensure_on_path()


from core.serial_link import EspSdrLink  # noqa: E402
from session import HostConfig, HostSession, to_json  # noqa: E402


def session_windows(args: argparse.Namespace) -> list[tuple[int, int, int]]:
    """跟 ``HostSession`` 一样的计划表生成逻辑（只为了 raw 测量先跑一遍）。"""
    session = HostSession(bins_out=256)
    try:
        windows, _bins, _bw = session._sweep_windows(None)
    finally:
        session.close()
    cap = max(1, int(getattr(args, "plan_dwell", 20)))
    return [(mhz, max(1, min(dwell, cap)), mod) for mhz, dwell, mod in windows]


def raw_rate(port: str, windows: list[tuple[int, int, int]], bins: int, seconds: float) -> dict[str, object]:
    """独立链路测设备原始吐帧速率（不经调度器、不缓存）。

    注意：这条链路跑完必须关掉再开会话 —— 自扫源会起一个后台读线程，
    两个读者同时读同一个串口会把帧切歪（实测 MemoryError: bins 解成天文数字）。
    """
    if seconds <= 0 or not windows:
        return {}
    link = EspSdrLink(port, 2_000_000)
    link.open()
    frames = 0
    freqs: set[int] = set()
    try:
        link.set_sweep_plan(windows)
        link.start_sweep(bins, 2, 0)
        started = time.time()
        while time.time() - started < seconds:
            frame = link.read_sweep_frame(timeout_s=1.0)
            if frame is None:
                continue
            frames += 1
            freqs.add(int(frame.center_hz) // 1_000_000)
        elapsed = time.time() - started
        link.stop_sweep(3.0)
    finally:
        link.close()
    report = {
        "frames": frames,
        "windows_per_s": round(frames / max(0.1, elapsed), 2),
        "distinct_mhz": len(freqs),
        "seconds": round(elapsed, 1),
    }
    print(f"[raw] {json.dumps(report, ensure_ascii=False)}")
    time.sleep(0.5)  # 让串口彻底放手，再让会话去开
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真板：设备自扫引擎实测")
    parser.add_argument("--port", default="COM56")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--bins", type=int, default=512)
    parser.add_argument("--gain-index", type=int, default=60)
    parser.add_argument("--plan-dwell", type=int, default=20,
                        help="设备计划表里每个窗的驻留 ms（device 引擎的节流阀）")
    parser.add_argument("--engine", default="device", choices=("device", "host"),
                        help="device = 设备自扫；host = 主机逐窗驱动（对照基线）")
    parser.add_argument("--mock", action="store_true",
                        help="假固件（不碰串口）；用来在没有板子时验证统计口径")
    parser.add_argument(
        "--raw-seconds",
        type=float,
        default=3.0,
        help="先用连续读帧测一下设备「原始吐帧速率」（不经调度器），0 则不测",
    )
    args = parser.parse_args(argv)

    raw = (
        raw_rate(args.port, session_windows(args), args.bins, args.raw_seconds)
        if args.engine == "device"
        else {}
    )

    session = HostSession(bins_out=256)
    config = HostConfig.from_json(
        to_json(
            {
                "port": "MOCK" if args.mock else args.port,
                "mock": bool(args.mock),
                "gain_index": args.gain_index,
                "gain_mode": "MANUAL",
                "bins_out": 256,
                "engine": args.engine,
                "plan_dwell_ms": args.plan_dwell,
            }
        )
    )
    info = session.open(config)
    link = session.link
    print(f"[link] {link.identity} caps={len(link.caps)} sweep_supported={info.get('sweep_supported')}")
    if args.engine == "device" and info.get("engine") != "device":
        print(f"[FAIL] 设备没能自扫：{info.get('engine_note')}（先烧带 scan_fsm 的固件）")
        session.close()
        return 2
    print(f"[engine] {info.get('engine')} — {info.get('engine_note')}")
    state = session.sweep_state()
    if session.sweep is not None:
        print(f"[plan] {state.get('windows')} 窗 / bins={session.sweep.bins} / rate={session.sweep.rate_code}")

    seen: list[int] = []

    def on_snapshot(snap) -> None:
        seen.append(int(snap.center_hz))

    started = time.time()
    try:
        while time.time() - started < args.seconds:
            session.step(0.5, on_snapshot=on_snapshot)
    finally:
        state = session.sweep_state()
        stats = session.stats()
        tables = session.tables()
        session.close()

    elapsed = time.time() - started
    rows: dict[str, int] = {}
    for item in (tables or {}).get("plugins") or []:
        table = (item or {}).get("table") or {}
        rows[str((item or {}).get("id"))] = len(table.get("rows") or [])
    # host 引擎没有设备自扫源（session.sweep 为 None），「扫了几窗」只能按喂给模块的快照数算
    frames = state.get("frames")
    frames_src = "device-sweep"
    if frames is None:
        frames = int(stats.get("snapshots") or 0)
        frames_src = "host-snapshots"
    report = {
        "seconds": round(elapsed, 1),
        "engine": state.get("engine"),
        "raw": raw,
        "windows": state.get("windows"),
        "frames": frames,
        "frames_src": frames_src,
        "misses": state.get("misses"),
        "distinct_mhz": len(set(seen)),
        "snapshots": len(seen),
        "frames_per_s": round(int(frames or 0) / max(0.1, elapsed), 2),
        "tables": rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    ok = (
        report["engine"] == args.engine
        and int(report["frames"] or 0) > 0
        and len(seen) > 0
        and sum(rows.values()) > 0
    )
    print("[OK]" if ok else "[FAIL]")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())