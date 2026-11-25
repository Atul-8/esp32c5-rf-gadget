"""esp-sdr 串口协议客户端（连接、发指令、收帧）。

协议事实源：仓库 ``docs/protocol.md`` 与 ``docs/spectrum.md``。
ESP32-C5 要点：Serial/JTAG（USB）或 UART0，2 Mbaud，8N1；
``SPEC`` 快照频谱支持 4/8/10/20/40/80 MS/s 与 256..2048 bin。

本文件只做协议封装，不做业务判断。任何功能模块都不许改它。
"""
from __future__ import annotations

import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .registry import ScanRequest
from .snapshot import (
    END_MAGIC,
    FRAME_HEADER_BYTES,
    FRAME_TRAILER_BYTES,
    SPECTRUM_MAGIC,
    STATS_BYTES,
    STATS_MAGIC,
    Snapshot,
    SnapshotError,
    SpectrumFrame,
    decode_spectrum_frame,
    decode_stats,
)

DEFAULT_BAUD = 2_000_000
SYNC_TIMEOUT_S = 10.0

#: 采样率码 → 标称速率（Hz）。见 docs/protocol.md。
RATE_TABLE = {
    0: 80_000_000,
    1: 40_000_000,
    2: 20_000_000,
    3: 10_000_000,
    4: 8_000_000,
    5: 4_000_000,
    6: 16_000_000,
    7: 32_000_000,
    8: 10_666_667,
    9: 6_400_000,
}


class SerialLinkError(RuntimeError):
    """串口/协议层错误（含帧同步丢失）。"""


@dataclass
class IqCapture:
    """一次 ``CAP16``/``CAP20`` 突发抓取结果，``(I, Q)`` 已还原标准符号。"""

    samples_i: tuple[int, ...]
    samples_q: tuple[int, ...]
    bits: int
    rate_code: int
    duration_us: int
    freq_hz: int = 0
    gain_code: int | None = None

    @property
    def sample_rate_hz(self) -> int:
        return RATE_TABLE.get(self.rate_code, 0)

    def to_stream_bytes(self, bits: int | None = None) -> bytes:
        """打包成连续 I/Q 字节（8 位有符号交错，或 10 位紧密打包）。"""
        bits = bits or self.bits
        if bits == 16:
            out = bytearray()
            for i, q in zip(self.samples_i, self.samples_q):
                out += int(i & 0xFF).to_bytes(1, "little", signed=False)
                out += int(q & 0xFF).to_bytes(1, "little", signed=False)
            return bytes(out)
        if bits == 20:
            acc = 0
            bitpos = 0
            out = bytearray()
            for value in (v & 0x3FF for pair in zip(self.samples_i, self.samples_q) for v in pair):
                acc |= value << bitpos
                bitpos += 10
                while bitpos >= 8:
                    out.append(acc & 0xFF)
                    acc >>= 8
                    bitpos -= 8
            if bitpos:
                out.append(acc & 0xFF)
            return bytes(out)
        raise ValueError(f"不支持的采样位宽：{bits}")

    def save(self, path: str) -> str:
        with open(path, "wb") as handle:
            handle.write(self.to_stream_bytes())
        return path


