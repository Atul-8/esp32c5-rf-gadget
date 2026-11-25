"""``modes/guard_58g.py`` 的自测（任务 4 验收：告警触发 + 零误报）。

跑法（仓库根 ``examples/esp32c5-rf-monitor`` 下）：``python -m unittest tests.test_guard_58g -v``

两类用例：

* **纯函数**：``window_centers`` / ``runs_of`` / ``correlation`` / ``kind_of`` / ``cluster_by_freq``；
* **端到端（假快照源）**：``video_58g`` 必须报警（且字段完整、跨窗去重成一条），
  ``quiet`` 必须一条都不报，``mixed`` 报 5.8G 那条。
"""
from __future__ import annotations

import json
import unittest

from core.registry import ScanRequest
from core.snapshot import Snapshot
from modes._legacy.guard_58g import (
    ALERT_DB,
    ALT_CORR,
    BAND_HI_HZ,
    BAND_LO_HZ,
    BINS,
    CENTER_NOTCH_HZ,
    DWELL_MS,
    EDGE_GUARD_HZ,
    HOP_FILL,
    KIND_HOP,
    KIND_SPUR_BURST,
    KIND_SPUR_STEADY,
    KIND_STEADY_NARROW,
    KIND_STEADY_WIDE,
    MATCH_HZ,
    MAX_ALERT_HZ,
    MIN_FRAMES,
    MIN_NARROW_HZ,
    OCC_EXCESS_DB,
    SCAN_BW_HZ,
    SCAN_STEP_HZ,
    SPUR_MAX_HZ,
    STALE_S,
    STEADY_FILL,
    Guard58G,
    WindowStat,
    cluster_by_freq,
    correlation,
    kind_of,
    masked_bins,
    percentile,
    runs_of,
    window_centers,
)
from tests.mock_source import make_source


