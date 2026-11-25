"""阶段 0 全链路演示：注册 → 调度 → 收快照 → 入库 → 产出。

    python -m app.demo

默认用 mock 假快照源（无硬件），跑 3 秒后打印调度配比、入库统计和示例产出。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Sequence

from app import bind_capture, bind_store
from app.capture_link import CaptureAwareSource, iter_capture_plugins, synthetic_capture
from core.registry import Registry, discover
from core.scheduler import Scheduler
from core.store import Store
from output.format import ascii_table, fmt_duration, fmt_pct, kv_block
from tests.mock_modes import ProbePlugin, make_plugin
from tests.mock_source import SCENARIOS, MockSource, SpectrumSynthesizer

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data")


def parse_weights(text: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, value = chunk.partition(":")
        weights[name.strip()] = float(value or 1.0)
    return weights


def build_registry(args: argparse.Namespace) -> tuple[Registry, list[str]]:
    registry = discover("modes")
    notes = list(registry.errors)
    if args.weights:
        for plugin_id, weight in parse_weights(args.weights).items():
            registry.register(make_plugin(plugin_id, weight))
        notes.append(f"按 --weights 注册 {len(registry)} 个演示探针（不是真功能模块）")
    if len(registry) == 0:
        registry.register(ProbePlugin())
        notes.append("modes/ 里还没有功能模块 → 用演示探针跑通链路（阶段 0 的正常状态）")
    return registry, notes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ESP32-C5 射频监测 · 阶段 0 mock 全链路演示")
    parser.add_argument("--scenario", default="mixed", choices=sorted(SCENARIOS), help="mock 场景")
    parser.add_argument("--duration", type=float, default=3.0, help="运行秒数")
    parser.add_argument("--frames-per-scan", type=int, default=2, help="每次驻留产出几帧快照")
    parser.add_argument("--weights", default="", help='演示多模块配比，如 "wifi_check:40,ble_risk:30,guard_58g:20,capture:10"')
    parser.add_argument("--db", default=os.path.join(DEFAULT_DIR, "demo.db"), help="SQLite 路径")
    parser.add_argument("--csv", default=os.path.join(DEFAULT_DIR, "demo_snapshots.csv"), help="CSV 旁路路径")
    parser.add_argument("--quiet", action="store_true", help="不打印产出表格")
    parser.add_argument("--append", action="store_true", help="保留上一次的 demo 库（默认每次清空重跑）")
    args = parser.parse_args(argv)

    started = time.time()
    registry, notes = build_registry(args)
    source = MockSource(
        scenario=args.scenario,
        frames_per_scan=args.frames_per_scan,
        frame_interval_s=0.01,
        realtime=True,
    )
    synth = source.synth
    print(
        kv_block(
            "ESP32-C5 射频监测 · 阶段 0 演示",
            [
                ("跑法", "python -m app.demo " + " ".join(sys.argv[1:])),
                ("数据源", f"mock 假快照（场景 {args.scenario}：{SCENARIOS[args.scenario].description}）"),
                ("已注册模块", ", ".join(f"{p.id}(w={p.slice_weight:g})" for p in registry.plugins)),
                ("时间片配比", " / ".join(f"{p.id}={fmt_pct(p.slice_weight / (registry.total_weight() or 1))}" for p in registry.plugins)),
                ("运行时长", fmt_duration(args.duration)),
                ("噪声底", f"{synth.floor:.0f} 码"),
            ],
        )
    )
    for note in notes:
        print(f"! {note}")

    if not args.append:
        for stale in (args.db, args.csv):
            if stale and os.path.exists(stale):
                os.remove(stale)
    with Store(args.db, csv_path=args.csv) as store:
        # 告警类模块（guard_58g 等）自己声明 store 槽位，由 app 层注入后才会落事件
        bound = bind_store(registry, store)
        if bound:
            print(f"事件落库已绑定：{', '.join(bound)}")
        # 任务 5：抓档排在两次扫描之间；mock 用合成 I/Q（不经串口）
        cap_plugins = iter_capture_plugins(registry.plugins)
        run_source: object = source
        capture_source = None
        if cap_plugins:
            out_dir = os.path.join(DEFAULT_DIR, "capture")
            bound_cap = bind_capture(registry, synthetic_capture, out_dir=out_dir)
            capture_source = CaptureAwareSource(source, cap_plugins, synthetic_capture)
            run_source = capture_source
            if bound_cap:
                print(f"抓档已绑定（合成 I/Q）：{', '.join(bound_cap)} → {out_dir}")
        scheduler = Scheduler(run_source, registry, store=store)  # type: ignore[arg-type]
        stats = scheduler.run(duration_s=args.duration)
        if capture_source is not None:
            capture_source.flush()
        print()
        print(
            ascii_table(
                ["模块", "权重", "时间片", "占比", "快照", "射频秒数", "错误"],
                [
                    [
                        pid,
                        f"{registry.get(pid).slice_weight:g}",
                        per.slices,
                        fmt_pct(per.slices / (stats.slices or 1)),
                        per.snapshots,
                        f"{per.seconds:.2f}",
                        per.errors,
                    ]
                    for pid, per in stats.per_plugin.items()
                ],
                title=f"调度统计（共 {stats.slices} 个时间片 / {stats.snapshots} 帧快照）",
            )
        )
        print(f"入库：{store.count()} 行 → {args.db}")
        for name in bound:
            events = store.events(name)
            if events:
                print(f"事件：{name} {len(events)} 条 → {args.db}")
        if args.csv:
            print(f"CSV 旁路：{args.csv}")
        span = store.time_span()
        if span:
            print(f"时间跨度：{fmt_duration(span[1] - span[0])}，按模块：{store.count_by_module()}")
        if stats.source_errors:
            print(f"! 源错误 {stats.source_errors} 次：{stats.last_source_error}")
        if not args.quiet:
            for plugin in registry.plugins:
                try:
                    output = plugin.produce()
                except NotImplementedError:
                    print(f"\n! {plugin.id} 的 produce() 还没实现（功能分支负责）")
                    continue
                print()
                print(output.render())
    print(f"\n端到端演示完成，用时 {fmt_duration(time.time() - started)}。")
    print("下一步：真机跑一遍同名场景（python -m app.live --port COM56 --duration 60 --gain-index 60）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())