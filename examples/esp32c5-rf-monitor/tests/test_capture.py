"""``modes/capture.py`` + ``app/capture_link.py`` 的自测（任务 5 验收）。

跑法（仓库根 ``examples/esp32c5-rf-monitor`` 下）：``python -m unittest tests.test_capture -v``

四组用例：

* **纯函数**：``window_centers`` / ``percentile`` / ``runs_of`` / ``masked_bins`` / ``kind_of``；
* **候选判定**：造平顶凸起必须被判定；噪声底必须一条都不判；直流缺口与窗沿滚降不许误判；
* **抓档链路**：排队 → 队列被取走 → 合成 I/Q 落盘（文件 + sidecar + sha256）→ 事件入库；
* **边界**：预算上限、同频点重复抑制、单次抓档失败不许拖垮采集。
"""
from __future__ import annotations

import json
import os
import time
import shutil
import tempfile
import unittest

from app.capture_link import CaptureAwareSource, FakeCapture, iter_capture_plugins, synthetic_capture
from core.registry import ScanRequest
from core.snapshot import Snapshot
from core.store import Store
from modes._legacy.capture import (
    ALERT_DB,
    BAND_HI_HZ,
    BAND_LO_HZ,
    BINS,
    CAPTURE_BITS,
    CAPTURE_RATE_CODE,
    CENTER_NOTCH_HZ,
    CONTEXT_BINS,
    CONTEXT_S,
    EDGE_GUARD_HZ,
    FIRMWARE_MAX_SAMPLES,
    FLOOR_PCT,
    KIND_AMBIGUOUS,
    KIND_BURST,
    KIND_CW,
    KIND_STEADY,
    MATCH_HZ,
    MAX_CAPTURES,
    MIN_FRAMES,
    RETRIGGER_S,
    SPUR_MAX_HZ,
    SCAN_BW_HZ,
    Candidate,
    CaptureMode,
    CaptureRequest,
    WindowStat,
    kind_of,
    masked_bins,
    notch_bins,
    percentile,
    runs_of,
    runs_of_state,
    window_centers,
)

BIN_HZ = SCAN_BW_HZ / BINS  # ≈ 39.06 kHz，别用「一格 1 MHz」的错觉造数据


def flat(bins: int = BINS, level: float = 40.0) -> list[float]:
    return [level] * bins


def add_run(frame: list[float], center_hz: float, width_hz: float, gain: float, window_hz: float) -> list[float]:
    """在 ``frame`` 上以 ``window_hz`` 为窗中心，抬一段 ``width_hz`` 宽、``gain`` 高。"""
    out = list(frame)
    half = SCAN_BW_HZ / 2.0
    for index in range(len(out)):
        offset = -half + (index + 0.5) * BIN_HZ
        if abs(offset - (center_hz - window_hz)) <= width_hz / 2.0:
            out[index] += gain
    return out


def feed(plugin: CaptureMode, window_hz: float, frames: list[list[float]], t0: float = 1000.0, step: float = 0.25):
    """帧间隔 0.25 s：插件按 FRAME_MIN_GAP_S(0.2) 抽帧，间隔太小会被整批丢掉。"""
    """把一串帧喂给插件（每喂一帧都调 on_snapshot，节流由插件自己管）。"""
    for index, bins in enumerate(frames):
        plugin.on_snapshot(
            Snapshot(
                ts=t0 + index * step,
                center_hz=window_hz,
                bw_hz=SCAN_BW_HZ,
                bins_db=tuple(bins),
            )
        )


def steady(window_hz: float, center_hz: float, width_hz: float, gain: float, count: int = 16) -> list[list[float]]:
    """默认 16 帧 × 0.25 s = 3.75 s 跨度（插件要求环形缓冲覆盖 ≥MIN_SPAN_S 才判）。"""
    return [add_run(flat(), center_hz, width_hz, gain, window_hz) for _ in range(count)]


