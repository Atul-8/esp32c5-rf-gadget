"""功能 1：WiFi 信道体检（2.4G / 5G 占用评估 + 推荐信道）。

只用 ``core`` 暴露的接口：
  * ``Snapshot.slice_db`` / ``noise_floor_db`` 做分带与判决
  * ``ScanRequest`` 声明驻留计划
  * ``Output`` 交付表格

判据（可自测，见 tests/test_wifi_check.py）：
  * ``wifi_busy``  : ch1 / ch6 / ch11 里忙的信道必须被判「拥挤」，推荐信道必须落在干净信道
  * ``quiet``      : 全部信道「空闲」，推荐 ch1，干扰源计数 0（零误报）
  * ``microwave``  : 2.4G 中段（ch6..ch11）被抬升，ch1 保持干净

边界：分时快照，不是连续实时；功率码是相对 dB 码，不是 dBm。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from core.registry import ModePlugin, Output, ScanRequest
from core.snapshot import Snapshot
from output.format import ascii_table, bar, fmt_hz, fmt_pct

# ---------------------------------------------------------------------------
# 频段定义（不依赖 tests/ 里的常量，功能模块自带）
# ---------------------------------------------------------------------------

#: 2.4G 信道 → 中心频率（MHz）
CHANNELS_24 = {ch: 2412 + 5 * (ch - 1) for ch in range(1, 14)}
#: 5G 常用 20 MHz 栅格信道 → 中心频率（MHz）
CHANNELS_5 = {ch: 5000 + 5 * ch for ch in list(range(36, 65, 4)) + list(range(100, 145, 4)) + list(range(149, 166, 4))}
#: 只有这三个信道互不重叠，推荐优先从中选
NON_OVERLAP_24 = (1, 6, 11)

#: 2.4G 频段实测范围（ch1 下沿 → ch13 上沿）
BAND_24_HZ = (2_402_000_000, 2_482_000_000)
#: 5G 频段实测范围（ch36 下沿 → ch165 上沿）
BAND_5_HZ = (5_170_000_000, 5_835_000_000)

#: 单次扫描窗宽度（= 采样率）。实测（2026-11-18 COM56 实板）：
#: 80 MS/s 有效通带仅 30–35 MHz（38–44%），40 MS/s 约 29–34 MHz（72–86%），
#: 20/10 MS/s 是满带宽。取 20 MHz 窗：整窗都是可用带宽，不需要再猜边缘。
SCAN_BW_HZ = 20_000_000
#: 窗内可用半宽（20 MS/s 实测满带宽 → ±10 MHz）
USABLE_HALF_HZ = SCAN_BW_HZ // 2
#: 中心频率步进：15 MHz < 20 MHz 窗宽 → 相邻窗重叠 5 MHz，扫完不留空洞
SCAN_STEP_HZ = 15_000_000

CHANNEL_WIDTH_HZ = 20_000_000
#: 信道被实测覆盖率低于它就算「无覆盖」，不参与推荐也不计入干扰源
MIN_COVERAGE = 0.60
#: 整窗最大码仍 ≤ 它 ⇒ 这一帧没有有效测量（增益未稳 / 无覆盖），直接丢弃
DEGENERATE_MAX_CODE = 0
#: 帧内 bin 高于本帧 p10 多少算「有能量」（用于表格里的「占空」列，只作参考）
BUSY_EXCESS_DB = 10.0
#: 信道抬升分档（相对同频段最安静信道的中位电平，单位 dB）
#:
#: 为什么用「相对抬升」而不是「高于噪声底的 bin 比例」：实板实测同一帧内
#: 噪声底自身就有 ±10~18 dB 抖动（AGC 与量化台阶），任何绝对门限都会把
#: 纯噪声判成忙（实测 2.4G / 5G 全部 100% 忙）。改判「比本频段最安静的
#: 信道高多少」后，安静环境全档位归零，有 AP 的信道高出 20~40 dB。
LEVELS = ((25.0, "重度"), (15.0, "拥挤"), (8.0, "轻度"), (0.0, "空闲"))
#: 选信道建议：避开抬升最高的 3 个（任务书口径）
AVOID_TOP = 3
#: 比最优信道高不到这么多的不算「必须避开」，免得全空闲时乱排除
AVOID_MARGIN_DB = 6.0


def sliding_windows(lo_hz: int, hi_hz: int, step_hz: int = SCAN_STEP_HZ,
                    half_hz: int = USABLE_HALF_HZ) -> list[int]:
    """重叠滑扫的中心频率序列：每窗覆盖 ``±half_hz``，步进小于窗宽即重叠。"""
    first = lo_hz + half_hz
    last = hi_hz - half_hz
    if last <= first:
        return [(lo_hz + hi_hz) // 2]
    centers = []
    center = first
    while center <= last:
        centers.append(center)
        center += step_hz
    if centers[-1] < last:  # 步进除不尽时把上沿补齐
        centers.append(last)
    return centers


def level_of(excess_db: float) -> str:
    for threshold, label in LEVELS:
        if excess_db >= threshold:
            return label
    return "空闲"


@dataclass
class ChannelStat:
    """单个信道的累积统计（跨多帧）。"""

    channel: int
    center_hz: int
    band: str
    frames: int = 0
    level_sum: float = 0.0
    occupancy_sum: float = 0.0
    peak_db: float = 0.0
    covered_mhz: set[int] = field(default_factory=set)
    #: 反向引用插件，用来取「同频段最安静信道」的参考电平（不参与比较/序列化）
    owner: Any = field(default=None, repr=False, compare=False)

    def feed(self, occupancy: float, level_db: float, peak_db: float,
             covered_mhz: Iterable[int] = ()) -> None:
        self.frames += 1
        self.covered_mhz.update(covered_mhz)
        self.occupancy_sum += occupancy
        self.level_sum += level_db
        self.peak_db = max(self.peak_db, peak_db)

    @property
    def level_db(self) -> float:
        """该信道的平均中位电平（相对 dB 码）。"""
        return self.level_sum / self.frames if self.frames else 0.0

    @property
    def ref_db(self) -> float:
        """同频段最安静信道的电平（参考电平）。"""
        return self.owner.ref_db(self.band) if self.owner is not None else 0.0

    @property
    def excess_db(self) -> float:
        """比同频段最安静信道高多少 dB —— 有 AP 的信道会明显为正。"""
        return self.level_db - self.ref_db

    @property
    def occupancy(self) -> float:
        """帧内「高于本帧 p10 + 10 dB」的 bin 比例（只作参考，不参与分档）。"""
        return self.occupancy_sum / self.frames if self.frames else 0.0

    @property
    def coverage(self) -> float:
        """信道被实测覆盖的 MHz 占 20 MHz 的比例；多次重叠窗按并集算。"""
        return min(1.0, len(self.covered_mhz) / (CHANNEL_WIDTH_HZ / 1_000_000))

    @property
    def level(self) -> str:
        if self.coverage < MIN_COVERAGE:
            return "无覆盖"
        return level_of(self.excess_db)


@dataclass
class WifiCheck(ModePlugin):
    """WiFi 信道体检。"""

    id: str = "wifi_check"
    name: str = "WiFi 信道体检"
    # 射频时间份额：单射频分时，本模块与 waterfall 各占一半（40.0 会把瀑布图饿死：
    # 实测 share 0.984 vs 0.016，一轮 44 窗要 5 分钟才扫完）
    slice_weight: float = 1.0

    dwell_ms: int = 100
    bins: int = 1024
    include_5g: bool = True

    stats: dict[str, ChannelStat] = field(default_factory=dict)
    started_at: float = 0.0
    _window_key: int = 0
    _window_frames: int = 0
    last_ts: float = 0.0
    slices: int = 0
    frames: int = 0
    dropped_frames: int = 0
    _ref_cache: dict[str, float] = field(default_factory=dict, repr=False)

    # ---- 契约 -----------------------------------------------------------
    def scan_requests(self) -> list[ScanRequest]:
        """20 MHz 窗 / 15 MHz 步进的重叠滑扫计划。"""
        centers = sliding_windows(*BAND_24_HZ)
        if self.include_5g:
            centers += sliding_windows(*BAND_5_HZ)
        return [ScanRequest(hz, SCAN_BW_HZ, self.dwell_ms, self.bins) for hz in centers]

    def on_snapshot(self, snap: Snapshot) -> None:
        if not self.started_at:
            self.started_at = snap.ts
        self.last_ts = max(self.last_ts, snap.ts)

        if snap.center_hz != self._window_key:
            self._window_key = snap.center_hz
            self._window_frames = 0
        self._window_frames += 1

        # 整窗全是 0 码：增益还没稳/这段没真正收到东西，不计入统计
        if max(snap.bins_db, default=0) <= DEGENERATE_MAX_CODE:
            self.dropped_frames += 1
            return
        self.frames += 1
        self._ref_cache.clear()

        # 本帧自己的参考电平用 p10（比 noise_floor_db 的 p05 稳，抗 0 码台阶）
        ordered = sorted(snap.bins_db)
        frame_ref = ordered[int(len(ordered) * 0.10)]
        threshold = frame_ref + BUSY_EXCESS_DB
        visible_lo = snap.center_hz - USABLE_HALF_HZ
        visible_hi = snap.center_hz + USABLE_HALF_HZ
        for channel, center_mhz in self._channels():
            center = center_mhz * 1_000_000
            lo = max(center - CHANNEL_WIDTH_HZ / 2, visible_lo)
            hi = min(center + CHANNEL_WIDTH_HZ / 2, visible_hi)
            if hi <= lo:
                continue
            first, last = snap.band_indices(lo, hi)
            if last < first:
                continue
            bins = snap.bins_db[first : last + 1]
            if not bins:
                continue
            occupancy = sum(1 for value in bins if value > threshold) / len(bins)
            ordered_channel = sorted(bins)
            channel_level = ordered_channel[len(ordered_channel) // 2]  # 中位电平：抗单点尖峰
            # 只有真的收到能量（码 > 0）的格点才算"这段频谱被看到了"
            covered = {
                int(snap.bin_freq_hz(index) // 1_000_000)
                for index in range(first, last + 1)
                if snap.bins_db[index] > 0
            }
            key = f"{channel}{'g' if center_mhz > 3000 else 'c'}"
            if key not in self.stats:
                self.stats[key] = ChannelStat(
                    channel=channel, center_hz=center,
                    band="5G" if center_mhz > 3000 else "2.4G", owner=self,
                )
            stat = self.stats[key]
            stat.feed(occupancy, channel_level, max(bins), covered)

    def produce(self) -> Output:
        self.refresh()
        ranked = self._ranked()
        worst = max((stat.excess_db for stat in ranked), default=0.0) or 1.0
        rows = [
            [
                str(index + 1),
                f"ch{stat.channel}",
                stat.band,
                fmt_hz(stat.center_hz),
                bar(max(0.0, stat.excess_db), worst, 12),
                f"{stat.excess_db:+.0f} dB",
                fmt_pct(stat.occupancy),
                f"{stat.peak_db:.0f}",
                fmt_pct(stat.coverage),
                stat.level,
            ]
            for index, stat in enumerate(ranked)
        ]
        return Output(
            kind="table",
            title="WiFi 信道体检（按相对抬升降序）",
            columns=["排名", "信道", "频段", "中心", "占用条", "比最静", "占空", "峰值码", "覆盖", "分档"],
            rows=rows,
            notes=self.notes(),
            payload={"recommend_24": self.recommend_24(), "recommend_5": self.recommend_5(), "interferers": self.interferers()},
        )

    # ---- 判读 -----------------------------------------------------------
    def ref_db(self, band: str) -> float:
        """该频段「最安静信道」的平均中位电平，作为相对抬升的零点。"""
        cached = self._ref_cache.get(band)
        if cached is not None:
            return cached
        stats = [s for s in self.stats.values()
                 if s.band == band and s.frames and s.coverage >= MIN_COVERAGE]
        ref = min((stat.level_db for stat in stats), default=0.0)
        self._ref_cache[band] = ref
        return ref

    def refresh(self) -> None:
        """样本更新后清掉参考电平缓存（避免缓存与统计不一致）。"""
        self._ref_cache.clear()

    def recommend_24(self) -> int | None:
        """2.4G 推荐：优先在互不重叠的 1/6/11 里挑，且避开占用最高的 3 个。

        1/6/11 全被避开时退到其它 2.4G 信道（``fallback_24`` 置 True，notes 会说明）。
        """
        all24 = [stat for stat in self.stats.values() if stat.band == "2.4G"]
        pick = self._recommend([s for s in all24 if s.channel in NON_OVERLAP_24])
        self.fallback_24 = False
        if pick is None:
            pick = self._recommend(all24)
            self.fallback_24 = pick is not None
        return pick

    def recommend_5(self) -> int | None:
        return self._recommend([stat for stat in self.stats.values() if stat.band == "5G"])

    def avoided(self, band: str) -> list[int]:
        """该频段里「抬升明显偏高、建议避开」的信道（最多 AVOID_TOP 个）。"""
        self.refresh()
        stats = [stat for stat in self.stats.values()
                 if stat.band == band and stat.frames and stat.coverage >= MIN_COVERAGE]
        if not stats:
            return []
        best = min(stat.excess_db for stat in stats)
        top = sorted(stats, key=lambda s: (-s.excess_db, s.channel))[:AVOID_TOP]
        return [stat.channel for stat in top if stat.excess_db > best + AVOID_MARGIN_DB]

    def _recommend(self, candidates: Iterable[ChannelStat]) -> int | None:
        self.refresh()
        usable = [stat for stat in candidates if stat.coverage >= MIN_COVERAGE and stat.frames]
        if not usable:
            return None
        best = min(stat.excess_db for stat in usable)
        top = sorted(usable, key=lambda stat: (-stat.excess_db, stat.channel))[:AVOID_TOP]
        avoid = {stat.channel for stat in top if stat.excess_db > best + AVOID_MARGIN_DB}
        pool = [stat for stat in usable if stat.channel not in avoid] or usable
        # 排序键按 2 dB 分桶：噪声级抖动（<2 dB）不该改变推荐结果
        return min(pool, key=lambda stat: (round(stat.excess_db / 2.0), stat.channel)).channel

    def interferers(self) -> list[str]:
        """抬升进入「轻度」以上的信道 = 有明显信号活动的信道。"""
        self.refresh()
        return [f"ch{stat.channel}" for stat in self._ordered()
                if stat.coverage >= MIN_COVERAGE and stat.frames and stat.level != "空闲"]

    def channel_report(self) -> dict[str, Any]:
        return {
            "channels": [
                {
                    "channel": stat.channel,
                    "band": stat.band,
                    "level_db": round(stat.level_db, 1),
                    "excess_db": round(stat.excess_db, 1),
                    "occupancy": round(stat.occupancy, 4),
                    "peak_db": round(stat.peak_db, 1),
                    "coverage": round(stat.coverage, 3),
                    "level": stat.level,
                }
                for stat in self._ordered()
            ],
            "recommend_24": self.recommend_24(),
            "recommend_5": self.recommend_5(),
            "interferers": self.interferers(),
            "frames": self.frames,
            "span_s": round(self.span_s, 2),
        }

    def web_payload(self) -> dict[str, Any]:
        """给上位机（``host/web``）的结构化产出：表格由前端自己画，这里只给数。"""
        data = self.channel_report()
        data.update(
            {
                "kind": "wifi_check",
                "avoided": {"2.4G": self.avoided("2.4G"), "5G": self.avoided("5G")},
                "dropped_frames": self.dropped_frames,
                "levels": [{"min_db": threshold, "label": label} for threshold, label in LEVELS],
                "non_overlap_24": list(NON_OVERLAP_24),
                "scan_plan": {
                    "bw_hz": SCAN_BW_HZ,
                    "step_hz": SCAN_STEP_HZ,
                    "windows_24": len(sliding_windows(*BAND_24_HZ)),
                    "windows_5": len(sliding_windows(*BAND_5_HZ)) if self.include_5g else 0,
                },
            }
        )
        return data

    def notes(self) -> list[str]:
        self.refresh()
        lines = [
            "判据：信道中位电平比同频段最安静信道高 "
            f"{LEVELS[2][0]:.0f}/{LEVELS[1][0]:.0f}/{LEVELS[0][0]:.0f} dB ⇒ 轻度/拥挤/重度（实板噪声底自身抖动 ±10~18 dB，"
            "绝对门限会全判忙，所以用同频段相对抬升）。",
            f"扫描：{SCAN_BW_HZ/1e6:.0f}MHz 窗 / {SCAN_STEP_HZ/1e6:.0f}MHz 步进重叠滑扫，"
            f"整窗 0 码的帧丢弃（{self.dropped_frames} 帧）；信道覆盖 <{MIN_COVERAGE:.0%} 记「无覆盖」。",
        ]
        rec_24, rec_5 = self.recommend_24(), self.recommend_5()
        if rec_24 is not None:
            stat = self._stat(rec_24, "2.4G")
            how = "退而选占用最低的 2.4G 信道，非重叠 1/6/11 已全被避开" if self.fallback_24 else "非重叠信道"
            lines.append(f"2.4G 建议：ch{rec_24}（{how}；比最静信道 {stat.excess_db:+.0f} dB，{stat.level}）。")
            if stat.level != "空闲":
                lines.append(f"! 注意：2.4G 没有真正空闲的信道，ch{rec_24} 只是相对最轻的，"
                             "建议优先上 5G 或有线。")
        else:
            lines.append("2.4G 建议：没有可推荐信道（覆盖率不足或全部被占），建议改用 5G 或有线。")
        if rec_5 is not None:
            stat = self._stat(rec_5, "5G")
            lines.append(f"5G 建议：ch{rec_5}（比最静信道 {stat.excess_db:+.0f} dB，{stat.level}）。")
        elif self.include_5g:
            lines.append("5G 建议：本次样本里没有可推荐信道。")
        for band in ("2.4G", "5G"):
            avoid = self.avoided(band)
            if avoid:
                lines.append(f"{band} 建议避开（占用偏高）：{', '.join(f'ch{ch}' for ch in avoid)}。")
        found = self.interferers()
        lines.append(f"有明显抬升（轻度以上）的信道：{', '.join(found) if found else '无'}。")
        lines.append(f"样本：{self.frames} 帧 / 覆盖 {self.span_s:.1f}s（分时扫描，帧间有接收空档）。")
        lines.append("边界：分时快照，非连续实时；功率码是相对 dB 码，不是 dBm；"
                     "跨增益不可比，真机请固定增益扫描（app.live --gain-index N）后看相对抬升。")
        return lines

    # ---- 内部 -----------------------------------------------------------
    def _channels(self) -> Iterable[tuple[int, int]]:
        for channel, center_mhz in CHANNELS_24.items():
            yield channel, center_mhz
        if self.include_5g:
            for channel, center_mhz in CHANNELS_5.items():
                yield channel, center_mhz

    @staticmethod
    def _key(channel: int, band: str = "") -> str:
        suffix = "g" if band == "5G" or channel > 13 else "c"
        return f"{channel}{suffix}"

    def _stat(self, channel: int, band: str) -> ChannelStat | None:
        return self.stats.get(self._key(channel, band))

    def _ranked(self) -> list[ChannelStat]:
        """按相对抬升降序（任务书要求表格按占用率排序）。"""
        return sorted(self.stats.values(), key=lambda s: (-s.excess_db, s.band != "2.4G", s.center_hz))

    def _ordered(self) -> list[ChannelStat]:
        return sorted(self.stats.values(), key=lambda stat: (stat.band != "2.4G", stat.center_hz))

    @property
    def span_s(self) -> float:
        return max(0.0, self.last_ts - self.started_at) if self.started_at else 0.0

    def reset(self) -> None:
        self.stats.clear()
        self.started_at = 0.0
        self.last_ts = 0.0
        self._window_key = 0
        self._window_frames = 0
        self.slices = 0
        self.frames = 0
        self.dropped_frames = 0
        self._ref_cache.clear()

    def summary(self) -> dict[str, Any]:
        data = super().summary()
        data.update({"frames": self.frames, "recommend_24": self.recommend_24(), "interferers": self.interferers()})
        return data


# ---------------------------------------------------------------------------
# 自带自测入口：python -m modes._legacy.wifi_check --scenario wifi_busy
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="WiFi 信道体检（mock 自测，无需硬件）")
    parser.add_argument("--scenario", default="wifi_busy")
    parser.add_argument("--slices", type=int, default=28)
    parser.add_argument("--no-5g", action="store_true")
    args = parser.parse_args(argv)

    from core.scheduler import Scheduler
    from tests.mock_source import MockSource

    plugin = WifiCheck(include_5g=not args.no_5g)
    scheduler = Scheduler(MockSource(scenario=args.scenario, realtime=False), _registry(plugin))
    scheduler.run(max_slices=args.slices)
    print(ascii_table(
        ["项目", "值"],
        [["场景", args.scenario], ["时间片", scheduler.stats.slices], ["帧数", plugin.frames],
         ["推荐信道", f"2.4G ch{plugin.recommend_24() or '无干净信道'} / 5G ch{plugin.recommend_5() or '无'}"],
         ["干扰源", ", ".join(plugin.interferers()) or "无"]],
    ))
    print(plugin.produce().render())
    return 0


def _registry(plugin: ModePlugin):
    from core.registry import Registry

    registry = Registry()
    registry.register(plugin)
    return registry


if __name__ == "__main__":
    raise SystemExit(main())