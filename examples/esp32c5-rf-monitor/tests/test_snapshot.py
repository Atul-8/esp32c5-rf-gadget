"""Snapshot schema 与频谱帧解析。"""
import unittest

from core.snapshot import (
    Snapshot,
    SnapshotError,
    decode_spectrum_frame,
    decode_stats,
    quantiles,
)
from tests.fake_firmware import encode_spectrum_frame


def flat(bins=512, value=20):
    return tuple([value] * bins)


class SnapshotSchema(unittest.TestCase):
    def test_geometry_and_peak(self):
        bins = list(flat())
        bins[128] = 200
        snap = Snapshot(ts=1.0, center_hz=2_442_000_000, bw_hz=80_000_000, bins_db=tuple(bins))
        self.assertEqual(snap.bin_count, 512)
        self.assertAlmostEqual(snap.bin_width_hz, 80e6 / 512)
        peak_hz, peak_db = snap.peak()
        self.assertEqual(peak_db, 200)
        # 第 128 个 bin 的中心 = 2442 - 40 + 128.5*0.15625 = 2422.078125 MHz
        self.assertAlmostEqual(peak_hz / 1e6, 2422.078125, places=4)
        self.assertAlmostEqual(snap.bin_freq_hz(0) / 1e6, 2402.078125, places=4)
        self.assertEqual(snap.band_indices(2_402_000_000, 2_422_000_000), (0, 127))

    def test_occupancy_and_band_helpers(self):
        bins = [10] * 512
        for index in range(0, 128):
            bins[index] = 60
        snap = Snapshot(1.0, 2_442_000_000, 80_000_000, tuple(bins))
        self.assertAlmostEqual(snap.occupancy(50), 0.25)
        self.assertAlmostEqual(snap.band_occupancy(2_402_000_000, 2_422_000_000, 50), 1.0)
        self.assertAlmostEqual(snap.band_occupancy(2_422_000_000, 2_482_000_000, 50), 0.0)
        self.assertEqual(snap.band_peak_db(2_402_000_000, 2_422_000_000), 60)
        self.assertLessEqual(snap.noise_floor_db, 20)

    def test_validation(self):
        with self.assertRaises(SnapshotError):
            Snapshot(1.0, 2_442_000_000, 80_000_000, ())
        with self.assertRaises(SnapshotError):
            Snapshot(1.0, 2_442_000_000, 0, flat())
        with self.assertRaises(SnapshotError):
            Snapshot(1.0, 2_442_000_000, 80_000_000, flat(300))

    def test_quantiles(self):
        q = quantiles([1, 2, 3, 4, 5])
        self.assertEqual(q["min"], 1)
        self.assertEqual(q["max"], 5)
        self.assertEqual(q["median"], 3)


class FrameDecoding(unittest.TestCase):
    def test_roundtrip_keeps_frequency_order(self):
        bins = list(flat())
        bins[256] = 180  # 升序数组里第 256 个 bin = 中心频点
        frame_bytes = encode_spectrum_frame(bins, units=3, samples=1536)
        frame = decode_spectrum_frame(frame_bytes)
        self.assertEqual(frame.bin_count, 512)
        self.assertEqual(frame.ffts, 3)
        self.assertTrue(frame.snapshot_gaps)
        snap = frame.to_snapshot(ts=2.0, center_hz=2_442_000_000, bw_hz=80_000_000)
        peak_hz, peak_db = snap.peak()
        self.assertEqual(peak_db, 180)
        self.assertLess(abs(peak_hz - 2_442_000_000), 200_000)
        self.assertEqual(snap.meta["ffts"], 3)
        self.assertEqual(snap.meta["detector"], "mean")

    def test_bad_crc_rejected(self):
        frame_bytes = bytearray(encode_spectrum_frame(flat()))
        frame_bytes[-1] ^= 0xFF
        with self.assertRaises(SnapshotError):
            decode_spectrum_frame(bytes(frame_bytes))

    def test_truncated_frame_rejected(self):
        with self.assertRaises(SnapshotError):
            decode_spectrum_frame(encode_spectrum_frame(flat())[:-8])

    def test_wrong_magic_rejected(self):
        frame_bytes = bytearray(encode_spectrum_frame(flat()))
        frame_bytes[0:4] = b"XXXX"
        with self.assertRaises(SnapshotError):
            decode_spectrum_frame(bytes(frame_bytes))

    def test_stats_record(self):
        import zlib

        body = bytearray(36)
        body[0:4] = b"SPS1"
        body[8:10] = (250).to_bytes(2, "little")
        body[32:36] = (1234).to_bytes(4, "little")
        record = bytes(body) + (zlib.crc32(bytes(body)) & 0xFFFFFFFF).to_bytes(4, "little")
        stats = decode_stats(record)
        self.assertEqual(stats["fft_coverage_permille"], 250)
        self.assertEqual(stats["ffts_per_s"], 1234)


if __name__ == "__main__":
    unittest.main()