# ---------------------------------------------------------------- 纯函数
class HelperTest(unittest.TestCase):
    def test_window_centers_covers_band(self):
        centers = window_centers()
        self.assertGreaterEqual(len(centers), 5)
        self.assertAlmostEqual(centers[0] - SCAN_BW_HZ / 2, BAND_LO_HZ, delta=1.0)
        self.assertAlmostEqual(centers[-1] + SCAN_BW_HZ / 2, BAND_HI_HZ, delta=1.0)
        steps = [b - a for a, b in zip(centers, centers[1:])]
        for step in steps:
            self.assertLessEqual(step, SCAN_BW_HZ)

    def test_window_centers_single(self):
        self.assertEqual(len(window_centers(2_400_000_000, 2_410_000_000)), 1)

    def test_percentile(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(percentile(values, 0.0), 1.0)
        self.assertAlmostEqual(percentile(values, 100.0), 4.0)
        self.assertAlmostEqual(percentile(values, 50.0), 2.5)
        self.assertEqual(percentile([], 50.0), 0.0)
        self.assertEqual(percentile([7.0], 90.0), 7.0)

    def test_runs_of(self):
        self.assertEqual(runs_of([False, True, True, False, True]), [(1, 2), (4, 4)])
        self.assertEqual(runs_of([True, True]), [(0, 1)])
        self.assertEqual(runs_of([False, False]), [])

    def test_masked_bins_notch_and_edges(self):
        keep = masked_bins(BINS)
        self.assertEqual(len(keep), BINS)
        center = BINS // 2
        self.assertFalse(keep[center])  # 窗中心一定不判（直流/LO 泄漏）
        self.assertFalse(keep[0])  # 左侧滚降不判
        self.assertFalse(keep[-1])  # 右侧滚降不判
        kept = sum(1 for flag in keep if flag)
        # 挖掉的不能超过 20%（否则等于把窗废掉）
        self.assertGreater(kept / BINS, 0.80)
        # ±CENTER_NOTCH_HZ 内确实被挖
        notch_bins = CENTER_NOTCH_HZ / BIN_HZ
        self.assertFalse(keep[center - int(notch_bins) + 1])

    def test_notch_bins_only_centre(self):
        notch = notch_bins(BINS)
        self.assertEqual(len(notch), BINS)
        self.assertTrue(notch[BINS // 2])
        self.assertFalse(notch[0])
        self.assertFalse(notch[-1])
        # 中心挖掉的格数是偶数（±两侧），且远小于整窗
        self.assertLess(sum(notch) / BINS, 0.05)

    def test_runs_of_state_bridges_masked(self):
        # True / None / True 必须连成一段（窗中心被挖时不能把宽带信号劈成两半）
        self.assertEqual(runs_of_state([True, None, True]), [(0, 2)])
        self.assertEqual(runs_of_state([True, False, True]), [(0, 0), (2, 2)])
        self.assertEqual(runs_of_state([None, None, None]), [])
        self.assertEqual(runs_of_state([None, True, None]), [(0, 2)])

    def test_kind_of(self):
        self.assertEqual(kind_of(20e6, 0.90), KIND_AMBIGUOUS)
        self.assertEqual(kind_of(0.3e6, 0.90), KIND_CW)
        self.assertEqual(kind_of(0.3e6, 0.20), KIND_BURST)
        self.assertEqual(kind_of(3e6, 0.85), KIND_STEADY)
        self.assertEqual(kind_of(3e6, 0.30), KIND_BURST)


# ---------------------------------------------------------------- 候选判定
class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.plugin = CaptureMode()
        self.window = window_centers()[2]  # 2.4G 中段的某个窗

    def test_flat_noise_no_candidate(self):
        feed(self.plugin, self.window, [flat() for _ in range(20)])
        self.assertEqual(self.plugin.analyze(), [])
        self.assertEqual(self.plugin.pending, [])

    def test_steady_wide_is_candidate(self):
        feed(self.plugin, self.window, steady(self.window, self.window + 3e6, 12e6, ALERT_DB + 8))
        found = self.plugin.analyze()
        self.assertTrue(found, "稳态宽带凸起必须判出候选")
        widest = max(found, key=lambda c: c.width_hz)
        self.assertGreater(widest.width_hz, 8e6)
        self.assertGreater(widest.peak_excess_db, ALERT_DB)
        self.assertGreaterEqual(widest.fill, 0.75)

    def test_narrow_steady_spur_reported_not_captured(self):
        # 3 格宽（117 kHz）的持续源 = 本机杂散：要进 spurs 清单，但不许抓
        feed(self.plugin, self.window, steady(self.window, self.window + 4e6, BIN_HZ * 3, ALERT_DB + 12))
        found = self.plugin.analyze()
        self.assertTrue(found, "杂散也得报出来，不许悄悄丢掉")
        self.assertEqual(self.plugin.pending, [], "本机杂散不许进抓档队列")
        self.assertGreaterEqual(len(self.plugin.spurs), 1)
        self.assertGreaterEqual(self.plugin.skipped_spur, 1)
        self.assertTrue(all(c.width_hz <= SPUR_MAX_HZ for c in self.plugin.spurs))
        payload = self.plugin.web_payload()
        self.assertEqual(payload["spurs"][0]["width_khz"], round(self.plugin.spurs[0].width_hz / 1e3, 1))

    def test_narrow_steady_source_is_candidate_and_queued(self):
        # 1.2 MHz 持续窄带：宽过 SPUR_MAX_HZ(0.5 MHz) → 当可疑信号抓
        feed(self.plugin, self.window, steady(self.window, self.window + 4e6, 1_200_000.0, ALERT_DB + 12))
        found = self.plugin.analyze()
        self.assertTrue(found)
        narrow = min(found, key=lambda c: c.width_hz)
        self.assertEqual(narrow.classification.label, "unknown")
        self.assertTrue(self.plugin.pending, "unknown 的候选必须进抓档队列")
        request = self.plugin.pending[0]
        self.assertEqual(request.bits, CAPTURE_BITS)
        self.assertEqual(request.rate_code, CAPTURE_RATE_CODE)
        self.assertEqual(request.samples % 1_000_000, request.samples)  # 只是样本数，不是频率
        self.assertLessEqual(request.samples, FIRMWARE_MAX_SAMPLES)
        self.assertEqual(request.freq_hz % 1_000_000, 0, "固件 FREQ 只收整 MHz")

    def test_center_spike_ignored(self):
        # 窗中心 ±250 kHz 的直流/LO 泄漏：真板实测 13 个窗全有，不挖就是 13 条假告警
        bins = [flat() for _ in range(16)]
        center = BINS // 2
        for frame in bins:
            for delta in (-2, -1, 0, 1, 2):
                frame[center + delta] += 40.0
        feed(self.plugin, self.window, bins)
        self.assertEqual(self.plugin.analyze(), [])

    def test_edge_cut_segment_dropped(self):
        # 强源的尾巴被窗边界切开 → 宽度不可信 → 丢弃，而不是当成 5 MHz 的可疑源反复抓
        feed(self.plugin, self.window, steady(self.window, self.window - 5e6, 12e6, ALERT_DB + 10))
        found = self.plugin.analyze()
        self.assertEqual([c for c in found if c.width_hz < 15e6], [])
        self.assertGreater(self.plugin.skipped_edge, 0)

    def test_edge_rolloff_ignored(self):
        bins = [flat() for _ in range(16)]
        for frame in bins:
            for index in range(0, 6):
                frame[index] += 35.0
                frame[-1 - index] += 35.0
        feed(self.plugin, self.window, bins)
        self.assertEqual(self.plugin.analyze(), [])

    def test_wideband_classified_microwave_and_not_queued(self):
        # 占窗 ~85% 的宽带 → 大类微波炉/宽带干扰（已知类）→ 记录但不抓
        feed(self.plugin, self.window, steady(self.window, self.window, 17e6, ALERT_DB + 10))
        found = self.plugin.analyze()
        self.assertTrue(found)
        self.assertEqual(found[0].classification.label, "microwave")
        self.assertEqual(found[0].classification.notes.get("ambiguous"), "wideband_continuous")
        self.assertEqual(self.plugin.pending, [], "宽带连续是已知大类，不抓")

    def test_intermittent_unknown_is_queued(self):
        # 3 MHz 只占 25% 时间 → 分不清的间歇源 → 必须抓
        bins = []
        for index in range(40):
            frame = flat()
            if index % 4 == 0:
                # 占空 25% 的源要在均值谱上抬 ≥ ALERT_DB，单帧抬升就得 ×4；
                # 40 帧里 10 帧有信号，才过得了「连续 ≥MIN_FRAMES 帧」这道门
                frame = add_run(frame, self.window + 3e6, 3e6, ALERT_DB * 4, self.window)
            bins.append(frame)
        feed(self.plugin, self.window, bins)
        found = self.plugin.analyze()
        self.assertTrue(found, "间歇未知源必须判出候选")
        self.assertEqual(found[0].classification.label, "unknown")
        self.assertTrue(self.plugin.pending)

    def test_full_window_rise_is_not_a_signal(self):
        # 连 p10 都抬起来 = 增益/底噪整体漂移，不是信号（阶段 3 的教训），必须一条都不判
        feed(self.plugin, self.window, steady(self.window, self.window, 19.8e6, ALERT_DB + 20))
        self.assertEqual(self.plugin.analyze(), [])
        self.assertEqual(self.plugin.pending, [])

    def test_wifi_like_not_queued(self):
        # 12 MHz 持续凸起 → 判 wifi（已知类型）→ 不进队列
        feed(self.plugin, self.window, steady(self.window, self.window + 3e6, 12e6, ALERT_DB + 10))
        found = self.plugin.analyze()
        self.assertTrue(found)
        self.assertEqual(found[0].classification.label, "wifi")
        self.assertEqual(self.plugin.pending, [], "已知类型不抓")

    def test_too_few_frames_no_candidate(self):
        feed(self.plugin, self.window, steady(self.window, self.window, 10e6, ALERT_DB + 10, count=MIN_FRAMES - 2))
        self.assertEqual(self.plugin.analyze(), [])

    def test_dropped_frames_counted(self):
        feed(self.plugin, self.window, [flat(level=0.0) for _ in range(5)])
        self.assertGreater(self.plugin.dropped, 0)
        self.assertEqual(self.plugin.frames, 0)

    def test_ask_frames_baseline(self):
        requests = self.plugin.scan_requests()
        self.assertEqual(len(requests), len(window_centers()))
        self.assertTrue(all(isinstance(r, ScanRequest) for r in requests))
        self.assertTrue(all(r.bw_hz == SCAN_BW_HZ and r.bins == BINS for r in requests))


# ---------------------------------------------------------------- 抓档链路
class CaptureFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cap-test-")
        self.plugin = CaptureMode(out_dir=self.tmp)
        self.window = window_centers()[2]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _queue_one(self) -> CaptureRequest:
        feed(self.plugin, self.window, steady(self.window, self.window + 4e6, 1_200_000.0, ALERT_DB + 12))
        self.plugin.analyze()
        self.assertTrue(self.plugin.pending)
        return self.plugin.pending[0]

    def test_take_pending_empties_queue(self):
        self._queue_one()
        taken = self.plugin.take_pending()
        self.assertEqual(len(taken), 1)
        self.assertEqual(self.plugin.pending, [])

    def test_deliver_writes_iq_and_sidecar(self):
        request = self._queue_one()
        capture = synthetic_capture(request.freq_hz, request.samples, request.rate_code, request.bits)
        record = self.plugin.deliver(request, capture)
        self.assertIsNotNone(record)
        self.assertTrue(os.path.exists(record.path))
        self.assertTrue(os.path.exists(record.sidecar))
        self.assertEqual(os.path.getsize(record.path), record.samples * 4)  # int16 交错 I/Q
        with open(record.sidecar, encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual(meta["samples"], record.samples)
        self.assertEqual(meta["freq_hz"], record.freq_hz)
        self.assertEqual(meta["sha256"], record.sha256)
        self.assertEqual(meta["classification"]["label"], "unknown")
        self.assertTrue(meta["boundary"])
        self.assertEqual(self.plugin.records, [record])

    def test_deliver_rejects_junk_without_raising(self):
        request = self._queue_one()
        bad = FakeCapture(samples_i=(), samples_q=(), bits=20, rate_code=2, duration_us=0)
        # 空样本要给出「空抓档」提示，而不是静默成功
        record = self.plugin.deliver(request, bad)
        self.assertIsNotNone(record)
        self.assertEqual(record.samples, 0)
        self.assertIn("空抓档", record.note)

    def test_deliver_survives_write_error(self):
        request = self._queue_one()
        self.plugin.out_dir = os.path.join(self.tmp, "no", "such", "\0bad")
        capture = synthetic_capture(request.freq_hz, 64, request.rate_code, request.bits)
        self.assertIsNone(self.plugin.deliver(request, capture))
        self.assertTrue(self.plugin.errors)

    def test_store_event_written(self):
        db = os.path.join(self.tmp, "cap.db")
        with Store(db) as store:
            self.plugin.store = store
            request = self._queue_one()
            self.plugin.deliver(request, synthetic_capture(request.freq_hz, 256, request.rate_code, request.bits))
            events = store.events("capture")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].module, "capture")
            self.assertEqual(events[0].kind, "capture")
            self.assertEqual(events[0].payload["label"], "unknown")
            self.assertEqual(events[0].payload["id"], "CAP-001")

    def test_repeat_suppression(self):
        self._queue_one()
        self.plugin.take_pending()
        # 同一频点 15 s 后再来一次 → 被 RETRIGGER_S 挡掉
        feed(
            self.plugin,
            self.window,
            steady(self.window, self.window + 4e6, 1_200_000.0, ALERT_DB + 12),
            t0=1015.0,
        )
        self.plugin.analyze()
        self.assertEqual(self.plugin.pending, [])
        self.assertGreater(self.plugin.skipped_repeat, 0)

    def test_budget_cap(self):
        plugin = self.plugin
        for index in range(MAX_CAPTURES + 3):
            centre = BAND_LO_HZ + 2e6 + index * (MATCH_HZ + 0.5e6)
            if centre > BAND_HI_HZ - 2e6:
                break
            window = window_centers()[0]
            cand = Candidate(
                center_hz=centre,
                width_hz=1_200_000,  # 宽过 SPUR_MAX_HZ，否则会被当本机杂散挡掉
                fill=0.95,
                frames=MIN_FRAMES,
                peak_excess_db=ALERT_DB + 5,
                window_hz=window,
                kind=KIND_CW,
                classification=__import__("core.classifier", fromlist=["classify"]).classify(
                    __import__("core.classifier", fromlist=["SignalFeatures"]).SignalFeatures(
                        bw_hz=200_000, duration_s=5.0, duty_cycle=0.95
                    )
                ),
                first_seen=0.0,
                last_seen=5.0,
            )
            plugin.seen.clear()  # 本用例只验预算上限，重复抑制另行覆盖
            plugin._maybe_queue(cand)
        self.assertLessEqual(len(plugin.pending), MAX_CAPTURES)
        self.assertGreater(plugin.skipped_budget, 0)


class ContextTest(unittest.TestCase):
    """抓档 sidecar 必须带前后各 30 s 的频谱上下文（任务书要求）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cap-ctx-")
        self.db = os.path.join(self.tmp, "ctx.db")
        self.store = Store(self.db)
        self.plugin = CaptureMode(out_dir=self.tmp, store=self.store)
        self.window = window_centers()[2]

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _feed_and_capture(self):
        frames = steady(self.window, self.window + 4e6, 1_200_000.0, 30.0)
        base = time.time()  # deliver() 用墙上时间取上下文，喂帧也要在同一条时间线上
        for index, bins in enumerate(frames):
            snap = Snapshot(ts=base + index * 0.5, center_hz=self.window, bw_hz=SCAN_BW_HZ, bins_db=tuple(bins))
            self.store.add(snap, module="capture")
            self.plugin.on_snapshot(snap)
        self.plugin.analyze()
        request = self.plugin.pending[0]
        record = self.plugin.deliver(request, synthetic_capture(request.freq_hz, 64, request.rate_code, request.bits))
        with open(record.sidecar, encoding="utf-8") as handle:
            return record, json.load(handle)

    def test_sidecar_has_context(self):
        record, meta = self._feed_and_capture()
        context = meta["context"]
        self.assertIsNotNone(context, meta.get("context_note"))
        self.assertEqual(context["window_hz"], int(self.window))
        self.assertEqual(context["before_s"], CONTEXT_S)
        self.assertGreater(context["frames"], 0)
        self.assertEqual(context["spectrum_bins"], CONTEXT_BINS)
        self.assertEqual(len(context["mean_spectrum"]), CONTEXT_BINS)
        self.assertIn("相对码", context["note"])

    def test_no_store_means_no_fake_context(self):
        plugin = CaptureMode(out_dir=self.tmp)
        plugin.pending.append(
            CaptureRequest(2_440_000_000, 64, CAPTURE_RATE_CODE, CAPTURE_BITS,
                           Candidate(center_hz=2_440_000_000, width_hz=300_000, fill=0.9, frames=10,
                                     peak_excess_db=25.0, window_hz=2_440_000_000, kind=KIND_CW,
                                     classification=__import__("core.classifier", fromlist=["classify"]).classify(
                                         __import__("core.classifier", fromlist=["SignalFeatures"]).SignalFeatures(
                                             bw_hz=300_000, duration_s=5.0, duty_cycle=0.9))))
        )
        request = plugin.pending[0]
        record = plugin.deliver(request, synthetic_capture(request.freq_hz, 64, request.rate_code, request.bits))
        with open(record.sidecar, encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertIsNone(meta["context"])
        self.assertIn("没有可用快照上下文", meta["context_note"])


class EndToEndMockTest(unittest.TestCase):
    """真跑一遍 mock 源：未知窄带必须抓、已知 WiFi 场景一条都不抓。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cap-e2e-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, scenario: str, seconds: float = 12.0) -> CaptureMode:
        from app.capture_link import CaptureAwareSource
        from core.registry import Registry
        from core.scheduler import Scheduler
        from tests.mock_source import make_source

        plugin = CaptureMode(out_dir=os.path.join(self.tmp, scenario))
        registry = Registry()
        registry.register(plugin)
        source = make_source(scenario, realtime=False, frames_per_scan=2, frame_interval_s=0.01)
        wrapper = CaptureAwareSource(source, [plugin], synthetic_capture)
        Scheduler(wrapper, registry).run(duration_s=seconds)
        wrapper.flush()
        return plugin

    def test_unknown_cw_triggers_capture(self):
        # 用 unknown_cw_clean：CW 落在 2454 MHz（没人占的频点）。原来的 unknown_cw 把 CW
        # 埋在 20 MHz WiFi 信道里面（2413 MHz），物理上就分不开 —— 那是「环境饱和」，
        # 抓拍模块按设计不判，不能拿它当验收用例。
        plugin = self._run("unknown_cw_clean")
        self.assertTrue(plugin.records, "未知窄带必须触发抓档")
        record = plugin.records[0]
        self.assertTrue(os.path.exists(record.path))
        self.assertEqual(record.label, "unknown")
        self.assertGreater(record.samples, 0)
        # CW 在 2454 MHz：允许有别的记录（噪声/邻道残留），但 2454 必须被抓到
        self.assertTrue(
            [r for r in plugin.records if abs(r.freq_hz - 2_454_000_000) <= 6_000_000],
            f"2454 MHz 的 CW 必须被抓到，实际 {[r.freq_hz/1e6 for r in plugin.records]}",
        )
        sidecar = json.load(open(record.sidecar, encoding="utf-8"))
        self.assertIn("wire", sidecar, "sidecar 必须写清线上格式（CAP20 是 10 bit 打包，不是 int16）")
        self.assertEqual(sidecar["bytes"], record.samples * 4)

    def test_cw_inside_saturated_window_is_not_captured(self):
        # 反面：CW 埋在满负荷 WiFi 信道里 → 环境饱和，不做逐段判定，也就不该抓
        plugin = self._run("unknown_cw")
        self.assertEqual([r for r in plugin.records if abs(r.freq_hz - 2_413_000_000) < 6e6], [])
        self.assertGreater(plugin.skipped_busy, 0)

    def test_known_wifi_does_not_trigger(self):
        plugin = self._run("wifi_busy")
        self.assertEqual(plugin.records, [], "已知 WiFi/BLE 场景不该抓档")
        self.assertEqual(plugin.triggers, 0)


class SourceWrapTest(unittest.TestCase):
    """``CaptureAwareSource``：抓档必须发生在扫描结束之后。"""

    class FakeSource:
        def __init__(self):
            self.calls = []

        def scan(self, request):
            self.calls.append(request)
            yield Snapshot(ts=1.0, center_hz=2_440_000_000, bw_hz=SCAN_BW_HZ, bins_db=tuple(flat()))

    def test_flush_after_scan(self):
        plugin = CaptureMode()
        plugin.pending.append(
            CaptureRequest(
                freq_hz=2_440_000_000,
                samples=256,
                rate_code=CAPTURE_RATE_CODE,
                bits=CAPTURE_BITS,
                candidate=Candidate(
                    center_hz=2_440_000_000,
                    width_hz=300_000,
                    fill=0.9,
                    frames=10,
                    peak_excess_db=25.0,
                    window_hz=2_440_000_000,
                    kind=KIND_CW,
                    classification=__import__("core.classifier", fromlist=["classify"]).classify(
                        __import__("core.classifier", fromlist=["SignalFeatures"]).SignalFeatures(
                            bw_hz=300_000, duration_s=5.0, duty_cycle=0.9
                        )
                    ),
                ),
            )
        )
        order: list[str] = []

        def capture_fn(freq, samples, rate, bits):
            order.append("capture")
            return synthetic_capture(freq, min(samples, 64), rate, bits)

        source = self.FakeSource()
        wrapper = CaptureAwareSource(source, [plugin], capture_fn, logger=order.append)
        frames = list(wrapper.scan(ScanRequest(2_440_000_000, SCAN_BW_HZ, 100, BINS)))
        self.assertEqual(len(frames), 1)
        self.assertEqual(order[0], "capture")  # 扫描 yield 完之后才抓
        self.assertEqual(len(plugin.records), 1)
        self.assertEqual(wrapper.done, 1)
        self.assertEqual(wrapper.failed, 0)

    def test_failure_does_not_raise(self):
        plugin = CaptureMode()
        plugin.pending.append(
            CaptureRequest(2_440_000_000, 256, 2, 20, Candidate(
                center_hz=2_440_000_000, width_hz=300_000, fill=0.9, frames=10,
                peak_excess_db=25.0, window_hz=2_440_000_000, kind=KIND_CW,
                classification=__import__("core.classifier", fromlist=["classify"]).classify(
                    __import__("core.classifier", fromlist=["SignalFeatures"]).SignalFeatures(
                        bw_hz=300_000, duration_s=5.0, duty_cycle=0.9))
            ))
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError("串口忙")

        wrapper = CaptureAwareSource(self.FakeSource(), [plugin], boom)
        list(wrapper.scan(ScanRequest(2_440_000_000, SCAN_BW_HZ, 100, BINS)))
        self.assertEqual(wrapper.failed, 1)
        self.assertEqual(plugin.records, [])
        self.assertTrue(plugin.errors)

    def test_iter_capture_plugins(self):
        class Plain:
            id = "plain"

        self.assertEqual(iter_capture_plugins([Plain(), CaptureMode()]), [self._capture_of()])
        self.assertEqual(iter_capture_plugins([Plain()]), [])

    def _capture_of(self):
        # 单独造一个实例做比较（id() 不同，用类型比较）
        for plugin in iter_capture_plugins([CaptureMode()]):
            return plugin
        raise AssertionError("CaptureMode 应被识别为抓档插件")


class OutputTest(unittest.TestCase):
    def test_empty_output(self):
        output = CaptureMode().produce()
        self.assertEqual(output.kind, "report")
        self.assertTrue(output.notes)
        self.assertEqual(output.payload["captures"], [])

    def test_payload_thresholds(self):
        payload = CaptureMode().web_payload()
        self.assertEqual(payload["kind"], "capture")
        self.assertIn("alert_db", payload["thresholds"])
        self.assertIn("max_captures", payload["thresholds"])
        self.assertEqual(payload["band"]["lo_hz"], BAND_LO_HZ)
        self.assertTrue(any("不是 dBm" in note for note in payload["notes"]))
        self.assertTrue(any("启发式" in note for note in payload["notes"]))

    def test_record_row(self):
        tmp = tempfile.mkdtemp(prefix="cap-row-")
        try:
            plugin = CaptureMode(out_dir=tmp)
            feed(plugin, window_centers()[2], steady(window_centers()[2], window_centers()[2] + 4e6, 1_200_000.0, 30.0))
            plugin.analyze()
            request = plugin.pending[0]
            record = plugin.deliver(request, synthetic_capture(request.freq_hz, 128, request.rate_code, request.bits))
            row = record.row()
            self.assertEqual(len(row), 10)
            self.assertTrue(row[0].startswith("CAP-"))
            self.assertEqual(plugin.produce().kind, "archive")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WindowStatTest(unittest.TestCase):
    def test_coverage(self):
        stat = WindowStat(center_hz=2_440_000_000)
        stat.add(Snapshot(ts=1.0, center_hz=2_440_000_000, bw_hz=SCAN_BW_HZ, bins_db=tuple(flat())))
        stat.add(Snapshot(ts=2.0, center_hz=2_440_000_000, bw_hz=SCAN_BW_HZ, bins_db=tuple(flat(level=0.0))))
        self.assertAlmostEqual(stat.coverage(), 0.5)

    def test_ring_keeps_recent(self):
        stat = WindowStat(center_hz=2_440_000_000)
        frames = [list(flat()) for _ in range(4)]
        for index, frame in enumerate(frames):
            frame[10] = float(index)
            stat.add(Snapshot(ts=float(index), center_hz=2_440_000_000, bw_hz=SCAN_BW_HZ, bins_db=tuple(frame)))
        recent = stat.recent(1.5, now=3.0)
        self.assertEqual([ts for ts, _ in recent], [2.0, 3.0])

    def test_clock_going_backwards_clears(self):
        stat = WindowStat(center_hz=2_440_000_000)
        stat.add(Snapshot(ts=100.0, center_hz=2_440_000_000, bw_hz=SCAN_BW_HZ, bins_db=tuple(flat())))
        stat.add(Snapshot(ts=10.0, center_hz=2_440_000_000, bw_hz=SCAN_BW_HZ, bins_db=tuple(flat())))
        self.assertEqual(len(stat.frames), 1)


if __name__ == "__main__":
    unittest.main()