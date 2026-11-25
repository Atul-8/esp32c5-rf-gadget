"""调度器：权重配比、故障隔离、空计划退出。"""
import unittest

from core.registry import ModePlugin, Output, Registry, ScanRequest
from core.scheduler import Scheduler
from core.snapshot import Snapshot
from tests.mock_source import MockSource


def snap(value: int = 40) -> Snapshot:
    return Snapshot(ts=1.0, center_hz=2_442_000_000, bw_hz=80_000_000, bins_db=tuple([value] * 512))


class StaticSource:
    def __init__(self, frames: int = 1, fail: bool = False) -> None:
        self.frames = frames
        self.fail = fail
        self.calls = 0

    def scan(self, request: ScanRequest):
        self.calls += 1
        if self.fail:
            raise RuntimeError("射频炸了")
        for _ in range(self.frames):
            yield snap()


class BoomPlugin(ModePlugin):
    id = "boom"
    name = "会炸的模块"
    slice_weight = 1.0

    def __init__(self) -> None:
        self.requests = [ScanRequest(2_442_000_000, 80_000_000, 100, 512)]

    def scan_requests(self):
        return list(self.requests)

    def on_snapshot(self, snap):
        raise ValueError("故意炸")

    def produce(self):
        return Output("table", "boom")


class PlanePlugin(ModePlugin):
    id = "plane"
    name = "正常模块"
    slice_weight = 1.0

    def __init__(self) -> None:
        self.seen = 0
        self.requests = [ScanRequest(2_442_000_000, 80_000_000, 100, 512)]

    def scan_requests(self):
        return list(self.requests)

    def on_snapshot(self, snap):
        self.seen += 1

    def produce(self):
        return Output("table", "plane")


class WeightedPlugin(PlanePlugin):
    def __init__(self, plugin_id: str, weight: float) -> None:
        super().__init__()
        self.id = plugin_id
        self.name = plugin_id
        self.slice_weight = weight


class EmptyPlugin(ModePlugin):
    id = "empty"
    name = "占位模块"
    slice_weight = 5.0

    def scan_requests(self):
        return []

    def on_snapshot(self, snap):
        raise AssertionError("空模块不该收到快照")

    def produce(self):
        return Output("table", "empty")


class SchedulerTests(unittest.TestCase):
    def test_weight_proportions(self):
        registry = Registry()
        for plugin_id, weight in (("a", 4.0), ("b", 2.0), ("c", 1.0)):
            registry.register(WeightedPlugin(plugin_id, weight))
        scheduler = Scheduler(StaticSource(), registry)
        stats = scheduler.run(max_slices=700)
        self.assertEqual(stats.slices, 700)
        share = stats.share()
        self.assertAlmostEqual(share["a"], 4 / 7, delta=0.02)
        self.assertAlmostEqual(share["b"], 2 / 7, delta=0.02)
        self.assertAlmostEqual(share["c"], 1 / 7, delta=0.02)

    def test_zero_weight_modules_are_skipped_quietly(self):
        registry = Registry()
        registry.register(EmptyPlugin())
        registry.register(WeightedPlugin("real", 1.0))
        scheduler = Scheduler(StaticSource(), registry)
        stats = scheduler.run(max_slices=10)
        self.assertEqual(stats.per_plugin["empty"].slices, 0)
        self.assertEqual(stats.per_plugin["real"].slices, 10)

    def test_broken_plugin_does_not_block_others(self):
        registry = Registry()
        registry.register(BoomPlugin())
        plane = PlanePlugin()
        registry.register(plane)
        scheduler = Scheduler(StaticSource(), registry)
        stats = scheduler.run(max_slices=20)
        self.assertGreater(stats.per_plugin["boom"].errors, 0)
        self.assertEqual(stats.per_plugin["plane"].errors, 0)
        self.assertEqual(plane.seen, stats.per_plugin["plane"].snapshots)
        self.assertGreater(plane.seen, 0)

    def test_source_error_isolated_to_one_slice(self):
        registry = Registry()
        registry.register(PlanePlugin())
        scheduler = Scheduler(StaticSource(fail=True), registry)
        stats = scheduler.run(max_slices=3)
        self.assertEqual(stats.slices, 3)  # 每片都尝试了，没被卡死
        self.assertEqual(stats.source_errors, 3)
        self.assertIn("射频炸了", stats.last_source_error or "")

    def test_no_work_exits(self):
        registry = Registry()
        registry.register(EmptyPlugin())
        scheduler = Scheduler(StaticSource(), registry, sleep=lambda _s: None)
        stats = scheduler.run(max_slices=10)
        self.assertEqual(stats.slices, 0)

    def test_store_and_callback_wired(self):
        class Recorder:
            def __init__(self):
                self.rows = []

            def add(self, snap, module=None):
                self.rows.append((module, snap))
                return len(self.rows)

        store = Recorder()
        seen = []
        registry = Registry()
        registry.register(PlanePlugin())
        scheduler = Scheduler(StaticSource(frames=2), registry, store=store, on_snapshot=seen.append)
        stats = scheduler.run(max_slices=3)
        self.assertEqual(stats.snapshots, 6)
        self.assertEqual(len(store.rows), 6)
        self.assertEqual(len(seen), 6)
        self.assertEqual({m for m, _ in store.rows}, {"plane"})

    def test_mock_source_end_to_end(self):
        registry = Registry()
        registry.register(PlanePlugin())
        source = MockSource(scenario="wifi_busy", realtime=False, frames_per_scan=2)
        scheduler = Scheduler(source, registry)
        stats = scheduler.run(max_slices=5)
        self.assertEqual(stats.snapshots, 10)
        self.assertEqual(stats.per_plugin["plane"].errors, 0)


if __name__ == "__main__":
    unittest.main()