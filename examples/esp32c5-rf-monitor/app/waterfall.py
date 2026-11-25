"""任务 2 演示 CLI：mock 轮扫 → 入库 → 从 SQLite 渲成时频瀑布图。

    python -m app.waterfall --minutes 10 --scenario mixed
    python -m app.waterfall --render-only --db .data/waterfall/mock.db   # 只重渲染

为什么要有「虚拟时间」：mock 源只有真的 sleep 才推进时间戳（``MockSource`` 用
``time.time()``），而瀑布图必须有时间轴。真机走设备时间；这里按「帧序号 × 每帧
虚拟耗时」线性铺开时间轴，``--minutes 10`` 表示 **10 分钟的等效数据**，实际几秒
就跑完。产出物上会写清这一点，不冒充真实连续记录。

数据流与任务书一致：

    scheduler → plugin.on_snapshot()（环形缓冲） + Store.add()（SQLite 顺路入库）
    → output.heatmap.render_waterfall(store.iter_all())
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Iterator, Sequence

from core.registry import Registry, discover
from core.scheduler import Scheduler
from core.snapshot import Snapshot
from core.store import Store
from modes.waterfall import (
    DEFAULT_OUT,
    DWELL_MS,
    PAGE_SECONDS,
    WaterfallMonitor,
    sweep_plan,
    sweep_seconds,
)
from output.heatmap import BANDS, BANNER, ascii_preview, render_waterfall
from output.format import ascii_table
from tests.mock_source import SCENARIOS, MockSource

DEFAULT_DB = os.path.join(DEFAULT_OUT, "mock.db")


class PacedMockSource:
    """给 mock 快照铺一条线性虚拟时间轴（帧序号 × 每帧耗时）。"""

    def __init__(self, inner: MockSource, seconds_per_frame: float, t0: float | None = None) -> None:
        self.inner = inner
        self.dt = max(1e-3, float(seconds_per_frame))
        self.t0 = time.time() if t0 is None else t0
        self.count = 0

    def scan(self, request) -> Iterator[Snapshot]:
        for snap in self.inner.scan(request):
            self.count += 1
            yield Snapshot(
                ts=self.t0 + self.count * self.dt,
                center_hz=snap.center_hz,
                bw_hz=snap.bw_hz,
                bins_db=snap.bins_db,
                meta=dict(snap.meta, virtual_time=True, dt_s=round(self.dt, 4)),
            )


def virtual_plan(minutes: float, frames_per_scan: int) -> tuple[int, float, float]:
    """算出「跑多少帧」才等于 ``minutes`` 分钟的等效轮扫时长。"""
    windows = sum(len(centers) for _band, centers in sweep_plan())
    sweep_s = windows * DWELL_MS / 1000.0
    target_s = max(1.0, minutes * 60.0)
    sweeps = max(1, int(round(target_s / sweep_s)))
    snapshots = sweeps * windows * max(1, frames_per_scan)
    seconds_per_frame = sweep_s / (windows * max(1, frames_per_scan))
    return snapshots, seconds_per_frame, sweeps * sweep_s


def build_registry() -> Registry:
    registry = discover("modes")
    if len(registry) == 0:
        raise SystemExit("modes/ 里没有模块，无法生成瀑布图")
    return registry


def generate(args: argparse.Namespace, registry: Registry) -> tuple[Store, dict[str, float]]:
    db_path = args.db
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass
    store = Store(db_path)
    snapshots, seconds_per_frame, span_s = virtual_plan(args.minutes, args.frames_per_scan)
    source = PacedMockSource(
        MockSource(scenario=args.scenario, realtime=False, frames_per_scan=args.frames_per_scan),
        seconds_per_frame,
    )
    scheduler = Scheduler(source, registry, store=store)
    started = time.time()
    stats = scheduler.run(max_snapshots=snapshots)
    wall = time.time() - started
    info = {
        "snapshots": snapshots,
        "span_s": span_s,
        "seconds_per_frame": seconds_per_frame,
        "wall_s": wall,
        "slices": stats.slices,
        "stored": store.count(),
        "errors": stats.source_errors,
    }
    return store, info


def render(store: Store, args: argparse.Namespace) -> int:
    span = store.time_span() or (0.0, 0.0)
    result = render_waterfall(
        store.iter_between(span[0] - 1e-6, span[1] + 1e-6, module="waterfall"),
        args.out,
        page_seconds=args.page_seconds,
    )
    print()
    print("[figure] 周期频谱瀑布图 · 周期快照，非连续实时")
    if result.pages:
        print(
            ascii_table(
                ["页", "时间范围", "时长 s", "帧数", "色标码"],
                [
                    [
                        page.index,
                        f"{time.strftime('%H:%M:%S', time.localtime(page.t0))}–"
                        f"{time.strftime('%H:%M:%S', time.localtime(page.t1))}",
                        f"{page.duration_s:.0f}",
                        page.frames,
                        f"{page.code_range[0]}–{page.code_range[1]}",
                    ]
                    for page in result.pages
                ],
            )
        )
    print(f"! {BANNER}")
    print("! 色标是相对功率码（0..255），不是 dBm。")
    if result.pages and args.ascii:
        page = result.pages[0]
        for band in BANDS:
            print()
            print(f"[ascii] {band.label} 第 1 页（一行 ≈ {sweep_seconds():.1f} s，压缩到 18×96）")
            print(ascii_preview(page.grids[band.id], lo=page.code_range[0], hi=page.code_range[1]))
    for path in result.files:
        print(f"-> {path}")
    print()
    print(f"用浏览器打开 {result.index_html} 翻页查看（{result.page_count} 页 / {result.frames} 帧）。")
    return 0 if result.pages else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 2：周期性频谱瀑布图（mock 轮扫 → SQLite → PNG/HTML）")
    parser.add_argument("--minutes", type=float, default=10.0, help="等效数据时长（虚拟时间，默认 10 分钟）")
    parser.add_argument("--scenario", default="mixed", choices=sorted(SCENARIOS), help="mock 场景")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 路径")
    parser.add_argument("--out", default=DEFAULT_OUT, help="PNG/HTML 输出目录")
    parser.add_argument("--page-seconds", type=float, default=PAGE_SECONDS, help="每页时长（默认 300 s，一小时 12 页）")
    parser.add_argument("--frames-per-scan", type=int, default=1, help="每个扫描窗产几帧")
    parser.add_argument("--render-only", action="store_true", help="不生成新数据，只从已有 SQLite 重渲染")
    parser.add_argument("--ascii", action="store_true", help="额外在终端打印第 1 页的 ASCII 预演（人眼自检）")
    args = parser.parse_args(argv)

    registry = build_registry()
    print("[info] 模块：" + ", ".join(f"{p.id}({type(p).__name__})" for p in registry.plugins))

    if args.render_only:
        if not os.path.exists(args.db):
            raise SystemExit(f"找不到 {args.db}，先不加 --render-only 跑一次")
        store = Store(args.db)
    else:
        store, info = generate(args, registry)
        print(
            "[info] mock 生成了 {snapshots} 帧 / 等效 {span_s:.0f} s（虚拟时间，实际 {wall_s:.1f} s 跑完），"
            "入库 {stored} 条 / 错误 {errors}".format(**info)
        )
        print(f"[info] 每帧虚拟耗时 {info['seconds_per_frame'] * 1000:.0f} ms")
        for plugin in registry.plugins:
            if isinstance(plugin, WaterfallMonitor):
                summary = plugin.summary()
                print(f"[info] {plugin.id}: 帧 {summary.get('frames')} / 轮次 {summary.get('sweeps')}")

    with store:
        return render(store, args)


if __name__ == "__main__":
    sys.exit(main())