def _unpack_cap20(payload: bytes, samples: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    total = samples * 2
    values: list[int] = []
    acc = 0
    bitpos = 0
    index = 0
    while len(values) < total:
        while bitpos < 10:
            if index >= len(payload):
                raise SerialLinkError("CAP20 载荷不足")
            acc |= payload[index] << bitpos
            index += 1
            bitpos += 8
        raw = acc & 0x3FF
        acc >>= 10
        bitpos -= 10
        values.append(raw - 1024 if raw >= 512 else raw)
    return tuple(values[0::2]), tuple(values[1::2])


class EspSdrLink:
    """esp-sdr 固件连接。用法::

        with EspSdrLink("COM5") as link:
            for snap in link.scan(ScanRequest(2_442_000_000, 80_000_000, 200)):
                print(snap.summary())
    """

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUD,
        timeout: float = 5.0,
        factory: Callable[..., Any] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.port_name = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._factory = factory
        self._log = logger or (lambda _m: None)
        self._port: Any = None
        self.identity = ""
        self.caps: list[str] = []
        self._sweep_rate_code = 2  #: 自扫用的采样率码（start_sweep 时更新）
        self.spec_caps: dict[str, Any] = {}
        self.limits: dict[str, Any] = {}
        self.transport = ""
        self.failed = False
        self.stats_records: list[dict[str, Any]] = []
        self._last_scan: dict[str, Any] = {}

    # ---- 连接管理 --------------------------------------------------------
    def open(self) -> "EspSdrLink":
        if self._port is not None:
            return self
        factory = self._factory
        if factory is None:
            try:
                import serial  # type: ignore
            except ModuleNotFoundError as exc:  # pragma: no cover - 环境相关
                raise SerialLinkError("缺少 pyserial：pip install pyserial") from exc
            factory = serial.Serial
        try:
            self._port = factory(self.port_name, self.baudrate, timeout=self.timeout)
        except TypeError:
            self._port = factory(self.port_name, self.baudrate)
            if hasattr(self._port, "timeout"):
                self._port.timeout = self.timeout
        self._synchronize()
        self.identity = self.command("INFO")
        self.caps = self.command("CAPS").split()
        if hasattr(self._port, "timeout"):
            self._port.timeout = self.timeout
        if "SPECCAPS" in self.caps:
            reply = self.command("SPECINFO?")
            if not reply.startswith("SPECINFO "):
                raise SerialLinkError(f"SPECINFO? 回复异常：{reply!r}")
            import json

            self.spec_caps = json.loads(reply[len("SPECINFO ") :])
        limits = self.command("LIMITS?")
        if limits.startswith("LIMITS "):
            import json

            try:
                self.limits = json.loads(limits[len("LIMITS ") :])
            except ValueError:
                self.limits = {}
        transport = self.command("TRANSPORT?")
        self.transport = transport.split()[1] if transport.startswith("TRANSPORT ") else ""
        return self

    def close(self) -> None:
        if self._port is None:
            return
        try:
            self.command("RELEASE", strict=False)
        except Exception:  # noqa: BLE001 - 关闭阶段不掩盖原始错误
            pass
        try:
            self._port.close()
        finally:
            self._port = None

    def __enter__(self) -> "EspSdrLink":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 底层读写 --------------------------------------------------------
    def _write(self, data: bytes) -> None:
        if self._port is None:
            raise SerialLinkError("连接未打开")
        self._port.write(data)

    def _readline(self) -> str:
        if self._port is None:
            raise SerialLinkError("连接未打开")
        raw = self._port.readline()
        if not raw:
            raise SerialLinkError("串口读取超时（无数据）")
        return raw.decode("ascii", errors="replace").strip()

    def _read_exact(self, count: int, deadline_s: float = 5.0) -> bytes:
        if self._port is None:
            raise SerialLinkError("连接未打开")
        chunks = bytearray()
        deadline = time.monotonic() + deadline_s
        while len(chunks) < count:
            chunk = self._port.read(count - len(chunks))
            if chunk:
                chunks.extend(chunk)
                continue
            if time.monotonic() > deadline:
                raise SerialLinkError(f"二进制读取超时：{len(chunks)}/{count} 字节")
        return bytes(chunks)

    def _synchronize(self) -> None:
        """发 SYNC 标记并丢弃其前的所有输出（打开端口可能复位板子）。"""
        deadline = time.monotonic() + SYNC_TIMEOUT_S
        while time.monotonic() < deadline:
            marker = f"SYNC {time.time_ns()}".encode("ascii")
            self._write(b"\nRELEASE\n" + marker + b"\n")
            local_deadline = time.monotonic() + 1.0
            while time.monotonic() < local_deadline:
                try:
                    if self._readline().encode("ascii") == marker:
                        return
                except SerialLinkError:
                    break
        raise SerialLinkError("无法取得串口租约（SYNC 未回显）")

    def command(self, line: str, strict: bool = True) -> str:
        self._write((line + "\n").encode("ascii"))
        reply = self._readline()
        if strict and reply.startswith("ERR"):
            raise SerialLinkError(f"{line} -> {reply}")
        return reply

    # ---- 接收机设置 ------------------------------------------------------
    def set_frequency_hz(self, freq_hz: int) -> int:
        mhz = int(round(freq_hz / 1e6))
        if not 100 <= mhz <= 6000:
            raise SerialLinkError(f"频点超出固件范围：{mhz} MHz")
        self.command(f"FREQ {mhz}", strict=False)
        return mhz

    def set_bandwidth_mhz(self, bandwidth_mhz: float) -> None:
        self.command(f"BANDWIDTH {bandwidth_mhz:g}", strict=False)

    def set_gain(self, mode: str = "HARDWARE", index: int | None = None) -> None:
        line = f"GAIN MANUAL {index}" if mode.upper() == "MANUAL" and index is not None else "GAIN HARDWARE"
        self.command(line, strict=False)

    def release(self) -> None:
        self.command("RELEASE", strict=False)

    # ---- 频谱剖面 --------------------------------------------------------
    @property
    def family(self) -> str:
        return self.identity.split()[0] if self.identity else ""

    def profiles(self) -> list[tuple[int, int, int, int, int, bool]]:
        """[sample_rate_hz, rate_code, fft_bins, stride, units_per_frame, continuous]"""
        out = []
        for entry in self.spec_caps.get("profiles", []):
            if len(entry) < 5:
                continue
            fs, code, bins, stride, units = entry[:5]
            continuous = bool(entry[5]) if len(entry) > 5 else bool(self.spec_caps.get("continuous"))
            out.append((int(fs), int(code), int(bins), int(stride), int(units), continuous))
        return out

    def choose_profile(self, bw_hz: int, bins_hint: int = 512) -> tuple[int, int, int, int, int, bool]:
        """按需求带宽/点数挑选最接近的固件剖面。"""
        profiles = self.profiles()
        if not profiles:
            raise SerialLinkError("固件未通告频谱能力（CAPS 无 SPECCAPS），无法扫描")
        snapshot_ok = [p for p in profiles if not p[5]] or profiles

        def score(p: tuple[int, int, int, int, int, bool]) -> tuple[float, float]:
            return (abs(p[0] - bw_hz), abs(p[2] - bins_hint))

        chosen = min(snapshot_ok, key=score)
        if chosen[2] < 256 or (chosen[2] & (chosen[2] - 1)):
            raise SerialLinkError(f"固件剖面点数异常：{chosen}")
        return chosen

    # ---- 频谱扫描 --------------------------------------------------------
    def scan(self, request: ScanRequest) -> Iterator[Snapshot]:
        """按请求驻留扫一个频点，逐帧产出 Snapshot（一次请求 = 一段驻留）。"""
        if self.failed:
            raise SerialLinkError("连接已标记失效，请重新打开")
        fs, rate_code, bins, stride, units, _continuous = self.choose_profile(
            request.bw_hz, request.bins
        )
        if stride != 1:
            raise SerialLinkError(f"固件的便携快照 stride 必须为 1，实际 {stride}")
        mhz = self.set_frequency_hz(request.freq_hz)
        header = self.command(
            f"SPEC {request.dwell_ms} {stride} {units} {request.detector} {rate_code} {bins}"
        ).split()
        if len(header) != 5 or header[0] != "SPEC":
            raise SerialLinkError(f"SPEC 启动回复异常：{header}")
        header_bins, header_fs = int(header[1]), int(header[2])
        if header_bins != bins or (fs and header_fs != fs):
            raise SerialLinkError(f"SPEC 剖面与请求不符：{header}")
        self._last_scan = {
            "freq_hz": mhz * 1_000_000,
            "sample_rate_hz": header_fs,
            "bins": header_bins,
            "units": int(header[3]),
            "detector": request.detector,
            "dwell_ms": request.dwell_ms,
        }
        frames = 0
        while True:
            magic = self._read_exact(4)
            if magic == END_MAGIC:
                report = (magic + self._readline().encode("ascii")).decode("ascii").split()
                if len(report) != 13 or report[0] != "SPECEND":
                    raise SerialLinkError(f"SPECEND 记录异常：{report}")
                status = int(report[1])
                self._last_scan.update(
                    {
                        "frames": frames,
                        "status": status,
                        "ffts": int(report[3]),
                        "pairs": int(report[4]),
                        "elapsed_us": int(report[5]),
                        "frames": int(report[8]),
                        "drops": int(report[9]),
                        "stopped_by_host": bool(int(report[12])),
                    }
                )
                if status != 0:
                    raise SerialLinkError(f"SPEC 以状态 {status} 结束：{report}")
                if frames == 0:
                    raise SerialLinkError("SPEC 未产出任何帧")
                return
            if magic == STATS_MAGIC:
                self.stats_records.append(decode_stats(magic + self._read_exact(STATS_BYTES - 4)))
                self.stats_records = self.stats_records[-64:]
                continue
            if magic != SPECTRUM_MAGIC:
                self.failed = True
                raise SerialLinkError(f"帧同步丢失：{magic!r}（需重新连接）")
            payload = self._read_exact(header_bins + FRAME_HEADER_BYTES + FRAME_TRAILER_BYTES - 4)
            try:
                frame: SpectrumFrame = decode_spectrum_frame(magic + payload)
            except SnapshotError as exc:
                self.failed = True
                raise SerialLinkError(f"频谱帧解析失败：{exc}") from exc
            frames += 1
            yield frame.to_snapshot(
                ts=time.time(),
                center_hz=mhz * 1_000_000,
                bw_hz=header_fs,
                extra={"dwell_ms": request.dwell_ms, "transport": self.transport},
            )

    # ---- 原始 I/Q 抓取（任务 5 用） --------------------------------------
    def capture_iq(
        self,
        samples: int,
        rate_code: int = 4,
        bits: int = 16,
        freq_hz: int | None = None,
    ) -> IqCapture:
        """抓取一段突发 I/Q。``CAP16``=8 位，``CAP20``=10 位紧密打包。

        固件传输的是 ``(I, -Q)``，这里已经按文档把 Q 取反还原成标准 ``(I, Q)``。
        """
        if samples <= 0:
            raise ValueError("samples 必须为正")
        if bits not in (16, 20):
            raise ValueError("bits 只能是 16 或 20")
        if rate_code not in RATE_TABLE:
            raise ValueError(f"未知采样率码：{rate_code}")
        if freq_hz is not None:
            self.set_frequency_hz(freq_hz)
        tag = "CAP16" if bits == 16 else "CAP20"
        reply = self.command(f"{tag} {samples} {rate_code}").split()
        if len(reply) != 4 or reply[0] != "DATA":
            raise SerialLinkError(f"{tag} 回复异常：{reply}")
        got, crc_hex, duration_us = int(reply[1]), reply[2], int(reply[3])
        if got != samples:
            raise SerialLinkError(f"{tag} 样本数不符：{got} != {samples}")
        # CAP16/CAP20 的位宽是「一个 (I,Q) 对」的总位数（8+8 / 10+10）
        payload_bytes = (samples * bits + 7) // 8
        payload = self._read_exact(payload_bytes)
        if (zlib.crc32(payload) & 0xFFFFFFFF) != int(crc_hex, 16):
            raise SerialLinkError("I/Q 载荷 CRC32 不匹配")
        if bits == 16:
            raw_i = payload[0::2]
            raw_q = payload[1::2]
            samples_i = tuple(v - 256 if v > 127 else v for v in raw_i)
            neg_q = tuple(v - 256 if v > 127 else v for v in raw_q)
        else:
            samples_i, neg_q = _unpack_cap20(payload, samples)
        samples_q = tuple(-v for v in neg_q)
        return IqCapture(
            samples_i=samples_i,
            samples_q=samples_q,
            bits=bits,
            rate_code=rate_code,
            duration_us=duration_us,
            freq_hz=(freq_hz or 0) // 1_000_000 * 1_000_000,
        )

    # ---- 设备端自扫（FSM）------------------------------------------------
    # 方案 A：调度搬进设备、判据留在主机。这里只包协议，不含任何判据。
    # 帧格式与 ``scan()`` 完全相同（一行 SPEC 头 + 一帧 SPC1），所以解析器共用。
    @property
    def sweep_supported(self) -> bool:
        """设备会不会自扫（``CAPS`` 里带 ``FSM``）。假固件与老固件没有。"""
        return "FSM" in self.caps

    def set_sweep_plan(self, windows: list[tuple[int, int, int]]) -> int:
        """下发扫频计划：``[(freq_mhz, dwell_ms, module_index), ...]``。返回窗口数。"""
        if not windows:
            raise SerialLinkError("扫频计划不能为空")
        self.command("FSMPLAN CLR")
        for mhz, dwell_ms, module in windows:
            self.command(f"FSMPLAN ADD {int(mhz)} {int(dwell_ms)} {int(module) & 0xFF}")
        reply = self.command("FSMPLAN?").split()
        if len(reply) != 2 or reply[0] != "FSMPLAN" or int(reply[1]) != len(windows):
            raise SerialLinkError(f"FSMPLAN 回读不符：{reply} != {len(windows)}")
        count = int(reply[1])
        for _ in range(count):  # W 行必须读干净，否则会混进下一帧的同步里
            self._readline()
        if self._readline() != "FSMPLANEND":
            raise SerialLinkError("FSMPLANEND 缺失")
        return count

    def start_sweep(
        self, bins: int, rate_code: int, detector: int = 0, repeats: int = 0
    ) -> str:
        """开始自扫。``repeats=0`` = 一直转。返回 ``FSM START n bins rate det``。"""
        self._sweep_rate_code = int(rate_code)
        reply = self.command(f"FSM START {int(bins)} {int(rate_code)} {int(detector)} {int(repeats)}")
        if not reply.startswith("FSM START"):
            raise SerialLinkError(f"FSM START 回复异常：{reply!r}")
        return reply

    def stop_sweep(self, drain_s: float = 5.0) -> str:
        """停止自扫并**排空在途帧**直到 ``FSMEND``（设备不会因为主机不读就丢帧）。"""
        if self._port is None:
            raise SerialLinkError("连接未打开")
        self._write(b"FSM STOP\n")
        deadline = time.monotonic() + drain_s
        while time.monotonic() < deadline:
            try:
                magic = self._read_exact(4, deadline_s=max(0.05, deadline - time.monotonic()))
            except SerialLinkError:
                if self.failed:
                    raise
                continue
            if magic == SPECTRUM_MAGIC:
                # 头的第 26 字节是 log2(bins)；吃掉 magic 之后偏移 -4
                head = self._read_exact(FRAME_HEADER_BYTES - 4, deadline_s=drain_s)
                bins = 1 << head[22]
                self._read_exact(bins + FRAME_TRAILER_BYTES, deadline_s=drain_s)
                continue
            text = (magic + self._readline_tail()).decode("ascii", "replace").strip()
            if text.startswith("FSMEND"):
                return text
        raise SerialLinkError("FSM STOP 未收到 FSMEND（在途帧没排空）")

    def _readline_tail(self) -> bytes:
        """读完当前行剩下的字节（前面已经吃了 4 个）。"""
        if self._port is None:
            raise SerialLinkError("连接未打开")
        return self._port.readline()

    def read_sweep_frame(self, timeout_s: float = 2.0) -> Snapshot | None:
        """读**一帧**自扫输出；没数据返回 None。

        设备每窗先发一行 ``SPEC <bins> <fs> <bins> <mhz>`` 再发一帧 ``SPC1``，
        这些都是 ``scan()`` 里那两种字节，所以解析器共用。
        """
        if self._port is None:
            raise SerialLinkError("连接未打开")
        deadline = time.monotonic() + max(0.05, timeout_s)
        freq_mhz = 0
        fs_hz = 0
        while time.monotonic() < deadline:
            try:
                magic = self._read_exact(4, deadline_s=max(0.05, deadline - time.monotonic()))
            except SerialLinkError:
                if self.failed:
                    raise
                return None
            if magic == b"SPEC":
                # 头行格式与 SPEC 命令的启动回复相同：
                # ``SPEC <fft_bins> <sample_rate_hz> <unit_pairs> <MHz>``
                parts = (magic + self._readline_tail()).decode("ascii", "replace").split()
                if len(parts) >= 5:
                    freq_mhz = int(parts[4])
                    fs_hz = int(parts[2])
                continue
            if magic != SPECTRUM_MAGIC:
                text = (magic + self._readline_tail()).decode("ascii", "replace").strip()
                if text.startswith("FSMEND"):
                    return None
                continue
            # SPC1 头里自带 log2(bins)，所以即使头行丢了也还能解帧
            head = self._read_exact(FRAME_HEADER_BYTES - 4, deadline_s=max(0.2, deadline - time.monotonic()))
            bins = 1 << head[22]
            rest = self._read_exact(bins + FRAME_TRAILER_BYTES, deadline_s=max(0.2, deadline - time.monotonic()))
            try:
                frame: SpectrumFrame = decode_spectrum_frame(magic + head + rest)
            except SnapshotError as exc:
                self.failed = True
                raise SerialLinkError(f"频谱帧解析失败：{exc}") from exc
            self._last_scan = {
                "freq_hz": freq_mhz * 1_000_000,
                "bins": bins,
                "mode": "sweep",
            }
            return frame.to_snapshot(
                ts=time.time(),
                center_hz=freq_mhz * 1_000_000,
                bw_hz=fs_hz or RATE_TABLE.get(self._sweep_rate_code, 0) or 20_000_000,
                extra={"transport": self.transport, "engine": "device"},
            )
        return None

    # ---- 诊断 ------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "port": self.port_name,
            "baudrate": self.baudrate,
            "identity": self.identity,
            "family": self.family,
            "transport": self.transport,
            "caps": list(self.caps),
            "profiles": self.profiles(),
            "failed": self.failed,
            "last_scan": dict(self._last_scan),
        }


__all__ = [
    "DEFAULT_BAUD",
    "EspSdrLink",
    "IqCapture",
    "RATE_TABLE",
    "SerialLinkError",
]