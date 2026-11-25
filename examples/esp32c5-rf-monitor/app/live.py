"""真机（或假固件）前台：连上 esp-sdr，按注册模块的时间片跑一轮。

    python -m app.live --port COM5 --duration 30       # 真机 ESP32-C5
    python -m app.live --mock --duration 5            # 无硬件自测同一条代码路径

阶段 0 只有地基层，``modes/`` 为空时会退化成单个「链路探针」：
它的作用是证明串口协议、调度、入库在真机上跑得通。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Sequence

from app import bind_capture, bind_store
from app.capture_link import CaptureAwareSource, iter_capture_plugins
from core.registry import Registry, discover
from core.scheduler import Scheduler
from core.serial_link import EspSdrLink, SerialLinkError
from core.store import Store
from output.format import ascii_table, fmt_duration, fmt_hz_short, fmt_pct, kv_block
from tests.fake_firmware import FakeFirmware
from tests.mock_modes import ProbePlugin
from tests.mock_source import SCENARIOS

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data")


def build_registry() -> tuple[Registry, list[str]]:
    registry = discover("modes")
    notes = list(registry.errors)
    if len(registry) == 0:
        registry.register(ProbePlugin(name="链路探针", slice_weight=1.0))
        notes.append("modes/ 还没有功能模块 → 只用「链路探针」验证真机链路（阶段 0 正常状态）")
    return registry, notes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="连接 esp-sdr 固件跑一轮调度")
    parser.add_argument("--port", default="MOCK", help="串口，如 COM5 / /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=2_000_000)
    parser.add_argument("--duration", type=float, default=20.0, help="运行秒数")
    parser.add_argument("--mock", action="store_true", help="用假固件（无硬件自测）")
    parser.add_argument("--scenario", default="mixed", choices=sorted(SCENARIOS), help="--mock 时的场景")
    parser.add_argument("--db", default=os.path.join(DEFAULT_DIR, "live.db"), help="SQLite 路径")
    parser.add_argument("--csv", default="", help="CSV 旁路路径（空=不写）")
    parser.add_argument("--no-store", action="store_true", help="不入库，只打印")
    parser.add_argument(
        "--gain-index",
        type=int,
        default=None,
        help="固定前端增益档位（PHY 表索引，非 dB）。不同信道要横向比较就必须固定增益，"
        "否则硬件 AGC 会把每个信道拉到相近电平，占用判断失去意义；不给则用硬件 AGC。",
    )
    args = parser.parse_args(argv)

    registry, notes = build_registry()
    factory = (lambda port, baud, timeout=5.0: FakeFirmware(port, baud, timeout, scenario=args.scenario)) if args.mock else None

    link = EspSdrLink(args.port, args.baud, factory=factory)
    store = None if args.no_store else Store(args.db, csv_path=args.csv or None)
    try:
        link.open()
        if args.gain_index is not None and not args.mock:
            link.set_gain("MANUAL", args.gain_index)
        profiles = link.profiles()
        print(
            kv_block(
                "esp-sdr 连接",
                [
                    ("端口", f"{args.port} @ {args.baud} baud" + ("（假固件）" if args.mock else "")),
                    ("固件", link.identity),
                    ("传输", link.transport or "未知"),
                    ("能力", " ".join(link.caps) or "（未通告）"),
                    ("剖面", ", ".join(f"{fs/1e6:.0f}MS/s/{bins}bin" for fs, _c, bins, _s, _u, _cont in profiles) or "（无）"),
                    ("增益", "硬件 AGC" if args.gain_index is None or args.mock else f"固定档位 {args.gain_index}"),
                    ("模块", ", ".join(f"{p.id}(w={p.slice_weight:g})" for p in registry.plugins)),
                ],
            )
        )
        for note in notes:
            print(f"! {note}")
        bound = bind_store(registry, store)
        if bound:
            print(f"事件落库已绑定：{', '.join(bound)}")
        # 任务 5：抓档只能在两次扫描之间做 → 包一层 Source，扫描结束才清队列
        cap_plugins = iter_capture_plugins(registry.plugins)
        source: object = link
        capture_source = None
        if cap_plugins:
            bound_cap = bind_capture(
                registry,
                lambda freq, samples, rate, bits: link.capture_iq(samples, rate, bits, freq),
                out_dir=os.path.join(DEFAULT_DIR, "capture"),
            )
            capture_source = CaptureAwareSource(
                link, cap_plugins, lambda freq, samples, rate, bits: link.capture_iq(samples, rate, bits, freq)
            )
            source = capture_source
            if bound_cap:
                print(f"抓档已绑定：{', '.join(bound_cap)} → {os.path.join(DEFAULT_DIR, 'capture')}")
        scheduler = Scheduler(source, registry, store=store)  # type: ignore[arg-type]
        stats = scheduler.run(duration_s=args.duration)
        if capture_source is not None:
            capture_source.flush()  # 收尾再清一次，别把最后一轮排队的抓档丢了
        print()
        print(
            ascii_table(
                ["模块", "时间片", "占比", "快照", "射频秒数", "错误"],
                [
                    [pid, per.slices, fmt_pct(per.slices / (stats.slices or 1)), per.snapshots, f"{per.seconds:.2f}", per.errors]
                    for pid, per in stats.per_plugin.items()
                ],
                title=f"调度统计（{stats.slices} 片 / {stats.snapshots} 帧 / {fmt_duration(stats.elapsed_s)}）",
            )
        )
        last = link.describe()["last_scan"]
        if last:
            print(
                "最后一次扫描："
                f"{fmt_hz_short(last.get('freq_hz', 0))} / {last.get('sample_rate_hz', 0)/1e6:.0f}MS/s / "
                f"{last.get('bins', 0)}bin / {last.get('dwell_ms', 0)}ms / 状态 {last.get('status', '?')}"
            )
        if store is not None:
            print(f"入库：{store.count()} 行 → {args.db}")
            for name in bound:
                events = store.events(name)
                if events:
                    print(f"事件：{name} {len(events)} 条 → {args.db}")
        if stats.source_errors:
            print(f"! 源错误 {stats.source_errors} 次：{stats.last_source_error}")
        for plugin in registry.plugins:
            try:
                rendered = plugin.produce().render()
            except Exception as exc:  # noqa: BLE001 - 产出失败不该掩盖调度结果
                print(f"! {plugin.id} 产出失败：{type(exc).__name__}: {exc}")
                continue
            print()
            print(rendered)
    except SerialLinkError as exc:
        print(f"!! 串口/协议错误：{exc}", file=sys.stderr)
        return 2
    finally:
        link.close()
        if store is not None:
            store.close()
    print(f"\n跑完：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())