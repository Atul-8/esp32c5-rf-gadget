"""``app/capture_link.py`` 的接口单测：假抓档长相 + 「抓档只在两次扫描之间」纪律。

这一层容易出错的地方都不在算法，而在时序：

1. ``CaptureAwareSource.scan()`` 必须**等整轮扫描 yield 完**才 ``flush()``
   （同一根串口一边扫一边抓会把读缓冲打乱，增益那轮踩过）；
2. 单次抓档失败只许计入 ``failed``，不许把采集带崩；
3. ``max_per_slice=1`` 时每轮只做一条，剩下的留给下一轮（时间片公平）。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capture_link import (  # noqa: E402
    CAPTURE_PROFILES,
    CaptureAwareSource,
    FakeCapture,
    iter_capture_plugins,
    profile_for,
    synthetic_capture,
)
from core.registry import ScanRequest  # noqa: E402
from core.snapshot import Snapshot  # noqa: E402


def make_snapshot(ts: float = 1000.0) -> Snapshot:
    # bins 只能取 256/512/…（core/snapshot.py 的契约），别图省事写 16
    return Snapshot(
        ts=ts,
        center_hz=2_448_000_000,
        bw_hz=20_000_000,
        bins_db=tuple([0.0] * 256),
        meta={"gain_code": 60, "module": "test"},
    )


class FakeRequest:
    """``CaptureRequest`` 的最小替身（只要四个字段）。"""

    def __init__(self, freq_hz: int = 2_454_000_000) -> None:
        self.freq_hz = freq_hz
        self.samples = 4096
        self.rate_code = 2
        self.bits = 20


class FakePlugin:
    """有 ``take_pending`` 的假模块：记录调用顺序，可注入失败。"""

    def __init__(self, requests: list[FakeRequest] | None = None, fail_first: bool = False) -> None:
        self.id = "fake_capture"
        self.pending = list(requests or [])
        self.errors: list[str] = []
        self.delivered: list[tuple[object, object]] = []
        self.calls: list[str] = []
        self.fail_first = fail_first

    def take_pending(self, limit=None):
        self.calls.append("take_pending")
        if limit is None or limit >= len(self.pending):
            out, self.pending = self.pending, []
            return out
        out, self.pending = self.pending[:limit], self.pending[limit:]
        return out

    def deliver(self, request, capture):
        self.calls.append("deliver")
        self.delivered.append((request, capture))
        record = type("Record", (), {"index": len(self.delivered), "freq_hz": request.freq_hz,
                                     "samples": capture.samples_i.__len__(), "label": "unknown",
                                     "path": ""})()
        return record


class TracingSource:
    """扫描源替身：每 yield 一帧就记一笔，用来验证「抓档在扫描之后」。"""

    def __init__(self, frames: int = 3, log: list[str] | None = None) -> None:
        self.frames = frames
        self.log = log if log is not None else []

    def scan(self, request: ScanRequest):
        for i in range(self.frames):
            self.log.append(f"frame{i}")
            yield make_snapshot(1000.0 + i)


class TestSyntheticCapture(unittest.TestCase):
    def test_fields_match_real_capture(self):
        cap = synthetic_capture(2_454_000_000, samples=256)
        self.assertIsInstance(cap, FakeCapture)
        self.assertEqual(len(cap.samples_i), 256)
        self.assertEqual(len(cap.samples_q), 256)
        self.assertEqual(cap.bits, 20)
        self.assertEqual(cap.rate_code, 2)
        self.assertEqual(cap.sample_rate_hz, 20_000_000)
        self.assertEqual(cap.freq_hz, 2_454_000_000)
        # 20 MS/s × 256 样本 = 12.8 µs
        self.assertEqual(cap.duration_us, 12)

    def test_samples_within_bit_range(self):
        cap = synthetic_capture(2_454_000_000, samples=512, bits=20)
        limit = 2 ** 19 - 1
        self.assertLessEqual(max(abs(v) for v in cap.samples_i), limit)
        self.assertLessEqual(max(abs(v) for v in cap.samples_q), limit)

    def test_bit_width_changes_clamp(self):
        cap = synthetic_capture(2_400_000_000, samples=256, bits=16)
        self.assertLessEqual(max(abs(v) for v in cap.samples_i), 127)

    def test_profiles_are_distinguishable(self):
        # cw：单音强 → 前后半段能量接近；burst：后半段几乎只剩噪声
        cw = synthetic_capture(2_454_000_000, samples=1024, amplitude=260.0, noise=12.0, profile="cw")
        burst = synthetic_capture(2_454_000_000, samples=1024, amplitude=260.0, noise=12.0, profile="burst")
        wide = synthetic_capture(2_454_000_000, samples=1024, amplitude=260.0, noise=12.0, profile="wideband")

        def rms(values):
            return (sum(v * v for v in values) / len(values)) ** 0.5

        cw_tail = rms(cw.samples_i[768:])
        burst_tail = rms(burst.samples_i[768:])
        self.assertGreater(cw_tail, 150.0)
        self.assertLess(burst_tail, 40.0)
        # 平谱：没有单根尖峰，样本呈高斯分布（方差大但无周期性）
        self.assertGreater(rms(wide.samples_i), 50.0)
        for cap in (cw, burst, wide):
            self.assertIn(cap.profile, CAPTURE_PROFILES)

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ValueError):
            synthetic_capture(2_454_000_000, samples=64, profile="nonsense")

    def test_profile_for_scenarios(self):
        self.assertEqual(profile_for("video_58g"), "wideband")
        self.assertEqual(profile_for("microwave"), "wideband")
        self.assertEqual(profile_for("wifi_busy"), "wideband")
        self.assertEqual(profile_for("unknown_cw_clean"), "cw")
        self.assertEqual(profile_for(None), "cw")


class TestIterCapturePlugins(unittest.TestCase):
    def test_only_plugins_with_take_pending(self):
        good = FakePlugin()
        bad = object()
        self.assertEqual(iter_capture_plugins([good, bad]), [good])


class TestCaptureAwareSource(unittest.TestCase):
    def test_capture_runs_after_scan(self):
        log: list[str] = []
        plugin = FakePlugin([FakeRequest()])
        plugin.calls = log
        source = TracingSource(frames=3, log=log)
        wrapped = CaptureAwareSource(source, [plugin], synthetic_capture)
        frames = list(wrapped.scan(ScanRequest(freq_hz=2_448_000_000, bw_hz=20_000_000)))
        self.assertEqual(len(frames), 3)
        # 顺序：三帧 → take_pending → deliver（抓档绝不许插在扫描中间）
        self.assertEqual(log, ["frame0", "frame1", "frame2", "take_pending", "deliver"])

    def test_deliver_receives_real_capture(self):
        plugin = FakePlugin([FakeRequest()])
        wrapped = CaptureAwareSource(TracingSource(), [plugin], synthetic_capture)
        wrapped.flush()
        self.assertEqual(wrapped.done, 1)
        request, capture = plugin.delivered[0]
        self.assertEqual(request.freq_hz, 2_454_000_000)
        self.assertEqual(len(capture.samples_i), request.samples)

    def test_single_failure_does_not_break_pipeline(self):
        plugin = FakePlugin([FakeRequest(2_454_000_000), FakeRequest(2_460_000_000)])
        state = {"n": 0}

        def flaky(freq_hz, samples, rate_code, bits):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("串口忙")
            return synthetic_capture(freq_hz, samples, rate_code, bits)

        wrapped = CaptureAwareSource(TracingSource(), [plugin], flaky, max_per_slice=2, logger=lambda _m: None)
        wrapped.flush()
        self.assertEqual(wrapped.failed, 1)
        self.assertEqual(wrapped.done, 1)
        self.assertTrue(plugin.errors and "OSError" in plugin.errors[0])
        self.assertEqual(len(plugin.delivered), 1)

    def test_max_per_slice_keeps_queue_short(self):
        plugin = FakePlugin([FakeRequest(2_450_000_000), FakeRequest(2_460_000_000)])
        wrapped = CaptureAwareSource(TracingSource(), [plugin], synthetic_capture, max_per_slice=1)
        wrapped.flush()
        self.assertEqual(wrapped.done, 1)
        self.assertEqual(len(plugin.pending), 1)  # 剩下那条留到下一轮，不许静默丢
        wrapped.flush()
        self.assertEqual(wrapped.done, 2)

    def test_no_pending_is_noop(self):
        plugin = FakePlugin([])
        wrapped = CaptureAwareSource(TracingSource(), [plugin], synthetic_capture)
        self.assertEqual(wrapped.flush(), 0)
        self.assertEqual(wrapped.done, 0)
        self.assertEqual(plugin.delivered, [])

    def test_plugin_without_hooks_ignored(self):
        wrapped = CaptureAwareSource(TracingSource(), [object()], synthetic_capture)
        self.assertEqual(wrapped.flush(), 0)

    def test_attributes_pass_through(self):
        class Src:
            label = "real-source"

        self.assertEqual(CaptureAwareSource(Src(), [], synthetic_capture).label, "real-source")


if __name__ == "__main__":
    unittest.main(verbosity=2)