# ---------------------------------------------------------------- 纯函数
class HelperTest(unittest.TestCase):
    def test_window_centers_covers_edges(self):
        centers = window_centers()
        self.assertGreaterEqual(len(centers), 10)
        self.assertLessEqual(centers[0] - SCAN_BW_HZ // 2, BAND_LO_HZ)  # 首窗压左沿
        self.assertGreaterEqual(centers[-1] + SCAN_BW_HZ // 2, BAND_HI_HZ)  # 末窗压右沿
        self.assertEqual(list(centers), sorted(centers))

    def test_window_steps_never_exceed_scan_step(self):
        # 均分出来的步进必须 ≤ SCAN_STEP_HZ，否则会留下 >15 MHz 的空档
        centers = window_centers()
        for a, b in zip(centers, centers[1:]):
            self.assertLessEqual(b - a, SCAN_STEP_HZ)
            self.assertGreater(b, a)

    def test_window_centers_single_when_band_narrower_than_window(self):
        centers = window_centers(5_700_000_000, 5_705_000_000)
        self.assertEqual(len(centers), 1)

    def test_runs_of_basic(self):
        self.assertEqual(runs_of([False, True, True, False, True]), [(1, 2), (4, 4)])
        self.assertEqual(runs_of([True, True]), [(0, 1)])
        self.assertEqual(runs_of([False, False]), [])

    def test_correlation_perfect(self):
        self.assertAlmostEqual(correlation([1, 0, 1, 0], [0, 1, 0, 1]), -1.0, places=6)
        self.assertAlmostEqual(correlation([1, 0, 1, 0], [1, 0, 1, 0]), 1.0, places=6)

    def test_correlation_flat_is_zero(self):
        # 一侧恒定 → 无法判断互斥，不许硬判成跳频
        self.assertEqual(correlation([1, 1, 1, 1], [0, 1, 0, 1]), 0.0)
        self.assertEqual(correlation([1, 0], [0, 1]), 0.0)  # 样本太少

    def test_kind_of(self):
        self.assertEqual(kind_of(1e6, 0.95), KIND_STEADY_NARROW)
        self.assertEqual(kind_of(1e6, 0.40), KIND_HOP)
        self.assertEqual(kind_of(10e6, STEADY_FILL), KIND_STEADY_WIDE)

    def test_kind_of_spur_is_separate(self):
        # 0.1 MHz 级 = 本振/时钟杂散，不能和图传（「窄带」）混成一类
        self.assertEqual(kind_of(120_000, 0.95), KIND_SPUR_STEADY)
        self.assertEqual(kind_of(120_000, 0.30), KIND_SPUR_BURST)
        self.assertEqual(kind_of(SPUR_MAX_HZ, 0.9), KIND_SPUR_STEADY)
        self.assertEqual(kind_of(SPUR_MAX_HZ + 1, 0.9), KIND_STEADY_NARROW)

    def test_percentile_matches_known_values(self):
        self.assertAlmostEqual(percentile([0, 10], 50), 5.0)
        self.assertEqual(percentile([], 50), 0.0)
        self.assertEqual(percentile([5], 90), 5.0)
        self.assertAlmostEqual(percentile([0, 10, 20], 10), 2.0)

    def test_cluster_by_freq_merges_overlaps(self):
        cands = [{"center_hz": int(5730e6)}, {"center_hz": int(5735e6)}, {"center_hz": int(5800e6)}]
        groups = cluster_by_freq(cands, match_hz=MATCH_HZ)
        self.assertEqual([len(g) for g in groups], [2, 1])

    def test_cluster_by_freq_empty(self):
        self.assertEqual(cluster_by_freq([]), [])


# ---------------------------------------------------------------- 造快照
#: 20 MHz / 512 bin ≈ 39.06 kHz/格 —— 造数据时必须按这个宽度换算，
#: 别用「1 格 = 1 MHz」的错觉（第一版测试就是这么错的）
BIN_HZ = SCAN_BW_HZ / BINS


def bins_for(width_hz: float) -> int:
    """多宽的信号占多少格。"""
    return max(1, int(round(width_hz / BIN_HZ)))


def synth_frame(center_hz: int, bins: int, peaks: dict[int, int], floor: int = 20) -> tuple[int, ...]:
    """``peaks`` = {bin 下标: 抬升码}；其余 bin 就是底噪。"""
    out = [floor] * bins
    for index, level in peaks.items():
        out[index] = floor + level
    return tuple(out)


def feed(guard: Guard58G, frames_per_window: int, ts0: float = 1000.0, dt: float = 0.1,
         spec=None) -> float:
    """喂一轮完整轮扫（每个窗 ``frames_per_window`` 帧），返回下一个可用时间戳。"""
    ts = ts0
    for request in guard.scan_requests():
        for _ in range(frames_per_window):
            bins = spec(request) if spec else (20,) * request.bins
            snap = Snapshot(
                ts=ts,
                center_hz=request.freq_hz,
                bw_hz=request.bw_hz,
                bins_db=bins,
                meta={"source": "test"},
            )
            guard.on_snapshot(snap)
            ts += dt
    return ts


def feed_mock(guard: Guard58G, scenario: str, rounds: int = 3, frames_per_scan: int = 4,
              ts0: float = 1000.0) -> None:
    source = make_source(scenario, frames_per_scan=frames_per_scan)
    ts = ts0
    for _ in range(rounds):
        for request in guard.scan_requests():
            for snap in source.scan(request):
                guard.on_snapshot(
                    Snapshot(
                        ts=ts,
                        center_hz=snap.center_hz,
                        bw_hz=snap.bw_hz,
                        bins_db=snap.bins_db,
                        meta=snap.meta,
                    )
                )
                ts += 0.02


# ---------------------------------------------------------------- 契约
class ContractTest(unittest.TestCase):
    def test_plugin_identity(self):
        guard = Guard58G()
        self.assertEqual(guard.id, "guard_58g")
        self.assertEqual(guard.slice_weight, 1.0)  # 单射频公平（META-0087）

    def test_scan_requests_within_band(self):
        requests = Guard58G().scan_requests()
        self.assertEqual(len(requests), len(window_centers()))
        for request in requests:
            self.assertIsInstance(request, ScanRequest)
            self.assertEqual(request.bw_hz, SCAN_BW_HZ)
            self.assertEqual(request.dwell_ms, DWELL_MS)
            self.assertEqual(request.bins, BINS)
            self.assertGreaterEqual(request.freq_hz - SCAN_BW_HZ // 2, BAND_LO_HZ - 1)
            self.assertLessEqual(request.freq_hz + SCAN_BW_HZ // 2, BAND_HI_HZ + 1)

    def test_registered_by_discover(self):
        from core.registry import discover

        registry = discover()
        self.assertIn("guard_58g", [plugin.id for plugin in registry.plugins])
        self.assertEqual(registry.errors, [])


# ---------------------------------------------------------------- 检测
class CandidateTest(unittest.TestCase):
    def _stat(self, frames):
        stat = WindowStat(center_hz=5_740_000_000)
        for index, frame in enumerate(frames):
            stat.add(frame, ts=1000.0 + index * 0.1)
        return stat

    def test_spur_is_reported_as_spur(self):
        # 3 格（≈117 kHz）的持续尖峰：要报，但类别是「单频点」，不是「窄带」
        bins = 512
        peaks = {i: 40 for i in range(100, 103)}
        frames = [synth_frame(0, bins, peaks) for _ in range(MIN_FRAMES + 2)]
        cands = Guard58G().candidates(self._stat(frames))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["kind"], KIND_SPUR_STEADY)

    def test_narrow_steady_candidate(self):
        bins = 512
        peaks = {i: 40 for i in range(100, 100 + bins_for(1_200_000))}  # 1.2 MHz 才叫「窄带」
        frames = [synth_frame(0, bins, peaks) for _ in range(MIN_FRAMES + 2)]
        cands = Guard58G().candidates(self._stat(frames))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["kind"], KIND_STEADY_NARROW)
        self.assertAlmostEqual(cands[0]["fill"], 1.0, places=6)
        self.assertGreaterEqual(cands[0]["peak_excess_db"], ALERT_DB)

    def test_wide_steady_is_labelled_wide(self):
        bins = 512
        # 8.2 MHz 宽且整段落在窗中心左侧（跨中心会被直流缺口切成两段）
        peaks = {i: 40 for i in range(30, 30 + bins_for(8_200_000))}
        frames = [synth_frame(0, bins, peaks) for _ in range(MIN_FRAMES + 2)]
        cands = Guard58G().candidates(self._stat(frames))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["kind"], KIND_STEADY_WIDE)
        self.assertLessEqual(cands[0]["bw_hz"], MAX_ALERT_HZ)

    def test_too_few_frames_no_candidate(self):
        bins = 512
        peaks = {i: 40 for i in range(100, 104)}
        frames = [synth_frame(0, bins, peaks) for _ in range(MIN_FRAMES - 1)]
        self.assertEqual(Guard58G().candidates(self._stat(frames)), [])

    def test_noise_only_no_candidate(self):
        bins = 512
        frames = [synth_frame(0, bins, {}) for _ in range(MIN_FRAMES + 4)]
        self.assertEqual(Guard58G().candidates(self._stat(frames)), [])

    def test_weak_signal_below_threshold_ignored(self):
        bins = 512
        peaks = {i: ALERT_DB - 2.0 for i in range(100, 104)}  # 抬升不够
        frames = [synth_frame(0, bins, peaks) for _ in range(MIN_FRAMES + 2)]
        self.assertEqual(Guard58G().candidates(self._stat(frames)), [])

    def test_too_wide_segment_ignored(self):
        bins = 512
        # ≈18 MHz > MAX_ALERT_HZ(16 MHz)：不归本模块（那是 WiFi 那一档的事）
        peaks = {i: 40 for i in range(0, bins_for(18_000_000))}
        frames = [synth_frame(0, bins, peaks) for _ in range(MIN_FRAMES + 2)]
        self.assertEqual(Guard58G().candidates(self._stat(frames)), [])

    def test_alternating_pair_is_hop(self):
        bins = 512
        frames = []
        block_a = {i: 60 for i in range(100, 106)}
        block_b = {i: 60 for i in range(160, 166)}
        for index in range(12):
            frames.append(synth_frame(0, bins, block_a if index % 2 == 0 else block_b))
        cands = Guard58G().candidates(self._stat(frames))
        self.assertEqual(len(cands), 2)
        for cand in cands:
            self.assertEqual(cand["kind"], KIND_HOP)
            self.assertIn("alternate", cand)
        a, b = cands
        self.assertLessEqual(HOP_FILL, a["fill"])
        self.assertLess(a["fill"], STEADY_FILL)

    def test_center_spike_ignored(self):
        # 实板每个窗正中心都有一根 3 格宽 +33~43 码的尖峰（接收机直流泄漏）→ 不许报警
        bins = 512
        peaks = {bins // 2 - 1: 60, bins // 2: 60, bins // 2 + 1: 60}
        frames = [synth_frame(0, bins, peaks) for _ in range(MIN_FRAMES + 2)]
        self.assertEqual(Guard58G().candidates(self._stat(frames)), [])

    def test_edge_rolloff_ignored(self):
        bins = 512
        peaks = {b: 45 for b in range(bins - 4, bins)}  # 窗最外 4 格（滚降区）
        frames = [synth_frame(0, bins, peaks) for _ in range(MIN_FRAMES + 2)]
        self.assertEqual(Guard58G().candidates(self._stat(frames)), [])

    def test_near_edge_still_detected(self):
        bins = 512
        peaks = {b: 40 for b in range(bins - 32, bins - 28)}  # 距窗沿约 1.1 MHz：能判
        frames = [synth_frame(0, bins, peaks) for _ in range(MIN_FRAMES + 2)]
        cands = Guard58G().candidates(self._stat(frames))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["last_bin"], bins - 29)

    def test_masked_bins_shape(self):
        bins = 512
        keep = masked_bins(bins)
        self.assertEqual(len(keep), bins)
        bin_hz = SCAN_BW_HZ / bins
        center = bins // 2
        self.assertFalse(keep[center])  # 中心尖峰挖掉
        self.assertFalse(keep[0])  # 窗沿滚降挖掉
        self.assertFalse(keep[-1])
        notch_bins = int(CENTER_NOTCH_HZ // bin_hz)  # 250 kHz / 39 kHz ≈ 6 格
        self.assertFalse(keep[center + notch_bins - 1])
        self.assertTrue(keep[center + notch_bins + 2])  # 缺口外重新可判
        # 挖掉之后仍要剩 80% 以上的格可判，否则等于没扫
        self.assertGreater(sum(keep) / bins, 0.8)
        self.assertGreater(EDGE_GUARD_HZ, 0)


# ---------------------------------------------------------------- 端到端
class ScenarioTest(unittest.TestCase):
    def test_video_58g_triggers_and_fields_complete(self):
        guard = Guard58G()
        feed_mock(guard, "video_58g", rounds=3)
        guard.analyze(now=2000.0)
        alerts = guard.alerts
        self.assertGreaterEqual(len(alerts), 1, "video_58g 场景必须有告警")
        alert = alerts[0]
        # 注入的是 5740 MHz 持续信号：频点要落在 5735~5745
        self.assertLess(abs(alert.center_hz / 1e6 - 5740.0), 5.0)
        self.assertEqual(alert.kind, KIND_STEADY_NARROW)
        self.assertGreaterEqual(alert.fill, STEADY_FILL)
        self.assertGreater(alert.bw_hz, MIN_NARROW_HZ)
        self.assertLessEqual(alert.bw_hz, MAX_ALERT_HZ)
        # 日志字段必须完整（任务书验收项）
        for key in ("id", "kind", "center_hz", "bw_mhz", "peak_excess_db", "fill",
                    "first_seen", "last_seen", "duration_s", "frames", "active"):
            self.assertIn(key, alert.as_dict())
        self.assertRegex(alert.alert_id, r"^G58-\d{3}$")

    def test_overlapping_windows_are_deduped(self):
        # 重叠窗会在相邻窗里重复看到同一个源：聚类后必须是 1 条，不是 3 条
        guard = Guard58G()
        feed_mock(guard, "video_58g", rounds=3)
        guard.analyze(now=2000.0)
        near_5740 = [a for a in guard.alerts if abs(a.center_hz / 1e6 - 5740) < 12]
        self.assertEqual(len(near_5740), 1)

    def test_quiet_has_zero_alerts(self):
        guard = Guard58G()
        feed_mock(guard, "quiet", rounds=3)
        guard.analyze(now=2000.0)
        self.assertEqual(guard.alerts, [])
        self.assertEqual(guard.active_alerts(), [])

    def test_mixed_reports_58g_source_only(self):
        guard = Guard58G()
        feed_mock(guard, "mixed", rounds=3)
        guard.analyze(now=2000.0)
        self.assertGreaterEqual(len(guard.alerts), 1)
        for alert in guard.alerts:
            self.assertGreater(alert.center_hz, BAND_LO_HZ)
            self.assertLess(alert.center_hz, BAND_HI_HZ)

    def test_degenerate_frames_ignored(self):
        guard = Guard58G()
        ts = 1000.0
        for request in guard.scan_requests():
            for _ in range(MIN_FRAMES + 4):
                snap = Snapshot(
                    ts=ts, center_hz=request.freq_hz, bw_hz=request.bw_hz,
                    bins_db=(0,) * request.bins, meta={},
                )
                guard.on_snapshot(snap)
                ts += 0.1
        guard.analyze(now=2000.0)
        self.assertEqual(guard.alerts, [])
        self.assertEqual(guard.dropped_frames, guard.frames)  # 全被丢弃
        self.assertGreater(guard.frames, 0)

    def test_alert_duration_grows_across_rounds(self):
        guard = Guard58G()
        spec = None  # 用 mock 的 video 场景
        feed_mock(guard, "video_58g", rounds=3)
        first = guard.analyze(now=3000.0)[0]
        first_id, first_hits = first.alert_id, first.hits  # Alert 是可变对象，先取快照
        feed_mock(guard, "video_58g", rounds=1, ts0=guard.last_ts + 20.0)
        again = guard.analyze(now=3010.0)[0]
        self.assertEqual(again.alert_id, first_id)  # 同一条源 → 续时长，不新开日志
        self.assertGreaterEqual(again.duration_s, 10.0)
        self.assertGreater(again.hits, first_hits)

    def test_stale_alert_marked_inactive(self):
        # 源停了以后环形缓冲里还留着旧帧：存活判定必须按「最新帧时间」，不能按分析次数
        guard = Guard58G()
        feed_mock(guard, "video_58g", rounds=3)
        alert = guard.alerts[0]
        self.assertTrue(alert.active)
        live_ts = guard.last_ts
        # 接着喂安静的帧，把最新帧时间推过 STALE_S
        def quiet_spec(request):
            return tuple(20 for _ in range(request.bins))

        feed(guard, 12, ts0=live_ts + STALE_S + 5, dt=0.1, spec=quiet_spec)
        guard.analyze()
        self.assertFalse(alert.active)
        self.assertIn(alert, guard.alerts)  # 日志保留，只标「已停」
        self.assertEqual(guard.active_alerts(), [])

    def test_second_source_gets_new_id(self):
        guard = Guard58G()
        feed_mock(guard, "video_58g", rounds=3)
        guard.analyze(now=3000.0)
        first_ids = [alert.alert_id for alert in guard.alerts]
        # 换一个不相邻的源（5810 MHz）
        def spec(request):
            if abs(request.freq_hz - 5_810_000_000) < 8e6:
                return synth_frame(0, request.bins, {i: 60 for i in range(200, 206)})
            return tuple(20 for _ in range(request.bins))
        feed(guard, MIN_FRAMES + 2, ts0=4000.0, spec=spec)
        guard.analyze(now=4001.0)
        new_ids = [alert.alert_id for alert in guard.alerts]
        self.assertGreater(len(new_ids), len(first_ids))
        self.assertEqual(set(first_ids) - set(new_ids), set())


# ---------------------------------------------------------------- 产出
class OutputTest(unittest.TestCase):
    def _guard(self):
        guard = Guard58G()
        feed_mock(guard, "video_58g", rounds=3)
        return guard

    def test_produce_table_and_notes(self):
        output = self._guard().produce()
        self.assertEqual(output.kind, "alert")
        self.assertIn("5.8G 频段守卫", output.title)
        self.assertEqual(len(output.columns), 9)
        self.assertEqual(len(output.rows), 1)
        self.assertGreaterEqual(len(output.notes), 5)
        rendered = output.render()
        self.assertIn("周期快照，非连续实时", rendered)
        self.assertIn("不是 dBm", rendered)

    def test_produce_empty_guard_still_renders(self):
        output = Guard58G().produce()
        self.assertEqual(output.rows, [])
        self.assertIn("告警 0 条", output.title)

    def test_web_payload_is_json_and_has_thresholds(self):
        payload = self._guard().web_payload()
        text = json.dumps(payload, ensure_ascii=False)  # 必须可 JSON 化（走 QWebChannel）
        self.assertIn("guard_58g", text)
        self.assertEqual(payload["kind"], "guard_58g")
        self.assertEqual(payload["band"]["lo_hz"], BAND_LO_HZ)
        self.assertEqual(payload["scan"]["windows"], len(window_centers()))
        self.assertEqual(payload["thresholds"]["alert_db"], ALERT_DB)
        self.assertEqual(payload["thresholds"]["occ_excess_db"], OCC_EXCESS_DB)
        self.assertEqual(payload["thresholds"]["alt_corr"], ALT_CORR)
        self.assertGreaterEqual(len(payload["notes"]), 5)
        self.assertEqual(len(payload["alerts"]), 1)

    def test_reset_clears_everything(self):
        guard = self._guard()
        guard.reset()
        self.assertEqual(guard.alerts, [])
        self.assertEqual(guard.windows, {})
        self.assertEqual(guard.frames, 0)
        self.assertEqual(guard.analyses, 0)
        self.assertEqual(guard.produce().rows, [])

    def test_notes_mention_gain_dependency(self):
        text = "\n".join(self._guard().notes())
        self.assertIn("增益", text)
        self.assertIn("5895", text)  # 频段上限必须写出来


# ---------------------------------------------------------------- 落库
class StoreTest(unittest.TestCase):
    class FakeStore:
        def __init__(self):
            self.events = []

        def add_event(self, module, kind, payload, ts=None):
            self.events.append((module, kind, dict(payload)))

    def test_new_alert_written_to_store_once(self):
        guard = Guard58G()
        store = self.FakeStore()
        guard.store = store
        feed_mock(guard, "video_58g", rounds=3)
        guard.analyze(now=3000.0)
        self.assertEqual(len(store.events), 1)  # 同一条源续时长不重复落库
        module, kind, payload = store.events[0]
        self.assertEqual(module, "guard_58g")
        self.assertEqual(kind, "alert")
        for key in ("id", "kind", "center_hz", "bw_mhz", "duration_s"):
            self.assertIn(key, payload)
        guard.analyze(now=3100.0)
        self.assertEqual(len(store.events), 1)  # 第二轮不重复写

    def test_store_failure_does_not_raise(self):
        class Boom:
            def add_event(self, *args, **kwargs):
                raise RuntimeError("db locked")

        guard = Guard58G()
        guard.store = Boom()
        feed_mock(guard, "video_58g", rounds=3)
        guard.analyze(now=3000.0)  # 不许抛
        self.assertEqual(len(guard.alerts), 1)


if __name__ == "__main__":
    unittest.main()