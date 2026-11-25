"""功能 3 仿真佐证：把「完全安静 / 浅干扰 / 复杂噪音 / 静态形状 / 开关型宽带」五种
输入一次性跑出来，打印判决链路的全部中间量。

用法（在 examples/esp32c5-rf-monitor 下）::

    python tests/sim_ble_verdict.py            # 只跑仿真五档
    python tests/sim_ble_verdict.py --scenarios  # 再跑 tests/mock_source.py 的 8 个场景

这不是测试用例，是**给人看的证据**：每行都能对上 ``modes/ble_risk.py::BleRisk.risk_type``
里的一个分支，改判据时先跑它，看哪一行变了。
"""
from __future__ import annotations

import pathlib
import sys

# 允许直接 ``python tests/sim_ble_verdict.py``（不装包、不设 PYTHONPATH）
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modes._legacy.ble_risk import (
    CONTRAST_DB,
    LIGHT_RATIO,
    WIDEBAND_FRAC,
    BleRisk,
)
from tests.test_ble_risk import (
    _complex,
    _feed_shape,
    _flicker_hump,
    _shallow,
    _silence,
    _static_hump,
    _uniform_flicker,
)

CASES = [
    ("完全安静", _silence, 3.0, "只有底噪，什么都没有"),
    ("很浅的干扰", _shallow, 3.0, "广播信道 37 上 +6 dB 窄脉冲（未到 12 dB 门限）"),
    ("复杂噪音", _complex, 3.0, "常抬宽带 + 常亮 CW + 浅脉冲（多源混合）"),
    ("静态形状", _static_hump, 8.0, "整段高斯鼓包但时间上不动（前端起伏）"),
    ("开关型宽带", _flicker_hump, 8.0, "鼓包 + 一段 25 dB 开关（真宽带源）"),
    ("整段一起动", _uniform_flicker, 8.0, "病态输入：分不清，必须落到兜底判词"),
]

HEADER = (
    f"{'输入':12s} {'广播活动':>8s} {'浅抬升':>7s} {'宽带占用':>8s} "
    f"{'开关幅度':>8s} {'栅格':>5s} {'常亮':>4s}  判词"
)


def main(argv: list[str]) -> int:
    print(f"判据：广播活动 ≥0.10 · 浅抬升 ≥{LIGHT_RATIO:.2f} · 宽带占用 ≥{WIDEBAND_FRAC:.2f} "
          f"· 开关幅度 ≥{CONTRAST_DB:.0f} 码")
    print(HEADER)
    print("-" * (len(HEADER) + 8))
    verdicts = []
    for title, shape, sigma, note in CASES:
        plugin = _feed_shape(BleRisk(), shape, frames=16, sigma=sigma)
        ev = plugin.evidence()
        verdicts.append(plugin.risk_type())
        print(
            f"{title:12s} {ev['adv_activity']:8.2f} {ev['shallow']:7.2f} {ev['wideband']:8.2f} "
            f"{ev['contrast_db']:8.1f} {ev['slots']:5.1f} {ev['slots_persistent']:4.0f}  {plugin.risk_type()}"
        )
        print(f"{'':12s} └ {note}；本机噪声抬升零点 rise_floor={ev['rise_floor']:.1f} 码")
    core = verdicts[:-1]  # 前五档必须互不相同；第六档是兜底，本来就该落回「分不清」
    distinct = len(set(core)) == len(core)
    fallback_ok = verdicts[-1] == core[3]
    print()
    print(f"五档现实输入 → {len(set(core))} 个互不相同的判词：{'通过' if distinct else '失败'}（{core}）")
    print(f"病态输入（整段一起动）→ 落到兜底「{verdicts[-1]}」：{'通过' if fallback_ok else '失败'}")

    if "--scenarios" in argv:
        from tests.test_ble_risk import run_scenario

        print()
        print(HEADER)
        print("-" * (len(HEADER) + 8))
        for name in (
            "quiet",
            "video_58g",
            "ble_crowded",
            "wifi_suppress_ble",
            "wifi_busy",
            "microwave",
            "mixed",
            "unknown_cw",
        ):
            plugin = run_scenario(name)
            ev = plugin.evidence()
            print(
                f"{name:12s} {ev['adv_activity']:8.2f} {ev['shallow']:7.2f} {ev['wideband']:8.2f} "
                f"{ev['contrast_db']:8.1f} {ev['slots']:5.1f} {ev['slots_persistent']:4.0f}  "
                f"{plugin.risk_type()}（评分 {plugin.score()}）"
            )
    return 0 if distinct and fallback_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
