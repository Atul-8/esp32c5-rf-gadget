"""启发式信号分类器（仅任务 5「可疑信号抓拍」需要调用）。

算法由 feat/5-capture 实现（任务书授权本阶段动 core 的只有这一处）。
边界（写进输出注释，不许美化）：这是**启发式**分类，只用带宽 / 持续
时间 / 占空比三个特征，不做调制识别、不做设备级归因。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

#: 已知类别。``unknown`` 是任务 5 的抓拍触发条件。
KNOWN_LABELS = (
    "wifi",
    "ble",
    "microwave",
    "analog_video",
    "unknown",
)


@dataclass(frozen=True)
class SignalFeatures:
    """一个候选发射源的三个（可选再加两个）启发式特征。"""

    bw_hz: float
    duration_s: float
    duty_cycle: float
    peak_over_noise_db: float = 0.0
    hop_rate_hz: float | None = None
    centre_hz: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "bw_hz": self.bw_hz,
            "duration_s": self.duration_s,
            "duty_cycle": self.duty_cycle,
            "peak_over_noise_db": self.peak_over_noise_db,
            "hop_rate_hz": self.hop_rate_hz,
            "centre_hz": self.centre_hz,
        }


@dataclass(frozen=True)
class Classification:
    label: str
    confidence: float
    reason: str
    notes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.label not in KNOWN_LABELS:
            raise ValueError(f"未知类别：{self.label}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence 必须在 0..1")


#: 门槛全部集中在这里，改判据只改这一处（模块侧不许各写一份）。
#: 顺序即优先级：**更具体的特征先判**，判不动才落 ``unknown``。
AMBIGUOUS_MIN_BW_HZ = 15_000_000  #: 铺满整窗的抬升：微波炉/宽带干扰与满负荷 WiFi 分不开
AMBIGUOUS_MIN_DUTY = 0.70
VIDEO_BW_RANGE_HZ = (4_000_000, 8_000_000)
VIDEO_MIN_DUTY = 0.75
VIDEO_MIN_DURATION_S = 5.0
WIFI_MIN_BW_HZ = 8_000_000  #: 20 MHz 信道被部分覆盖时也会落到 8–15 MHz
WIFI_MIN_DUTY = 0.50
BLE_BW_RANGE_HZ = (1_000_000, 2_500_000)
BLE_MAX_DUTY = 0.60
CW_MAX_BW_HZ = 500_000
HOP_MAX_DUTY = 0.60


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def classify(features: SignalFeatures) -> Classification:
    """启发式分类入口（只用带宽 / 持续时间 / 占空比，不做调制识别）。

    判定顺序即优先级：**先判「像不像已知的」再判「是不是可疑」**，
    已知的都分不出去时落 ``unknown`` —— 而 ``unknown`` 正是任务 5 抓拍的触发条件。

    +--------------------------------+------------------+
    | 特征（按此顺序判，先具体后笼统）| 判读              |
    +================================+==================+
    | 带宽 ≥ 15 MHz 且占空 ≥ 70%      | ``unknown``（分不清）|
    | 带宽 4–8 MHz、占空 ≥ 75%、≥5 s  | ``analog_video``  |
    | 带宽 ≥ 8 MHz 且占空 ≥ 50%       | ``wifi``          |
    | 带宽 1–2.5 MHz 且占空 ≤ 60%     | ``ble``           |
    | 带宽 ≤ 500 kHz                  | ``unknown``（CW）  |
    | 其余（含跳频样/间歇）           | ``unknown``       |
    +--------------------------------+------------------+

    ``microwave`` 只在「宽带 + 高占空」这条分支上产出，且置信度固定压到 0.45、判词里
    明说「和满负荷 WiFi 分不清」：只敢给大类，不敢给设备（详见 ``docs/05-capture.md`` §3.1）。
    """
    bw_hz = float(features.bw_hz)
    if bw_hz < 0:
        raise ValueError(f"带宽不能为负：{bw_hz}")
    duty = _clamp(float(features.duty_cycle))
    duration_s = max(0.0, float(features.duration_s))
    peak = float(features.peak_over_noise_db)
    notes = {
        "bw_hz": f"{bw_hz/1e6:.2f} MHz",
        "duty": f"{duty:.0%}",
        "duration_s": f"{duration_s:.1f}",
        "peak_db": f"{peak:.1f}",
        "kind": "启发式（带宽/持续/占空比），不做调制识别",
    }

    # 1) 铺满整窗的连续抬升 → 微波炉/宽带干扰。置信度压低、判词写明「分不清」：
    #    这段特征和「满负荷的 WiFi 信道」完全重合，所以只敢给大类、不敢给具体设备；
    #    它是**已知大类**，因此不触发抓拍（任务 5 的验收要求：已知信号不抓）。
    if bw_hz >= AMBIGUOUS_MIN_BW_HZ and duty >= AMBIGUOUS_MIN_DUTY:
        return Classification(
            label="microwave",
            confidence=0.45,
            reason=(
                f"带宽 {bw_hz/1e6:.1f} MHz、占空 {duty:.0%} → 像微波炉/宽带干扰；"
                "但和满负荷 WiFi 信道在带宽与占空比上完全重合，分不清是哪一个"
            ),
            notes=dict(notes, ambiguous="wideband_continuous"),
        )
    # 2) 4–8 MHz 长时间持续 → 模拟图传（比「宽带 WiFi」更具体，必须先判）
    vlo, vhi = VIDEO_BW_RANGE_HZ
    if vlo <= bw_hz <= vhi and duty >= VIDEO_MIN_DUTY and duration_s >= VIDEO_MIN_DURATION_S:
        return Classification(
            label="analog_video",
            confidence=_clamp(0.40 + (duty - VIDEO_MIN_DUTY) + min(0.2, duration_s / 300)),
            reason=f"带宽 {bw_hz/1e6:.1f} MHz、占空 {duty:.0%}、已持续 {duration_s:.0f} s → 像模拟图传",
            notes=notes,
        )
    # 3) 8–15 MHz 的持续凸起 → WiFi 信道被占用（20 MHz 信道部分覆盖时也落这里）
    if bw_hz >= WIFI_MIN_BW_HZ and duty >= WIFI_MIN_DUTY:
        return Classification(
            label="wifi",
            confidence=_clamp(0.40 + (duty - WIFI_MIN_DUTY) + min(0.2, bw_hz / 100e6)),
            reason=f"带宽 {bw_hz/1e6:.1f} MHz、占空 {duty:.0%} → 像 WiFi 信道占用",
            notes=notes,
        )
    # 3) 1–2.5 MHz 的窄脉冲列 → BLE（广播/连接事件都是短脉冲）
    lo, hi = BLE_BW_RANGE_HZ
    if lo <= bw_hz <= hi and duty <= BLE_MAX_DUTY:
        return Classification(
            label="ble",
            confidence=_clamp(0.40 + (BLE_MAX_DUTY - duty) / 2),
            reason=f"带宽 {bw_hz/1e6:.2f} MHz、占空 {duty:.0%} → 像 BLE 短脉冲",
            notes=notes,
        )
    # 4) 落到这里就是「分不出去」→ unknown，也就是抓拍要抓的那一类
    if bw_hz <= CW_MAX_BW_HZ:
        reason = f"带宽仅 {bw_hz/1e3:.0f} kHz 的窄带，占空 {duty:.0%} → 不像已知的 WiFi/BLE/微波炉/图传"
    elif features.hop_rate_hz:
        reason = f"带宽 {bw_hz/1e6:.2f} MHz、占空 {duty:.0%}、跳速 {features.hop_rate_hz:.2f} Hz → 跳频样"
    elif duty <= HOP_MAX_DUTY:
        reason = f"带宽 {bw_hz/1e6:.2f} MHz、占空 {duty:.0%} → 间歇出现，不像已知类型"
    else:
        reason = f"带宽 {bw_hz/1e6:.2f} MHz、占空 {duty:.0%} → 不在任何已知类型的特征区间"
    return Classification(
        label="unknown",
        confidence=_clamp(0.50 + min(0.3, (CW_MAX_BW_HZ - min(bw_hz, CW_MAX_BW_HZ)) / 1e6 / 2)),
        reason=reason,
        notes=notes,
    )


__all__ = [
    "AMBIGUOUS_MIN_BW_HZ",
    "AMBIGUOUS_MIN_DUTY",
    "BLE_BW_RANGE_HZ",
    "BLE_MAX_DUTY",
    "CW_MAX_BW_HZ",
    "Classification",
    "KNOWN_LABELS",
    "HOP_MAX_DUTY",
    "SignalFeatures",
    "VIDEO_BW_RANGE_HZ",
    "VIDEO_MIN_DURATION_S",
    "VIDEO_MIN_DUTY",
    "WIFI_MIN_BW_HZ",
    "WIFI_MIN_DUTY",
    "classify",
]