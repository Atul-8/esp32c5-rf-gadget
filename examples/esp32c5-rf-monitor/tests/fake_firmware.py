"""假固件：一个假的 pyserial 端口，按 docs/protocol.md 的格式回话。

作用：没有 ESP32-C5 在手也能测试 ``core.serial_link`` 的全部解析路径
（SYNC 租约、INFO/CAPS、SPEC 头 + SPC1 帧 + SPECEND、CAP16/CAP20）。
"""
from __future__ import annotations

import math
import zlib
from typing import Any, Iterable, Sequence

from tests.mock_source import SpectrumSynthesizer

C5_PROFILES = [
    [80_000_000, 0, 256, 1, 1, 0],
    [80_000_000, 0, 512, 1, 1, 0],
    [80_000_000, 0, 1024, 1, 1, 0],
    [40_000_000, 1, 512, 1, 1, 0],
    [20_000_000, 2, 512, 1, 1, 0],
    [10_000_000, 3, 1024, 1, 1, 0],
    [8_000_000, 4, 512, 1, 1, 0],
    [4_000_000, 5, 256, 1, 1, 0],
]

RATE_HZ = {0: 80_000_000, 1: 40_000_000, 2: 20_000_000, 3: 10_000_000,
           4: 8_000_000, 5: 4_000_000, 6: 16_000_000, 7: 32_000_000,
           8: 10_666_667, 9: 6_400_000}


def encode_spectrum_frame(
    ascending_codes: Sequence[int],
    seq: int = 0,
    sample_index: int = 0,
    units: int = 1,
    samples: int | None = None,
    detector: int = 0,
    gain: int = 0,
    flags: int = 8,
    code_multiplier: int = 2,
) -> bytes:
    """按固件格式打包一帧 SPC1（线上顺序是 DC 在首，负频率在后半段）。"""
    bins = len(ascending_codes)
    if bins < 256 or bins & (bins - 1):
        raise ValueError(f"FFT 点数必须是 256..2048 的 2 次幂：{bins}")
    half = bins // 2
    wire = list(ascending_codes[half:]) + list(ascending_codes[:half])
    header = bytearray(28)
    header[0:4] = b"SPC1"
    header[4:8] = seq.to_bytes(4, "little")
    header[8:16] = sample_index.to_bytes(8, "little")
    header[16:20] = (samples if samples is not None else bins * units).to_bytes(4, "little")
    header[20:22] = units.to_bytes(2, "little")
    header[22] = (8 if flags & 8 else 0) | (1 if detector else 0)
    header[23] = gain
    header[26] = int(math.log2(bins))
    header[27] = code_multiplier
    body = bytes(int(max(0, min(255, v))) for v in wire)
    payload = bytes(header) + body
    return payload + (zlib.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "little")


