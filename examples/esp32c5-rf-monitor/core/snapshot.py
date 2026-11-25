"""统一数据 schema：Snapshot（频点/带宽/功率谱）+ 固件频谱帧解析。

事实源：仓库 ``docs/spectrum.md``（帧格式）与 ``docs/protocol.md``（命令集）。

约定（与本项目其它模块共享，任何模块不得自造数据格式）：

* ``Snapshot.bins_db`` 是按**频率升序**排列的功率码，长度 = FFT 点数。
  每个码是 ``20*log10(Q15 域功率)`` 的四舍五入值，0..255 饱和。
  它不是 dBm，也没有经过绝对增益校准；跨设备/跨增益只用于相对比较。
* 固件 ``SPC1`` 帧里 bin 0 是 DC（即中心频点），负频率在数组后半段。
  本模块在 ``SpectrumFrame.to_snapshot()`` 里做一次 fftshift，把数组转成
  ``[center - bw/2, center + bw/2)`` 的升序排列，下游统一按升序处理。
"""
from __future__ import annotations

import math
import statistics
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping

SPECTRUM_MAGIC = b"SPC1"
STATS_MAGIC = b"SPS1"
END_MAGIC = b"SPEC"
FRAME_HEADER_BYTES = 28
FRAME_TRAILER_BYTES = 4
STATS_BYTES = 40


class SnapshotError(ValueError):
    """快照/帧格式错误。"""


def _u16(buf: bytes, at: int) -> int:
    return int.from_bytes(buf[at : at + 2], "little")


def _u32(buf: bytes, at: int) -> int:
    return int.from_bytes(buf[at : at + 4], "little")


def _u64(buf: bytes, at: int) -> int:
    return int.from_bytes(buf[at : at + 8], "little")


