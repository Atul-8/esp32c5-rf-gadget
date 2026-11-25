"""场景矩阵：把**所有 mock 场景 × 所有真模块**跑一遍，出一张对照表。

用途有三个：

1. **验收**：每个场景该出什么结论（WiFi 拥挤 / BLE 风险 / 5.8G 告警 / 抓档条数）一眼可见，
   改门槛后重跑这张表就知道动了什么；
2. **回归**：脚本内置一组「场景 → 期望」的断言，跑不对就非 0 退出，可以进 CI；
3. **丰富测试数据**：一次跑完全部场景，把每个模块的完整 ``web_payload()``
   落成 JSON，前端/回放/文档配图都能直接拿这份数据用，不用再连板。

    python -m testing.scenario_matrix                     # 表 + 断言
    python -m testing.scenario_matrix --json .data/matrix.json
    python -m testing.scenario_matrix --slices 200 --out .data/capture/matrix

刻意**不接真板**：真板一次轮扫 4.4 s，跑 8 个场景要十几分钟，而且环境不可复现。
真板对照见 ``testing/capture_real.py`` / ``testing/guard_replay.py``。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bind_capture  # noqa: E402
from app.capture_link import CaptureAwareSource, iter_capture_plugins, profile_for, synthetic_capture  # noqa: E402
from core.registry import Registry, ScanRequest  # noqa: E402
from core.scheduler import Scheduler  # noqa: E402
from modes._legacy import ble_risk, capture as capture_mode, guard_58g, wifi_check  # noqa: E402
from output.format import ascii_table  # noqa: E402
from tests.mock_source import SCENARIOS, make_source  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(os.path.dirname(HERE), ".data")

#: 场景 → 期望（只写「一定要出现」的东西，不写具体数值，避免门槛微调就假红）
EXPECT: dict[str, dict[str, Any]] = {
    "quiet": {"capture_max": 0, "guard_max": 0},
    "wifi_busy": {"capture_max": 1, "wifi_busy_min": 1},
    "wifi_suppress_ble": {"capture_max": 1},
    # BLE 拥挤场景里除了广播信道那几根，还有一批「窄带但不在广播信道」的源会落
    # unknown（分类器只看形状不看信道）→ 这里允许 3 条，重点是 BLE 分数要起来。
    "ble_crowded": {"ble_score_min": 20.0, "capture_max": 3},
    "microwave": {"capture_max": 2},
    # 5.8G 守卫要「每个窗连续 8 帧」才敢报警。四模块共享一根射频 + 13 个窗轮转 →
    # 单窗帧被摊得太稀，共享跑法本来就攒不够（那一列只验「在算」）；
    # 「能报警」交给单模块自证列（独占 260 片跑）。
    "video_58g": {"guard_analyses_min": 5, "guard_solo_min": 1, "capture_max": 2},
    "unknown_cw": {"capture_max": 2},
    "unknown_cw_clean": {"capture_min": 1},
    "mixed": {"capture_max": 3},
    # 反偷拍「整场」场景（5.8G 模拟图传 + 2.4G 隐藏窄带源）。四模块共享射频时，
    # 两边都攒不够帧（守卫要每窗连续 8 帧、抓档要同窗 3 s 跨度），所以这里只验
    # 「在算 + 不误炸」；**「能发现」由 testing/anti_peep_sim.py 独占跑法自证**。
    "hotel_scan": {"capture_max": 4, "guard_analyses_min": 5},
}


def build_registry(out_dir: str | None, solo: str = "") -> tuple[Registry, list[Any]]:
    """四个真模块（瀑布图太重，矩阵里不跑；它有自己的 test_waterfall）。"""
    registry = Registry()
    if solo == "guard_58g":
        registry.register(guard_58g.Guard58G())
        return registry, []
    registry.register(wifi_check.WifiCheck())
    registry.register(ble_risk.BleRisk())
    registry.register(guard_58g.Guard58G())
    registry.register(capture_mode.CaptureMode(out_dir=out_dir))
    plugins = iter_capture_plugins(registry.plugins)
    return registry, plugins


def run_scenario(scenario: str, slices: int, out_dir: str | None, solo: str = "") -> dict[str, Any]:
    """``solo`` 非空时只注册那一个模块（给「共享射频下攒不够帧」的模块做灵敏度自证）。"""
    registry, cap_plugins = build_registry(out_dir, solo=solo)
    profile = profile_for(scenario)
    capture_fn = lambda f, n, r, b: synthetic_capture(f, n, r, b, profile=profile)  # noqa: E731
    if cap_plugins:
        # 存档目录由 CaptureMode(out_dir=...) 决定；这里只注入抓档函数
        bind_capture(registry, capture_fn, out_dir=out_dir)
    # virtual_step_s：不真睡，但每帧把假墙钟推 0.25 s（≥ capture 的 FRAME_MIN_GAP_S），
    # 这样非实时跑也能在几秒内攒够 MIN_SPAN_S=3 s 的跨度。
    source = make_source(scenario, realtime=False, frames_per_scan=2, virtual_step_s=0.25)
    wrapped = CaptureAwareSource(source, cap_plugins, capture_fn) if cap_plugins else source
    started = time.time()
    seen = {"ts": 0.0}
    scheduler = Scheduler(wrapped, registry, on_snapshot=lambda snap: seen.__setitem__("ts", snap.ts))
    stats = scheduler.run(max_slices=slices, max_snapshots=slices * 2)
    if cap_plugins:
        wrapped.flush()
    # 非实时跑没有「实时循环」帮模块踩分析节拍，这里显式补一脚：
    # 用**最后一帧的 ts**（虚拟时钟）当 now，跟模块内部的时间基准对齐。
    for plugin in registry.plugins:
        analyze = getattr(plugin, "analyze", None)
        if not callable(analyze):
            continue
        try:
            analyze(now=seen["ts"] or None)
        except TypeError:
            analyze()

    payloads: dict[str, Any] = {}
    for plugin in registry.plugins:
        payload_of = getattr(plugin, "web_payload", None)
        if callable(payload_of):
            try:
                payloads[plugin.id] = payload_of()
            except Exception as exc:  # noqa: BLE001 - 矩阵要能容忍单模块炸
                payloads[plugin.id] = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "scenario": scenario,
        "solo": solo,
        "slices": stats.slices,
        "snapshots": stats.snapshots,
        "elapsed_s": round(time.time() - started, 2),
        "profile": profile,
        "payloads": payloads,
    }


def facts_of(result: dict[str, Any]) -> dict[str, Any]:
    """从整包 payload 里抽「一行结论」要用的数字。"""
    payloads = result["payloads"]
    wifi = payloads.get("wifi_check", {}) or {}
    ble = payloads.get("ble_risk", {}) or {}
    guard = payloads.get("guard_58g", {}) or {}
    cap = payloads.get("capture", {}) or {}
    return {
        "scenario": result["scenario"],
        "slices": result["slices"],
        # wifi_check 的渠道表用 level 判「在用」，没有 in_use 字段
        "wifi_busy": sum(1 for c in (wifi.get("channels") or []) if c.get("level") not in ("空闲", "无覆盖", None)),
        "wifi_networks": len(wifi.get("interferers", []) or []),
        "ble_score": float(ble.get("score", 0.0) or 0.0),
        "ble_risk": str(ble.get("risk_type", "") or "-"),
        "guard_alerts": len(guard.get("alerts") or []) if isinstance(guard.get("alerts"), list) else int(guard.get("alerts", 0) or 0),
        "guard_analyses": int(guard.get("analyses", 0) or 0),
        "guard_frames": int(guard.get("frames", 0) or 0),
        "captures": len(cap.get("captures", []) or []),
        "cap_triggers": int(cap.get("triggers", 0) or 0),
        "cap_busy": int(cap.get("skipped_busy", 0) or 0),
        "cap_short": int(cap.get("skipped_short", 0) or 0),
        "cap_spur": int(cap.get("skipped_spur", 0) or 0),
        "cap_span": float(cap.get("span_s", 0.0) or 0.0),
        "cap_profile": result["profile"],
    }


def check(result: dict[str, Any]) -> list[str]:
    """返回失败原因列表（空 = 过）。"""
    facts = facts_of(result)
    expect = EXPECT.get(facts["scenario"], {})
    bad: list[str] = []
    if "capture_min" in expect and facts["captures"] < expect["capture_min"]:
        bad.append(f"captures {facts['captures']} < {expect['capture_min']}")
    if "capture_max" in expect and facts["captures"] > expect["capture_max"]:
        bad.append(f"captures {facts['captures']} > {expect['capture_max']}")
    if "guard_analyses_min" in expect and facts["guard_analyses"] < expect["guard_analyses_min"]:
        bad.append(f"guard analyses {facts['guard_analyses']} < {expect['guard_analyses_min']}")
    if "guard_max" in expect and facts["guard_alerts"] > expect["guard_max"]:
        bad.append(f"guard {facts['guard_alerts']} > {expect['guard_max']}")
    if "ble_score_min" in expect and facts["ble_score"] < expect["ble_score_min"]:
        bad.append(f"ble score {facts['ble_score']} < {expect['ble_score_min']}")
    if "wifi_busy_min" in expect and facts["wifi_busy"] < expect["wifi_busy_min"]:
        bad.append(f"wifi busy {facts['wifi_busy']} < {expect['wifi_busy_min']}")
    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mock 场景 × 模块 矩阵")
    parser.add_argument("--slices", type=int, default=240, help="每个场景跑多少个时间片（四模块共享）")
    parser.add_argument("--solo-slices", type=int, default=260, help="单模块自证跑多少个时间片")
    parser.add_argument("--json", default="", help="整包 payload 落盘路径")
    parser.add_argument("--out", default="", help="抓档存档目录（默认不落盘，只统计）")
    parser.add_argument("--only", default="", help="只跑这些场景（逗号分隔）")
    args = parser.parse_args(argv)

    wanted = [s.strip() for s in args.only.split(",") if s.strip()] or list(SCENARIOS)
    unknown = [s for s in wanted if s not in SCENARIOS]
    if unknown:
        print(f"!! 未知场景：{unknown}；可选 {list(SCENARIOS)}", file=sys.stderr)
        return 2

    rows: list[list[str]] = []
    details: list[dict[str, Any]] = []
    failures: list[str] = []
    for scenario in wanted:
        result = run_scenario(scenario, args.slices, args.out or None)
        facts = facts_of(result)
        details.append(result)
        bad = check(result)
        if bad:
            failures.append(f"{scenario}: " + "；".join(bad))
        solo_alerts = "-"
        solo_min = (EXPECT.get(scenario, {}) or {}).get("guard_solo_min")
        if solo_min:
            solo = run_scenario(scenario, args.solo_slices, args.out or None, solo="guard_58g")
            details.append(solo)
            solo_facts = facts_of(solo)
            solo_alerts = str(solo_facts["guard_alerts"])
            if solo_facts["guard_alerts"] < solo_min:
                failures.append(f"{scenario}: 单模块 5.8G 告警 {solo_facts['guard_alerts']} < {solo_min}")
        rows.append(
            [
                facts["scenario"],
                facts["cap_profile"],
                f"{facts['wifi_busy']}/{facts['wifi_networks']}",
                f"{facts['ble_score']:.0f} {facts['ble_risk']}",
                f"{facts['guard_alerts']}/{solo_alerts}",
                f"{facts['captures']}（trig {facts['cap_triggers']}）",
                f"{facts['cap_busy']}/{facts['cap_short']}/{facts['cap_spur']}",
                "PASS" if not bad else "FAIL " + "；".join(bad),
            ]
        )

    print(
        ascii_table(
            ["场景", "抓档长相", "WiFi 忙/网数", "BLE 分/判词", "5.8G 共享/单跑", "抓档条数", "busy/short/spur", "断言"],
            rows,
        )
    )
    print()
    print("口径：抓档条数 = 实际落盘条数；trig = 判成 unknown 的次数（含被过滤挡掉的）")
    print("      busy/short/spur = 环境饱和 / 缓冲跨度不足 / 本机杂散 三档过滤计数")
    print("      5.8G 共享/单跑 = 四模块共享射频时的告警数 / 该模块独占射频时的告警数")

    if args.json:
        path = args.json
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "slices": args.slices, "scenarios": details}, handle, ensure_ascii=False, indent=2)
        print(f"\n整包数据：{path}（{sum(len(d['payloads']) for d in details)} 个模块 payload）")

    if failures:
        print()
        for line in failures:
            print(f"!! {line}")
        return 1
    print("\n全部场景断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())