"""抓档灵敏度回放：直接喂合成快照，验证「什么时候抓、什么时候不抓」。

为什么需要它：真板上「能抓到摄像头」没法自证（房间里没有真设备，见
``docs/05-capture.md`` 的「未验证项」）。所以把 ``modes/capture.py`` 的四道门
（抽帧 / 最小跨度 / 环境饱和 / 本机杂散）逐个用合成数据压一遍，输出一张
「这个输入 → 这个判决」的对照表 —— 这就是可复核的自证材料。

    python -m testing.capture_replay                 # 全部用例
    python -m testing.capture_replay --case crowded  # 单个用例
    python -m testing.capture_replay --json .data/capture-replay.json

跟 ``tests/test_capture.py`` 的分工：单测守住「每个门各自的边界」，本脚本守住
「组合起来在典型输入下的行为」，并且**打印人眼可读的对照表**（给验收材料用）。

边界：合成快照是理想模型（无相位噪声、无多径、帧间隔规整），它只能证明
「判据按设计工作」，不能证明「真房间里抓得到」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.snapshot import Snapshot  # noqa: E402
from modes._legacy import capture as cap  # noqa: E402
from output.format import ascii_table  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(os.path.dirname(HERE), ".data")

#: 一个窗的 512 个格，频率升序（跟 core/snapshot.py 的契约一致）
BINS = cap.BINS
BW = cap.SCAN_BW_HZ
BIN_HZ = BW / BINS


def flat(level: float = 20.0) -> list[float]:
    return [level] * BINS


def bump(codes: list[float], center_hz: float, width_hz: float, gain: float, window_hz: float) -> list[float]:
    """在窗内按频率画一个矩形凸起（模拟一个发射源）。"""
    out = list(codes)
    half = width_hz / 2.0
    for i in range(BINS):
        freq = window_hz - BW / 2 + (i + 0.5) * BIN_HZ
        if abs(freq - center_hz) <= half:
            out[i] += gain
    return out


def snapshot(window_hz: float, codes: list[float], ts: float) -> Snapshot:
    return Snapshot(ts=ts, center_hz=int(window_hz), bw_hz=int(BW), bins_db=tuple(codes), meta={"source": "replay"})


def feed(plugin: cap.CaptureMode, window_hz: float, maker: Callable[[int], list[float]], count: int, step: float = 0.25) -> None:
    """按 ≥ FRAME_MIN_GAP_S 的间隔喂帧，**时间基准必须与 time.time() 同线**。

    两个坑都踩过：
    1. 不抽帧（step < FRAME_MIN_GAP_S）整批被抽帧过滤器吞掉；
    2. 时间基准写成 1000.0 这种「过去的值」→ 模块内部的新鲜度判断（拿真实
       ``time.time()`` 当 now）会认为所有帧都过期 → 一律 ``skipped_short``。
    """
    assert step >= cap.FRAME_MIN_GAP_S, "步进必须 ≥ FRAME_MIN_GAP_S，否则整批被抽帧过滤"
    t0 = time.time() - (count - 1) * step
    for i in range(count):
        plugin.on_snapshot(snapshot(window_hz, maker(i), t0 + i * step))
    # 非实时跑没有「实时循环」踩分析节拍，这里显式分析一次
    plugin.analyze()


# ---------------------------------------------------------------------------
# 用例：每个都是「一个窗喂什么 → 期望抓 / 不抓」
# ---------------------------------------------------------------------------
def case_clean_cw() -> dict[str, Any]:
    """干净环境里一个 1.2 MHz / +32 码的窄带持续源 → 必须抓。"""
    plugin = cap.CaptureMode()
    center = cap.window_centers()[2]
    feed(plugin, center, lambda i: bump(flat(), center + 1_500_000, 1_200_000, 32.0, center), 20)
    return {"plugin": plugin, "center": center, "expect": "capture", "why": "窄带持续 + 抬升 32 码，四道门全过"}


def case_pure_spur() -> dict[str, Any]:
    """300 kHz / 满占空 → 判本机杂散，报清单但不抓。"""
    plugin = cap.CaptureMode()
    center = cap.window_centers()[3]
    feed(plugin, center, lambda i: bump(flat(), center + 2_000_000, 300_000, 34.0, center), 20)
    return {"plugin": plugin, "center": center, "expect": "spur", "why": f"宽度 0.3 MHz ≤ SPUR_MAX_HZ({cap.SPUR_MAX_HZ/1e3:.0f} kHz)"}


def case_short_span() -> dict[str, Any]:
    """只有 1.2 s 跨度（5 帧）→ 不判「持续」。"""
    plugin = cap.CaptureMode()
    center = cap.window_centers()[2]
    feed(plugin, center, lambda i: bump(flat(), center, 1_200_000, 32.0, center), 5)
    return {"plugin": plugin, "center": center, "expect": "skip_short", "why": f"跨度 1.25 s < MIN_SPAN_S({cap.MIN_SPAN_S:.1f} s)"}


def case_crowded_window() -> dict[str, Any]:
    """17 MHz 平顶 + 两处深凹陷（满负荷 WiFi 的样子）→ 环境饱和，不抓。"""
    plugin = cap.CaptureMode()
    center = cap.window_centers()[1]

    def maker(i: int) -> list[float]:
        codes = bump(flat(), center, 17_000_000, 30.0, center)
        # 凹陷：把两端各切掉一段（真实 WiFi 是多个信道之间有缝）
        for j in range(BINS):
            freq = center - BW / 2 + (j + 0.5) * BIN_HZ
            if abs(freq - (center - 6_000_000)) < 400_000 or abs(freq - (center + 6_000_000)) < 400_000:
                codes[j] = 20.0
        return codes

    feed(plugin, center, maker, 20)
    return {"plugin": plugin, "center": center, "expect": "skip_busy", "why": "抬起格占窗 ≥35% 且被凹陷切成多段 = 饱和"}


def case_weak_narrow() -> dict[str, Any]:
    """1.2 MHz 但只 +12 码 → 窄段门槛 30 码，不抓。"""
    plugin = cap.CaptureMode()
    center = cap.window_centers()[0]
    feed(plugin, center, lambda i: bump(flat(), center, 1_200_000, 12.0, center), 20)
    return {"plugin": plugin, "center": center, "expect": "no_capture", "why": f"窄段抬升 12 码 < NARROW_ALERT_DB({cap.NARROW_ALERT_DB:.0f})"}


def case_edge_clipped() -> dict[str, Any]:
    """信号贴在窗沿被切一半 → 宽度不可信，不抓。"""
    plugin = cap.CaptureMode()
    center = cap.window_centers()[4]
    feed(plugin, center, lambda i: bump(flat(), center + BW / 2 - 1_000_000, 1_200_000, 32.0, center), 20)
    return {"plugin": plugin, "center": center, "expect": "no_capture", "why": "段贴窗沿（±500 kHz 内），宽度被窗切掉不可信"}


def case_wideband_microwave() -> dict[str, Any]:
    """16 MHz / +25 码的连续平顶（微波炉那种）→ 一段到底不算饱和，但**也不抓**。

    原因不是门挡了，而是分类器先把它判成 ``microwave``（宽带 + 高占空 = 已知大类），
    而抓档只盯 ``unknown``：抓的是「分不清是什么」的东西，不是「已知的干扰」。
    这条用例把这条分工钉住，避免以后把「没抓」误读成漏检。
    """
    plugin = cap.CaptureMode()
    center = cap.window_centers()[3]
    feed(plugin, center, lambda i: bump(flat(), center, 16_000_000, 25.0, center), 20)
    labels = [c.classification.label for c in plugin.candidates(plugin.windows[center])]
    return {
        "plugin": plugin,
        "center": center,
        "labels": labels,
        "expect": "no_capture",
        "why": "16 MHz 平顶被分类器判成 microwave（已知大类）→ 抓档只收 unknown",
    }


CASES: dict[str, Callable[[], dict[str, Any]]] = {
    "clean_cw": case_clean_cw,
    "pure_spur": case_pure_spur,
    "short_span": case_short_span,
    "crowded": case_crowded_window,
    "weak_narrow": case_weak_narrow,
    "edge_clipped": case_edge_clipped,
    "wideband_known": case_wideband_microwave,
}


def judge(result: dict[str, Any]) -> tuple[str, bool, str]:
    """把一次用例的实际结果判成 capture / spur / skip_xxx / no_capture。

    注意：``skipped_short`` 是**全局**计数器，空窗（其它 5 个窗）每轮都会 +1，
    所以「这一例是不是被跨度门挡了」必须看**目标窗自己的 span**，不能看计数器，
    否则每个用例都会被误标成 ``skip_short``（第一次跑就是这么假红的）。
    """
    plugin: cap.CaptureMode = result["plugin"]
    expect = result["expect"]
    center = result.get("center")
    stat = plugin.windows.get(center) if center is not None else None
    span = stat.span_s() if stat is not None else 0.0
    pending = list(plugin.pending)
    spurs = list(plugin.spurs)
    actual = "no_capture"
    detail = f"span={span:.2f}s"
    labels = result.get("labels") or []
    if pending:
        actual = "capture"
        req = pending[0]
        detail = f"{req.freq_hz/1e6:.2f} MHz / {req.samples} 样本 / rate_code={req.rate_code}"
    elif spurs:
        actual = "spur"
        detail = f"{spurs[0].center_hz/1e6:.2f} MHz / {spurs[0].width_hz/1e3:.0f} kHz / fill={spurs[0].fill:.2f}"
    elif plugin.skipped_busy:
        actual = "skip_busy"
        detail = f"skipped_busy={plugin.skipped_busy} span={span:.2f}s"
    elif span < cap.MIN_SPAN_S:
        actual = "skip_short"
        detail = f"span={span:.2f}s < {cap.MIN_SPAN_S:.1f}s"
    elif labels:
        detail = f"span={span:.2f}s / 分类 {','.join(labels)} → 非 unknown 不抓"
    ok = actual == expect
    return actual, ok, detail


def run(case_name: str, out_dir: str | None) -> dict[str, Any]:
    result = CASES[case_name]()
    plugin: cap.CaptureMode = result["plugin"]
    if out_dir:
        plugin.out_dir = out_dir
    actual, ok, detail = judge(result)
    return {
        "case": case_name,
        "expect": result["expect"],
        "actual": actual,
        "ok": ok,
        "why": result["why"],
        "detail": detail,
        "triggers": plugin.triggers,
        "skipped": {
            "short": plugin.skipped_short,
            "busy": plugin.skipped_busy,
            "spur": plugin.skipped_spur,
            "weak": plugin.skipped_weak,
            "edge": plugin.skipped_edge,
        },
        "span_s": round(plugin.span_s(), 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓档灵敏度回放（合成快照）")
    parser.add_argument("--case", default="", help=f"只跑一个：{','.join(CASES)}")
    parser.add_argument("--json", default="", help="结果落盘")
    parser.add_argument("--out", default="", help="存档目录（给了就真落 IQ 文件）")
    args = parser.parse_args(argv)

    names = [c.strip() for c in args.case.split(",") if c.strip()] or list(CASES)
    unknown = [n for n in names if n not in CASES]
    if unknown:
        print(f"!! 未知用例：{unknown}；可选 {list(CASES)}", file=sys.stderr)
        return 2

    out_dir = args.out or None
    rows: list[list[str]] = []
    details: list[dict[str, Any]] = []
    failures: list[str] = []
    for name in names:
        result = run(name, out_dir)
        details.append(result)
        if not result["ok"]:
            failures.append(f"{name}: 期望 {result['expect']}，实际 {result['actual']}")
        rows.append(
            [
                name,
                result["expect"],
                result["actual"],
                f"{result['triggers']}",
                result["detail"] or "-",
                "PASS" if result["ok"] else "FAIL",
                result["why"],
            ]
        )

    print(
        ascii_table(
            ["用例", "期望", "实际", "triggers", "证据", "判决", "依据"],
            rows,
        )
    )
    print()
    print("门槛（全部来自 modes/capture.py，改常量这张表跟着变）：")
    print(
        f"  ALERT_DB={cap.ALERT_DB} NARROW_ALERT_DB={cap.NARROW_ALERT_DB} NARROW_MAX_HZ={cap.NARROW_MAX_HZ/1e6:.1f}MHz "
        f"STEADY_FILL={cap.STEADY_FILL} MIN_FRAMES={cap.MIN_FRAMES}"
    )
    print(
        f"  FRAME_MIN_GAP_S={cap.FRAME_MIN_GAP_S}s MIN_SPAN_S={cap.MIN_SPAN_S}s BUSY_FILL={cap.BUSY_FILL} "
        f"SPUR_MAX_HZ={cap.SPUR_MAX_HZ/1e3:.0f}kHz EDGE_GUARD_HZ={cap.EDGE_GUARD_HZ/1e3:.0f}kHz"
    )

    if args.json:
        path = args.json
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "cases": details}, handle, ensure_ascii=False, indent=2)
        print(f"\n结果：{path}")

    if failures:
        print()
        for line in failures:
            print(f"!! {line}")
        return 1
    print("\n全部用例符合预期")
    print("! 边界：合成快照只能证明「判据按设计工作」，不能证明「真房间里抓得到」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())