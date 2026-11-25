"""串口协议层：用假固件覆盖 SYNC/CAPS/SPEC/CAP 的全部解析路径。"""
import unittest

from core.registry import ScanRequest
from core.serial_link import EspSdrLink, SerialLinkError
from tests.fake_firmware import FakeFirmware


def make_link(**kwargs) -> EspSdrLink:
    firmware = FakeFirmware(**kwargs)
    link = EspSdrLink("MOCK", factory=lambda port, baud, timeout=5.0: firmware)
    link.open()
    link.firmware = firmware  # type: ignore[attr-defined]
    return link


class Handshake(unittest.TestCase):
    def test_identity_and_capabilities(self):
        link = make_link()
        try:
            self.assertEqual(link.identity, "C5SDR 6 burst 16380")
            self.assertEqual(link.family, "C5SDR")
            self.assertIn("SPECCAPS", link.caps)
            self.assertEqual(link.transport, "USB")
            profiles = link.profiles()
            self.assertTrue(profiles)
            self.assertIn((80_000_000, 0, 512, 1, 1, False), profiles)
            self.assertEqual(link.limits["bits"], [8, 10])
        finally:
            link.close()
        self.assertTrue(link.firmware.closed)

    def test_choose_profile_matches_bandwidth(self):
        link = make_link()
        try:
            self.assertEqual(link.choose_profile(20_000_000, 512)[0], 20_000_000)
            self.assertEqual(link.choose_profile(80_000_000, 1024), (80_000_000, 0, 1024, 1, 1, False))
            self.assertEqual(link.choose_profile(5_000_000, 512)[0], 4_000_000)
            self.assertEqual(link.choose_profile(16_000_000, 512)[0], 20_000_000)
        finally:
            link.close()

    def test_no_spectrum_capability_raises(self):
        firmware = FakeFirmware(profiles=())
        link = EspSdrLink("MOCK", factory=lambda p, b, timeout=5.0: firmware)
        link.open()
        try:
            self.assertEqual(link.profiles(), [])
            with self.assertRaises(SerialLinkError):
                link.choose_profile(20_000_000, 512)
        finally:
            link.close()


class SpectrumScan(unittest.TestCase):
    def test_scan_yields_frames(self):
        link = make_link(frames_per_scan=3)
        try:
            snapshots = list(link.scan(ScanRequest(2_442_000_000, 20_000_000, 200, 512)))
        finally:
            link.close()
        self.assertEqual(len(snapshots), 3)
        first = snapshots[0]
        self.assertEqual(first.center_hz, 2_442_000_000)
        self.assertEqual(first.bw_hz, 20_000_000)
        self.assertEqual(first.bin_count, 512)
        self.assertEqual(first.meta["detector"], "mean")
        self.assertIn("last_scan", link.describe())
        self.assertEqual(link.describe()["last_scan"]["status"], 0)
        self.assertEqual(link.describe()["last_scan"]["frames"], 3)

    def test_scan_respects_frequency_and_bandwidth(self):
        link = make_link(frames_per_scan=1)
        try:
            list(link.scan(ScanRequest(5_745_000_000, 80_000_000, 100, 512)))
            self.assertEqual(link.firmware.frequency_mhz, 5745)
            self.assertEqual(link.describe()["last_scan"]["freq_hz"], 5_745_000_000)
            self.assertEqual(link.describe()["last_scan"]["sample_rate_hz"], 80_000_000)
        finally:
            link.close()

    def test_corrupt_crc_marks_link_failed(self):
        link = make_link(corrupt_crc=True)
        try:
            with self.assertRaises(SerialLinkError):
                list(link.scan(ScanRequest(2_442_000_000, 20_000_000, 100, 512)))
            self.assertTrue(link.failed)
            with self.assertRaises(SerialLinkError):
                list(link.scan(ScanRequest(2_442_000_000, 20_000_000, 100, 512)))
        finally:
            link.close()


class CaptureIq(unittest.TestCase):
    def test_cap16_restores_q_sign(self):
        link = make_link()
        try:
            capture = link.capture_iq(samples=256, rate_code=4, bits=16)
            expected_i, expected_q = zip(*link.firmware.last_pairs)
        finally:
            link.close()
        self.assertEqual(capture.samples_i, tuple(expected_i))
        self.assertEqual(capture.samples_q, tuple(expected_q))
        self.assertEqual(capture.sample_rate_hz, 8_000_000)
        stream = capture.to_stream_bytes()
        self.assertEqual(len(stream), 512)
        self.assertEqual(stream[0], expected_i[0] & 0xFF)
        self.assertEqual(stream[1], expected_q[0] & 0xFF)

    def test_cap20_roundtrip(self):
        link = make_link()
        try:
            capture = link.capture_iq(samples=64, rate_code=1, bits=20)
            payload = link.firmware.last_payload
            expected_i, expected_q = zip(*link.firmware.last_pairs)
        finally:
            link.close()
        self.assertEqual(capture.samples_i, tuple(expected_i))
        self.assertEqual(capture.samples_q, tuple(expected_q))
        self.assertEqual(len(payload), (64 * 20 + 7) // 8)
        # 抓档落盘用的是标准 (I,Q) 顺序（固件线上是 (I,-Q)），所以只有 Q 符号不同
        stream = capture.to_stream_bytes()
        self.assertEqual(len(stream), len(payload))
        self.assertEqual(stream[0:1], payload[0:1])
        # 前 20 bit 解回来必须是 samples_i[0] / samples_q[0]
        acc = int.from_bytes(stream[:3], "little")
        first = acc & 0x3FF
        second = (acc >> 10) & 0x3FF
        self.assertEqual(first - 1024 if first >= 512 else first, capture.samples_i[0])
        self.assertEqual(second - 1024 if second >= 512 else second, capture.samples_q[0])


if __name__ == "__main__":
    unittest.main()