@dataclass(frozen=True)
class SpectrumFrame:
    """一帧 ``SPC1``：header + bins + CRC32 已经校验过。"""

    seq: int
    sample_index: int
    samples: int
    ffts: int
    flags: int
    gain_code: int
    code_multiplier: int
    codes: tuple[int, ...]

    @property
    def max_detector(self) -> bool:
        return bool(self.flags & 1)

    @property
    def snapshot_gaps(self) -> bool:
        """快照模式恒为 True：两次抓取之间有接收空档（物理边界，不美化）。"""
        return bool(self.flags & 8)

    @property
    def bin_count(self) -> int:
        return len(self.codes)

    def to_snapshot(
        self,
        ts: float,
        center_hz: int,
        bw_hz: int,
        module: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> "Snapshot":
        """把帧转成升序频点的 Snapshot（DC 居中 → fftshift）。"""
        n = len(self.codes)
        half = n // 2
        shifted = self.codes[half:] + self.codes[:half]
        meta: dict[str, Any] = {
            "seq": self.seq,
            "sample_index": self.sample_index,
            "samples": self.samples,
            "ffts": self.ffts,
            "gain_code": self.gain_code,
            "code_multiplier": self.code_multiplier,
            "detector": "max" if self.max_detector else "mean",
            "snapshot_gaps": self.snapshot_gaps,
        }
        if module:
            meta["module"] = module
        if extra:
            meta.update(extra)
        return Snapshot(
            ts=ts,
            center_hz=center_hz,
            bw_hz=bw_hz,
            bins_db=shifted,
            meta=meta,
        )


def decode_spectrum_frame(data: bytes) -> SpectrumFrame:
    """解析并校验一整帧 ``SPC1``（含 magic 与 CRC）。"""
    if len(data) < FRAME_HEADER_BYTES + FRAME_TRAILER_BYTES:
        raise SnapshotError(f"帧太短：{len(data)} 字节")
    if data[:4] != SPECTRUM_MAGIC:
        raise SnapshotError(f"magic 不匹配：{data[:4]!r}")
    log2_bins = data[26]
    bins = 1 << log2_bins
    if bins < 256 or bins > 8192:
        raise SnapshotError(f"不支持的 FFT 点数：{bins}")
    if len(data) != FRAME_HEADER_BYTES + bins + FRAME_TRAILER_BYTES:
        raise SnapshotError(f"帧长度不符：{len(data)} != {FRAME_HEADER_BYTES + bins + FRAME_TRAILER_BYTES}")
    crc_expected = int.from_bytes(data[-4:], "little")
    crc_actual = zlib.crc32(data[:-4]) & 0xFFFFFFFF
    if crc_expected != crc_actual:
        raise SnapshotError(f"CRC32 不匹配：{crc_expected:#010x} != {crc_actual:#010x}")
    return SpectrumFrame(
        seq=_u32(data, 4),
        sample_index=_u64(data, 8),
        samples=_u32(data, 16),
        ffts=_u16(data, 20),
        flags=data[22],
        gain_code=data[23],
        code_multiplier=data[27] or 1,
        codes=tuple(data[FRAME_HEADER_BYTES : FRAME_HEADER_BYTES + bins]),
    )


def decode_stats(data: bytes) -> dict[str, Any]:
    """解析 ``SPS1`` 统计记录（40 字节，含 CRC）。仅用于日志/诊断。"""
    if len(data) != STATS_BYTES or data[:4] != STATS_MAGIC:
        raise SnapshotError("统计记录格式错误")
    if (zlib.crc32(data[:-4]) & 0xFFFFFFFF) != int.from_bytes(data[-4:], "little"):
        raise SnapshotError("统计记录 CRC 不匹配")
    return {
        "core0_load_permille": _u16(data, 4),
        "core1_load_permille": _u16(data, 6),
        "fft_coverage_permille": _u16(data, 8),
        "mode_flags": _u16(data, 10),
        "free_heap": _u32(data, 12),
        "largest_block": _u32(data, 16),
        "abandoned": _u32(data, 20),
        "dropped": _u32(data, 24),
        "late_max": _u16(data, 28),
        "queue_permille": _u16(data, 30),
        "ffts_per_s": _u32(data, 32),
    }


def quantiles(values: "tuple[float, ...] | list[float]") -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)

    def at(frac: float) -> float:
        return ordered[min(n - 1, max(0, int(round(frac * (n - 1)))))]

    return {
        "min": ordered[0],
        "p05": at(0.05),
        "median": ordered[n // 2],
        "p95": at(0.95),
        "max": ordered[-1],
    }


@dataclass(frozen=True)
class Snapshot:
    """一次频谱快照：时间 + 中心频点 + 瞬时带宽 + 升序功率码。

    契约（core/snapshot.py 定义，全项目共用）：

    * ``ts``        采集时刻（Unix 秒，浮点）
    * ``center_hz`` 中心频点（Hz）
    * ``bw_hz``     瞬时带宽（Hz，即固件采样率，也是 bins 覆盖范围）
    * ``bins_db``   升序功率码，``bins_db[i]`` 覆盖
                    ``[center - bw/2 + i*bw/n, center - bw/2 + (i+1)*bw/n)``
    * ``meta``      帧元数据（序号、探测器、增益码、来源模块等）
    """

    ts: float
    center_hz: int
    bw_hz: int
    bins_db: tuple[float, ...]
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.bw_hz <= 0:
            raise SnapshotError("bw_hz 必须为正")
        if not self.bins_db:
            raise SnapshotError("bins_db 不能为空")
        if len(self.bins_db) not in (256, 512, 1024, 2048, 4096, 8192):
            raise SnapshotError(f"bins_db 长度异常：{len(self.bins_db)}")

    # ---- 派生量 ----------------------------------------------------------
    @property
    def ts_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts))

    @property
    def bin_count(self) -> int:
        return len(self.bins_db)

    @property
    def bin_width_hz(self) -> float:
        return self.bw_hz / self.bin_count

    def bin_freq_hz(self, index: int) -> float:
        return self.center_hz - self.bw_hz / 2 + (index + 0.5) * self.bin_width_hz

    def bin_index(self, freq_hz: float) -> int:
        idx = int((freq_hz - self.center_hz + self.bw_hz / 2) // self.bin_width_hz)
        return min(self.bin_count - 1, max(0, idx))

    def band_indices(self, f0_hz: float, f1_hz: float) -> tuple[int, int]:
        """[f0, f1) 覆盖的 bin 下标区间（按 bin 中心归属，闭区间）。"""
        if f1_hz < f0_hz:
            f0_hz, f1_hz = f1_hz, f0_hz
        start = self.center_hz - self.bw_hz / 2
        width = self.bin_width_hz
        a = math.ceil((f0_hz - start) / width - 0.5)
        b = math.floor((f1_hz - start) / width - 0.5)
        a = min(self.bin_count - 1, max(0, a))
        b = min(self.bin_count - 1, max(0, b))
        return a, b

    def slice_db(self, f0_hz: float, f1_hz: float) -> tuple[float, ...]:
        """取出 [f0, f1) 内的 bin（按 bin 中心归属，越界自动截断）。"""
        a, b = self.band_indices(f0_hz, f1_hz)
        return self.bins_db[a : b + 1] if b >= a else ()

    @property
    def mean_db(self) -> float:
        return sum(self.bins_db) / self.bin_count

    @property
    def noise_floor_db(self) -> float:
        """低分位噪声底（用 p05 抗突发污染）。"""
        ordered = sorted(self.bins_db)
        return ordered[max(0, int(len(ordered) * 0.05))]

    def peak(self) -> tuple[float, float]:
        """(频点 Hz, 功率码)。"""
        best = max(range(self.bin_count), key=lambda i: self.bins_db[i])
        return self.bin_freq_hz(best), self.bins_db[best]

    def occupancy(self, threshold_db: float) -> float:
        """全带宽占用率：功率码 ≥ threshold 的 bin 占比（0..1）。"""
        hit = sum(1 for v in self.bins_db if v >= threshold_db)
        return hit / self.bin_count

    def band_occupancy(self, f0_hz: float, f1_hz: float, threshold_db: float) -> float:
        band = self.slice_db(f0_hz, f1_hz)
        return 0.0 if not band else sum(1 for v in band if v >= threshold_db) / len(band)

    def band_mean_db(self, f0_hz: float, f1_hz: float) -> float:
        band = self.slice_db(f0_hz, f1_hz)
        return 0.0 if not band else sum(band) / len(band)

    def band_peak_db(self, f0_hz: float, f1_hz: float) -> float:
        band = self.slice_db(f0_hz, f1_hz)
        return 0.0 if not band else max(band)

    def stats(self) -> dict[str, float]:
        base = quantiles(list(self.bins_db))
        peak_hz, peak_db = self.peak()
        return {
            **base,
            "mean": self.mean_db,
            "noise_floor": self.noise_floor_db,
            "peak_freq_hz": peak_hz,
            "peak_db": peak_db,
            "stdev": statistics.pstdev(self.bins_db),
        }

    def summary(self) -> str:
        peak_hz, peak_db = self.peak()
        return (
            f"{self.ts_iso} {self.center_hz/1e6:.1f}MHz ±{self.bw_hz/2e6:.1f}MHz "
            f"bins={self.bin_count} floor={self.noise_floor_db:.0f} "
            f"peak={peak_db:.0f}@{peak_hz/1e6:.2f}MHz"
        )


__all__ = [
    "END_MAGIC",
    "FRAME_HEADER_BYTES",
    "FRAME_TRAILER_BYTES",
    "SPECTRUM_MAGIC",
    "STATS_BYTES",
    "STATS_MAGIC",
    "Snapshot",
    "SnapshotError",
    "SpectrumFrame",
    "decode_spectrum_frame",
    "decode_stats",
    "quantiles",
]