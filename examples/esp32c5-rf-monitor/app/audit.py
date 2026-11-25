"""「酒店反偷拍」一键体检：跑一轮全频段扫描，出一张「活跃发射源清单 + 可疑项证据」。

    python -m app.audit --port COM56 --duration 90 --gain-index 60   # 真机
    python -m app.audit --mock --duration 20                        # 无硬件自测
    python -m app.audit --mock --duration 20 --json .data/audit/a.json

产出三样东西：

1. 终端一张「活跃发射源清单」（来源 / 类型 / 频点 / 带宽 / 持续 / 判读）；
2. 可疑项证据清单（抓拍文件路径 + 前后各 30 s 上下文所在 sidecar）；
3. 一份 JSON 体检报告落盘（默认 ``.data/audit/audit-<时间戳>.json``）。

**它不是什么**：不是「查出摄像头」的判决书。抓到的只是「这一段频谱像不像已知类型」，
分类是启发式的、功率是相对码、扫描是分时的。真实结论必须人拿着 IQ 文件复核。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Sequence

from app import bind_capture, bind_store
from app.capture_link import CaptureAwareSource, iter_capture_plugins, synthetic_capture
from core.registry import Registry, discover
from core.scheduler import Scheduler
from core.serial_link import EspSdrLink, SerialLinkError
from core.store import Store
from output.format import ascii_table, fmt_duration, fmt_hz_short, fmt_pct, kv_block
from tests.fake_firmware import FakeFirmware
from tests.mock_modes import ProbePlugin
from tests.mock_source import SCENARIOS

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data")

BOUNDARIES = [
    "分时扫描的快照，不是连续实时监测：同一时刻只有一个频段在被扫，抓档期间射频停扫。",
    "功率码是相对 dB 码，不是 dBm；跨增益不可比 —— 体检期间请固定增益（--gain-index）。",
    "分类是启发式的（只看带宽/持续/占空比），不做调制识别、不做设备级归因。",
    "「未发现可疑抓拍」≠「房间里没有摄像头」：只看 2.4G/5.8G 的功率特征，"
    "看不到离线存储型设备、也看不到非 ISM 频段。",
]


def build_registry() -> tuple[Registry, list[str]]:
    registry = discover("modes")
    notes = list(registry.errors)
    if len(registry) == 0:
        registry.register(ProbePlugin(name="链路探针", slice_weight=1.0))
        notes.append("modes/ 里没有功能模块 → 只跑链路探针（体检报告会是空的）")
    return registry, notes


def collect_sources(registry: Registry) -> list[dict[str, Any]]:
    """把各模块的产出汇成一张「活跃发射源清单」。"""
    rows: list[dict[str, Any]] = []
    for plugin in registry.plugins:
        payload_of = getattr(plugin, "web_payload", None)
        if not callable(payload_of):
            continue
        try:
            payload = payload_of()
        except Exception as exc:  # noqa: BLE001 - 单模块出错不该让体检中断
            rows.append(
                {"source": plugin.id, "kind": "模块出错", "detail": f"{type(exc).__name__}: {exc}", "level": "未知"}
            )
            continue
        rows.extend(_sources_of(plugin.id, payload))
    return rows


def _sources_of(module: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if module == "guard_58g":
        for alert in payload.get("alerts") or []:
            out.append(
                {
                    "source": "5.8G 守卫",
                    "kind": alert.get("kind", "?"),
                    "freq_mhz": alert.get("center_mhz"),
                    "bw_mhz": alert.get("bw_mhz"),
                    "duration_s": alert.get("duration_s"),
                    "detail": f"抬升 {alert.get('peak_excess_db', 0):.0f} 码 / 占空 {alert.get('fill', 0):.0%}",
                    "level": "需复核" if "窄带" in str(alert.get("kind")) else "关注",
                }
            )
        for window in payload.get("windows") or []:
            floor = window.get("floor_code")
            if floor is None or window.get("frames", 0) < 4:
                continue
            if window.get("coverage", 1.0) < 0.6:
                out.append(
                    {
                        "source": "5.8G 守卫",
                        "kind": "该窗无覆盖",
                        "freq_mhz": round(window.get("center_hz", 0) / 1e6, 2),
                        "detail": f"覆盖 {window.get('coverage', 0):.0%}",
                        "level": "未知",
                    }
                )
    elif module == "ble_risk":
        if payload.get("risk_type") and payload.get("frames"):
            out.append(
                {
                    "source": "BLE 风险评估",
                    "kind": payload.get("risk_type", "?"),
                    "detail": (
                        f"评分 {payload.get('score', 0):.0f}/100（{payload.get('level', '?')}）"
                        f" / 宽带占用 {payload.get('wideband', 0):.0%}"
                        f" / 参考底噪 {payload.get('ref_db', 0):.0f} 码"
                    ),
                    "level": "关注" if (payload.get("score") or 0) >= 40 else "低",
                }
            )
    elif module == "wifi_check":
        for channel in payload.get("channels") or []:
            if channel.get("level") in ("重度", "重度干扰"):
                out.append(
                    {
                        "source": "WiFi 信道",
                        "kind": f"{channel.get('band', '')} ch{channel.get('channel', '?')}",
                        "freq_mhz": channel.get("freq_mhz"),  # wifi_check 没下发频点就显示 '-'，不编数字
                        "detail": (
                            f"相对抬升 {channel.get('excess_db', 0):.0f} dB 码"
                            f" / 占用 {channel.get('occupancy', 0):.0%}"
                        ),
                        "level": "重度",
                    }
                )
    elif module == "capture":
        for record in payload.get("captures") or []:
            out.append(
                {
                    "source": "可疑抓拍",
                    "kind": f"CAP-{int(record.get('id', 'CAP-000').split('-')[-1]):03d}",
                    "freq_mhz": record.get("freq_mhz"),
                    "bw_mhz": (record.get("candidate") or {}).get("width_mhz"),
                    "detail": record.get("reason", ""),
                    "level": "可疑",
                    "file": record.get("path"),
                }
            )
    return out


def captures_of(registry: Registry) -> list[dict[str, Any]]:
    """把各抓档模块已经存下来的记录汇成一张清单（不触发新抓档）。"""
    rows: list[dict[str, Any]] = []
    for plugin in registry.plugins:
        if not callable(getattr(plugin, "take_pending", None)):
            continue
        try:
            rows.extend(record.as_dict() for record in getattr(plugin, "records", []) or [])
        except Exception as exc:  # noqa: BLE001 - 单模块出错不该让体检中断
            rows.append({"id": "-", "freq_mhz": None, "label": "模块出错", "reason": f"{type(exc).__name__}: {exc}"})
    return rows


def summary(registry: Registry) -> dict[str, Any]:
    """给上位机用的一键体检：**只读当前状态**，不重新跑扫描、不新抓档。

    CLI 版（``main``）会真跑一分钟；界面版拿的是「这一轮已经扫到的」结论，
    所以措辞同样必须带边界（``boundaries`` 照抄 CLI 的四条）。
    """
    sources = collect_sources(registry)
    captures = captures_of(registry)
    headline, why = verdict(sources, captures)
    return {
        "kind": "anti_peep_audit",
        "headline": headline,
        "why": why,
        "sources": sources,
        "captures": captures,
        "boundaries": list(BOUNDARIES),
        "done": False,  # 界面版：不是「跑完一分钟」的正式报告
    }


def verdict(sources: list[dict[str, Any]], captures: list[dict[str, Any]]) -> tuple[str, str]:
    """给一句结论 + 理由。措辞必须能反映「没抓到」不等于「安全」。"""
    suspicious = [row for row in sources if row.get("level") == "可疑"]
    review = [row for row in sources if row.get("level") == "需复核"]
    if suspicious:
        return (
            "发现可疑信号并已抓档",
            f"{len(suspicious)} 个分不清类型的源已存证（{', '.join(str(r.get('freq_mhz')) + ' MHz' for r in suspicious[:4])}），"
            "请人工复核 IQ 文件；这不是「确认是摄像头」。",
        )
    if review:
        return (
            "有需要复核的持续信号",
            f"{len(review)} 条窄带持续源（见清单），类型判读为 5.8G 图传样；分类器没抓到可分型证据。",
        )
    if captures:
        return ("有抓拍但分类为已知类型", "抓拍清单里有记录，但都不是「分不清」的源。")
    return (
        "本轮未抓到可疑信号",
        "只代表这段时间、这几个频段、这种灵敏度下没看到异常；不等于房间里没有摄像头。",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="酒店反偷拍 · 全频段一键体检")
    parser.add_argument("--port", default="MOCK", help="串口，如 COM56")
    parser.add_argument("--baud", type=int, default=2_000_000)
    parser.add_argument("--duration", type=float, default=90.0, help="体检时长（秒），越久越可信")
    parser.add_argument("--mock", action="store_true", help="用假固件（无硬件自测）")
    parser.add_argument("--scenario", default="mixed", choices=sorted(SCENARIOS), help="--mock 时的场景")
    parser.add_argument("--gain-index", type=int, default=None, help="固定增益档位（体检必须固定，否则跨信道不可比）")
    parser.add_argument("--db", default=os.path.join(DEFAULT_DIR, "audit.db"), help="SQLite 路径")
    parser.add_argument(
        "--json",
        default="",
        help="体检报告 JSON 路径（空 = .data/audit/audit-<时间戳>.json）",
    )
    parser.add_argument("--capture-dir", default=os.path.join(DEFAULT_DIR, "capture"), help="IQ 抓档目录")
    args = parser.parse_args(argv)

    registry, notes = build_registry()
    factory = (
        (lambda port, baud, timeout=5.0: FakeFirmware(port, baud, timeout, scenario=args.scenario))
        if args.mock
        else None
    )
    link = EspSdrLink(args.port, args.baud, factory=factory)
    report_path = args.json or os.path.join(DEFAULT_DIR, "audit", f"audit-{time.strftime('%Y%m%d-%H%M%S')}.json")
    started = time.time()
    exit_code = 0
    store: Store | None = None
    try:
        link.open()
        if args.gain_index is not None and not args.mock:
            link.set_gain("MANUAL", args.gain_index)
        store = Store(args.db)
        print(
            kv_block(
                "酒店反偷拍 · 全频段体检",
                [
                    ("端口", f"{args.port} @ {args.baud} baud" + ("（假固件）" if args.mock else "")),
                    ("固件", link.identity),
                    ("体检时长", fmt_duration(args.duration)),
                    ("增益", "硬件 AGC（结果不可跨信道比较）" if args.gain_index is None else f"固定档位 {args.gain_index}"),
                    ("模块", ", ".join(f"{p.id}(w={p.slice_weight:g})" for p in registry.plugins)),
                ],
            )
        )
        for note in notes:
            print(f"! {note}")
        bind_store(registry, store)
        cap_plugins = iter_capture_plugins(registry.plugins)
        if args.mock:
            capture_fn = synthetic_capture
        else:
            capture_fn = lambda freq, samples, rate, bits: link.capture_iq(samples, rate, bits, freq)  # noqa: E731
        bind_capture(registry, capture_fn, out_dir=args.capture_dir)
        source: Any = link
        capture_source = None
        if cap_plugins:
            capture_source = CaptureAwareSource(link, cap_plugins, capture_fn)
            source = capture_source
        stats = Scheduler(source, registry, store=store).run(duration_s=args.duration)
        if capture_source is not None:
            capture_source.flush()
        sources = collect_sources(registry)
        captures = [
            record
            for plugin in cap_plugins
            for record in [r.as_dict() for r in getattr(plugin, "records", [])]
        ]
        headline, why = verdict(sources, captures)
        print()
        print(
            ascii_table(
                ["来源", "类型", "频点", "带宽", "持续", "等级", "依据"],
                [
                    [
                        row.get("source", ""),
                        row.get("kind", ""),
                        fmt_hz_short(row["freq_mhz"] * 1e6) if row.get("freq_mhz") else "-",
                        f"{row['bw_mhz']:.1f} MHz" if row.get("bw_mhz") else "-",
                        f"{row['duration_s']:.1f} s" if row.get("duration_s") is not None else "-",
                        row.get("level", ""),
                        (row.get("detail") or "")[:60],
                    ]
                    for row in sources
                ]
                or [["-", "-", "-", "-", "-", "-", "没有任何模块报出活跃源"]],
                title="活跃发射源清单",
            )
        )
        print()
        print(f"结论：{headline}")
        print(f"理由：{why}")
        if captures:
            print()
            for record in captures:
                print(
                    f"证据：{record.get('id')} {record.get('freq_mhz')} MHz "
                    f"{record.get('samples')} 样本 / {record.get('bits')} 位 / "
                    f"{record.get('label')} → {record.get('path') or '(未落盘)'}"
                )
        report = {
            "kind": "anti_peep_audit",
            "ts": time.time(),
            "iso_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_s": round(stats.elapsed_s, 2),
            "slices": stats.slices,
            "snapshots": stats.snapshots,
            "source_errors": stats.source_errors,
            "gain_index": args.gain_index,
            "headline": headline,
            "why": why,
            "sources": sources,
            "captures": captures,
            "modules": [plugin.id for plugin in registry.plugins],
            "boundaries": BOUNDARIES,
            "elapsed_s": round(time.time() - started, 2),
        }
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print()
        print(f"报告：{report_path}")
        for boundary in BOUNDARIES:
            print(f"! {boundary}")
        if stats.source_errors:
            exit_code = 1
            print(f"! 源错误 {stats.source_errors} 次：{stats.last_source_error}", file=sys.stderr)
    except SerialLinkError as exc:
        print(f"!! 串口/协议错误：{exc}", file=sys.stderr)
        return 2
    finally:
        link.close()
        if store is not None:
            store.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())