class FakeFirmware:
    """假的 pyserial 串口对象（构造参数与 ``serial.Serial`` 兼容）。"""

    def __init__(
        self,
        port: str = "MOCK",
        baudrate: int = 2_000_000,
        timeout: float = 5.0,
        scenario: str = "mixed",
        seed: int = 1234,
        frames_per_scan: int = 2,
        corrupt_crc: bool = False,
        profiles: Sequence[Sequence[int]] = tuple(C5_PROFILES),
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.synth = SpectrumSynthesizer(scenario, seed)
        self.scenario = scenario
        self.frames_per_scan = frames_per_scan
        self.corrupt_crc = corrupt_crc
        self.profiles = [list(p) for p in profiles]
        self._buffer = bytearray()
        self._commands: list[str] = []
        self.frequency_mhz = 2442
        self.closed = False
        self.spectrum_runs = 0
        self.capture_requests = 0
        self.last_pairs: list[tuple[int, int]] = []
        self.last_payload: bytes = b""
        #: 设备端自扫（FSM）状态：与真固件同语义
        self.fsm_plan: list[tuple[int, int, int]] = []
        self.fsm_running = False
        self.fsm_bins = 512
        self.fsm_rate = 2
        self.fsm_detector = 0
        self.fsm_cursor = 0
        self.fsm_frames = 0

    # ---- pyserial 兼容层 -------------------------------------------------
    def write(self, data: bytes) -> int:
        text = data.decode("ascii", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            self._commands.append(line)
            self._handle(line)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if not self._buffer and self.fsm_running:
            self._fsm_frame()  # 真固件是异步吐帧；假固件只能「主机来读就补一帧」
        if not self._buffer:
            return b""
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk

    def readline(self) -> bytes:
        index = self._buffer.find(b"\n")
        if index < 0:
            return b""
        chunk = bytes(self._buffer[: index + 1])
        del self._buffer[: index + 1]
        return chunk

    def close(self) -> None:
        self.closed = True

    def flush(self) -> None:
        return None

    # ---- 命令处理 --------------------------------------------------------
    @property
    def commands(self) -> list[str]:
        return list(self._commands)

    def _send(self, text: str) -> None:
        self._buffer += text.encode("ascii")

    def _handle(self, line: str) -> None:
        head = line.split()[0]
        if head == "SYNC":
            self._send(line + "\n")
        elif head == "RELEASE":
            self._send("OK\n")
        elif head == "INFO":
            self._send("C5SDR 6 burst 16380\n")
        elif head == "CAPS":
            self._send("CAPS SPEC SPECN SPECCAPS SPECSTAT TRANSPORT DCT LIMITS FSM\n")
        elif head == "SPECINFO?":
            import json

            payload = {"continuous": False, "transports": ["USB", "UART"], "profiles": self.profiles}
            self._send("SPECINFO " + json.dumps(payload) + "\n")
        elif head == "LIMITS?":
            self._send('LIMITS {"gain":[0,60,1],"bandwidth":null,"rates":[80,40,20,10,8,4],"bits":[8,10]}\n')
        elif head == "TRANSPORT?":
            self._send("TRANSPORT USB 0\n")
        elif head == "FREQ":
            self.frequency_mhz = int(line.split()[1])
            self._send("OK\n")
        elif head == "GAIN":
            self._send("OK\n")
        elif head == "BANDWIDTH":
            self._send("OK\n")
        elif head == "DC?":
            self._send("DC 1\n")
        elif head == "SPEC":
            self._spectrum(line)
        elif line.startswith("FSM"):
            self._fsm(line)
        elif head in ("CAP16", "CAP20"):
            self._capture(line)
        elif head == "VERSION?":
            self._send('VERSION {"revision":"mock","build":"mock"}\n')
        else:
            self._send(f"ERR command {head}\n")

    def _spectrum(self, line: str) -> None:
        fields = line.split()
        if len(fields) < 7:
            self._send("ERR spec_args\n")
            return
        units, detector, rate, bins = (int(fields[3]), int(fields[4]), int(fields[5]), int(fields[6]))
        fs = RATE_HZ.get(rate)
        if fs is None:
            self._send("ERR spec_args\n")
            return
        self.spectrum_runs += 1
        self._send(f"SPEC {bins} {fs} {units} {self.frequency_mhz}\n")
        for frame in range(self.frames_per_scan):
            codes = self.synth.codes(
                self.frequency_mhz * 1_000_000, fs, bins, jitter=True
            )
            data = encode_spectrum_frame(
                codes,
                seq=frame,
                sample_index=frame * bins * units,
                units=units,
                detector=detector,
            )
            if self.corrupt_crc:
                data = data[:-1] + bytes([data[-1] ^ 0xFF])
            self._buffer += data
        self._send(
            f"SPECEND 0 0 {self.frames_per_scan * units} {bins * units * self.frames_per_scan}"
            f" 1000 0 0 {self.frames_per_scan} 0 0 {self.frames_per_scan * units} 0\n"
        )

    # ---- 设备端自扫（FSM）------------------------------------------------
    # 与真固件同一套语义：FSMPLAN 存计划、FSM START 之后不等主机发命令就吐帧。
    def _fsm(self, line: str) -> None:
        fields = line.split()
        head = fields[0]
        if head == "FSMPLAN" and len(fields) >= 2:
            if fields[1] == "CLR":
                self.fsm_plan = []
                self._send("OK\n")
            elif fields[1] == "ADD":
                if len(fields) < 4:
                    self._send("ERR fsm_plan_args\n")
                    return
                module = int(fields[4]) if len(fields) > 4 else 0xFF
                self.fsm_plan.append((int(fields[2]), int(fields[3]), module))
                self._send("OK\n")
            else:
                self._send("ERR fsm_plan_args\n")
            return
        if line in ("FSMPLAN?", "FSM PLAN?"):
            self._send(f"FSMPLAN {len(self.fsm_plan)}\n")
            for index, (mhz, dwell, module) in enumerate(self.fsm_plan):
                self._send(f"W {index} {mhz} {dwell} {module}\n")
            self._send("FSMPLANEND\n")
            return
        if line == "FSM?":
            state = "sweeping" if self.fsm_running else "idle"
            self._send(f"FSM {state} {len(self.fsm_plan)} {self.fsm_cursor} 0 {self.fsm_frames} 0 0 0\n")
            return
        if line == "FSM STOP":
            was = 1 if self.fsm_running else 0
            self.fsm_running = False
            self.fsm_cursor = 0
            self._send(f"FSMEND {was}\n")
            return
        if head == "FSM" and len(fields) >= 2 and fields[1] == "START":
            if len(fields) < 4:
                self._send("ERR fsm_args\n")
                return
            if not self.fsm_plan:
                self._send("ERR fsm_no_plan\n")
                return
            self.fsm_bins, self.fsm_rate = int(fields[2]), int(fields[3])
            self.fsm_detector = int(fields[4]) if len(fields) > 4 else 0
            self.fsm_running = True
            self.fsm_cursor = 0
            self.fsm_frames = 0
            self._send(f"FSM START {len(self.fsm_plan)} {self.fsm_bins} {self.fsm_rate} {self.fsm_detector}\n")
            return
        self._send("ERR fsm_args\n")

    def _fsm_frame(self) -> None:
        """按计划补一帧自扫输出（头行 + SPC1），字节布局与真固件一致。"""
        mhz, _dwell, _module = self.fsm_plan[self.fsm_cursor % len(self.fsm_plan)]
        self.frequency_mhz = mhz
        self.fsm_cursor += 1
        self.fsm_frames += 1
        fs = RATE_HZ.get(self.fsm_rate, 20_000_000)
        codes = self.synth.codes(mhz * 1_000_000, fs, self.fsm_bins, jitter=True)
        self._send(f"SPEC {self.fsm_bins} {fs} {self.fsm_bins} {mhz}\n")
        self._buffer += encode_spectrum_frame(
            codes,
            seq=self.fsm_frames,
            sample_index=self.fsm_frames * self.fsm_bins,
            units=self.fsm_bins,
            detector=self.fsm_detector,
        )

    def _capture(self, line: str) -> None:
        fields = line.split()
        tag = fields[0]
        samples = int(fields[1])
        rate = int(fields[2])
        bits = 16 if tag == "CAP16" else 20
        self.capture_requests += 1
        rng = self.synth._rng  # 复用同一随机源，保证可复现
        pairs = [(rng.randint(-128, 127), rng.randint(-128, 127)) for _ in range(samples)]
        self.last_pairs = pairs
        if bits == 16:
            payload = bytearray()
            for i, q in pairs:
                payload.append(i & 0xFF)
                payload.append((-q) & 0xFF)  # 固件传输 (I, -Q)
        else:
            acc = 0
            bitpos = 0
            payload = bytearray()
            for i, q in pairs:
                for value in (i & 0x3FF, (-q) & 0x3FF):  # 10 位补码，LSB 先
                    acc |= value << bitpos
                    bitpos += 10
                    while bitpos >= 8:
                        payload.append(acc & 0xFF)
                        acc >>= 8
                        bitpos -= 8
            if bitpos:
                payload.append(acc & 0xFF)
        crc = zlib.crc32(bytes(payload)) & 0xFFFFFFFF
        self.last_payload = bytes(payload)
        self.last_bits = bits
        self._send(f"DATA {samples} {crc:08x} {samples * 2}\n")
        self._buffer += bytes(payload)


__all__ = ["C5_PROFILES", "FakeFirmware", "encode_spectrum_frame"]