"""酒店反偷拍「整场」仿真：没有实物正样本时，用 mock 合成信号自证「能发现」。

## 为什么需要这个脚本

任务 4（5.8G 守卫）和任务 5（可疑信号抓拍）的验收标准都是「真来了要抓到」。
但手上**没有真的 5.8G 图传、也没有真的偷拍摄像头** —— 真板空房间里跑只能证明
「不误报」，证明不了「抓得到」。所以这里用两条互补的仿真把灵敏度自证补齐：

| 阶段 | 数据 | 模块 | 期望 |
| --- | --- | --- | --- |
| 1 | `video_58g` 场景（= 5.8G 模拟图传：宽平顶 + 载波） | 只跑 `guard_58g` | 至少 1 条告警 |
| 2 | `unknown_cw_clean` 场景（= 2.4G 干净处一根窄带未知源） | 只跑 `capture` | 至少 1 条抓档 |
| 3 | 上面两阶段的产出合流 | `app.audit.verdict()` | 结论必须同时承认「有 5.8G 告警」和「有抓档证据」 |

阶段 1/2 都**只跑一个模块**（独占射频）：这两个模块分别要「每窗连续 8 帧」和
「同一窗 ≥3 s 跨度」，四模块共享一根射频时帧被摊薄，攒不够是设计代价
（见 `testing/scenario_matrix.py` 的双列表）。

## 它不是什么（必须诚实）

- **仿真，不是实物**：数据是合成的，噪声底/时间戳都是假的。它证明的是「判据在这
  种形状上会响」，不能证明「真摄像头的波形一定长这样」。
- **不做设备级归因**：分类器只看带宽/持续/占空比，判出 `unknown` 不等于「这是摄像头」。
- 有实物（哪怕一台 5.8G 无线摄像头）时，应当补一条真信号实测，把这里的注入证据
  升级成实物证据；本脚本不退场，它继续当回归。

## 用法

    python -m testing.anti_peep_sim                    # 跑三阶段 + 落 JSON
    python -m testing.anti_peep_sim --json .data/x.json
    python -m testing.anti_peep_sim --capture-dir .data/capture   # 真让 IQ 落盘

退出码：0 = 三阶段全部按预期；1 = 有阶段不符合预期；2 = 参数问题。
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
from app.audit import BOUNDARIES, collect_sources, verdict  # noqa: E402
from app.capture_link import CaptureAwareSource, iter_capture_plugins, profile_for, synthetic_capture  # noqa: E402
from core.registry import Registry  # noqa: E402
from core.scheduler import Scheduler  # noqa: E402
from modes._legacy import capture as capture_mode, guard_58g  # noqa: E402
from output.format import ascii_table, fmt_hz_short, kv_block  # noqa: E402
from tests.mock_source import SCENARIOS, make_source  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(os.path.dirname(HERE), ".data")


def run_phase(scenario: str, slices: int, solo: str, out_dir: str | None) -> dict[str, Any]:
    """跑一个阶段：只注册一个模块，独占射频。

    三个非实时跑的坑（都踩过）：
    1. `virtual_step_s=0.25` —— 不真睡，但每帧把假墙钟往前推，几秒内攒够跨度；
    2. `t0` 由 mock 源自己按 `time.time()` 收尾，喂进去的时间必须与模块的 now 同源；
    3. 非实时跑没有实时循环踩分析节拍 → 收尾必须显式补一脚 `analyze(now=最后一帧)`。
    """
    registry = Registry()
    if solo == "guard_58g":
        registry.register(guard_58g.Guard58G())
    else:
        registry.register(capture_mode.CaptureMode(out_dir=out_dir))
    cap_plugins = iter_capture_plugins(registry.plugins)
    profile = profile_for(scenario)
    capture_fn = lambda f, n, r, b: synthetic_capture(f, n, r, b, profile=profile)  # noqa: E731
    if cap_plugins:
        bind_capture(registry, capture_fn, out_dir=out_dir)

    source = make_source(scenario, realtime=False, frames_per_scan=2, virtual_step_s=0.25)
    wrapped = CaptureAwareSource(source, cap_plugins, capture_fn) if cap_plugins else source
    seen = {"ts": 0.0}
    started = time.time()
    scheduler = Scheduler(wrapped, registry, on_snapshot=lambda snap: seen.__setitem__("ts", snap.ts))
    stats = scheduler.run(max_slices=slices, max_snapshots=slices * 2)
    if cap_plugins:
        wrapped.flush()
    for plugin in registry.plugins:
        analyze = getattr(plugin, "analyze", None)
        if callable(analyze):
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
            except Exception as exc:  # noqa: BLE001 - 仿真不该因单模块炸而中断
                payloads[plugin.id] = {"error": f"{type(exc).__name__}: {exc}"}

    records = []
    for plugin in cap_plugins:
        for record in getattr(plugin, "records", []) or []:
            try:
                records.append(record.as_dict())
            except Exception:  # noqa: BLE001
                pass
    return {
        "phase": solo,
        "scenario": scenario,
        "slices": stats.slices,
        "snapshots": stats.snapshots,
        "elapsed_s": round(time.time() - started, 2),
        "records": records,
        "payloads": payloads,
        "sources": collect_sources(registry),
    }


def facts(result: dict[str, Any]) -> dict[str, Any]:
    guard = result["payloads"].get("guard_58g", {}) or {}
    cap = result["payloads"].get("capture", {}) or {}
    alerts = guard.get("alerts")
    alert_rows = alerts if isinstance(alerts, list) else []
    return {
        "guard_alerts": len(alert_rows),
        "guard_analyses": int(guard.get("analyses", 0) or 0),
        "captures": len(result["records"]) or len(cap.get("captures", []) or []),
        "cap_triggers": int(cap.get("triggers", 0) or 0),
        "cap_span": float(cap.get("span_s", 0.0) or 0.0),
        "alert_rows": alert_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="酒店反偷拍整场仿真（无硬件自证灵敏度）")
    parser.add_argument("--guard-slices", type=int, default=260, help="阶段 1 跑多少时间片（独占射频）")
    parser.add_argument("--capture-slices", type=int, default=240, help="阶段 2 跑多少时间片（独占射频）")
    parser.add_argument("--guard-scenario", default="video_58g", choices=sorted(SCENARIOS))
    parser.add_argument("--capture-scenario", default="unknown_cw_clean", choices=sorted(SCENARIOS))
    parser.add_argument("--capture-dir", default="", help="IQ 落盘目录（空 = 只统计不落盘）")
    parser.add_argument("--json", default=os.path.join(DEFAULT_DIR, "anti-peep-sim.json"))
    args = parser.parse_args(argv)

    out_dir = args.capture_dir or None
    started = time.time()
    print(
        kv_block(
            "酒店反偷拍 · 整场仿真（mock 合成信号，非实物）",
            [
                ("阶段 1", f"5.8G 图传场景 {args.guard_scenario} → guard_58g 独占 {args.guard_slices} 片"),
                ("阶段 2", f"隐藏窄带源场景 {args.capture_scenario} → capture 独占 {args.capture_slices} 片"),
                ("阶段 3", "合流：app.audit.verdict() 判四档结论"),
                ("抓档目录", out_dir or "（不落盘，只统计）"),
            ],
        )
    )

    p1 = run_phase(args.guard_scenario, args.guard_slices, "guard_58g", out_dir)
    f1 = facts(p1)
    p2 = run_phase(args.capture_scenario, args.capture_slices, "capture", out_dir)
    f2 = facts(p2)

    print()
    print(
        ascii_table(
            ["阶段", "场景", "模块", "分析/触发", "告警", "抓档", "跨度", "耗时"],
            [
                [
                    "1",
                    p1["scenario"],
                    "guard_58g",
                    f"analyses {f1['guard_analyses']}",
                    str(f1["guard_alerts"]),
                    "-",
                    "-",
                    f"{p1['elapsed_s']:.1f}s",
                ],
                [
                    "2",
                    p2["scenario"],
                    "capture",
                    f"triggers {f2['cap_triggers']}",
                    "-",
                    str(f2["captures"]),
                    f"{f2['cap_span']:.1f}s",
                    f"{p2['elapsed_s']:.1f}s",
                ],
            ],
            title="两阶段实测",
        )
    )

    # ── 阶段 3：合流体检 ────────────────────────────────────────────────
    sources = list(p1["sources"]) + list(p2["sources"])
    captures = list(p2["records"])
    headline, why = verdict(sources, captures)
    print()
    print(f"合流结论：{headline}")
    print(f"理由：{why}")
    if sources:
        print()
        print(
            ascii_table(
                ["来源", "类型", "频点", "带宽", "持续", "等级"],
                [
                    [
                        row.get("source", ""),
                        row.get("kind", ""),
                        fmt_hz_short(row["freq_mhz"] * 1e6) if row.get("freq_mhz") else "-",
                        f"{row['bw_mhz']:.1f} MHz" if row.get("bw_mhz") else "-",
                        f"{row['duration_s']:.1f} s" if row.get("duration_s") is not None else "-",
                        row.get("level", ""),
                    ]
                    for row in sources
                ],
                title="活跃发射源清单（仿真）",
            )
        )

    # ── 断言：两个正样本都必须真的响，否则「能发现」这句话不成立 ──────────
    checks = {
        "guard_alerts": f1["guard_alerts"] >= 1,
        "guard_analyzed": f1["guard_analyses"] >= 1,
        "capture_taken": f2["captures"] >= 1,
        "capture_triggered": f2["cap_triggers"] >= 1,
        # 结论不许含糊：既不能说「什么都没发现」，也不能说「确认是摄像头」
        "verdict_not_empty": bool(headline) and headline not in ("未发现可疑信号",),
        "verdict_not_overclaim": "确认是摄像头" not in headline,
    }
    ok = all(checks.values())
    print()
    print(ascii_table(["检查项", "结果"], [[k, "true" if v else "FALSE"] for k, v in checks.items()], title="断言"))

    report = {
        "kind": "anti_peep_sim",
        "ts": time.time(),
        "iso_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "mock 合成信号（仿真），不是实物正样本；证明判据在这类形状上会响",
        "phases": {"guard": p1, "capture": p2},
        "facts": {"guard": {k: v for k, v in f1.items() if k != "alert_rows"}, "capture": f2},
        "headline": headline,
        "why": why,
        "sources": sources,
        "captures": captures,
        "boundaries": list(BOUNDARIES)
        + [
            "本脚本用 mock 合成信号仿真，不是真板实测；真板空环境只能证明「不误报」。",
            "「抓到一条 unknown」不等于「确认是摄像头」：分类器只看带宽/持续/占空比。",
        ],
        "checks": checks,
        "ok": ok,
        "elapsed_s": round(time.time() - started, 2),
    }
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print()
        print(f"报告：{args.json}")
    print()
    print("OK 三阶段全部符合预期" if ok else "FAIL " + "，".join(k for k, v in checks.items() if not v))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())