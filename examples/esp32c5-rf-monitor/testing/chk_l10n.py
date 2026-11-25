# -*- coding: utf-8 -*-
"""核对 ``host/l10n.py`` 的 TABLE：运行时真正出现的中文骨架，缺了谁、占位对不对得上。

和 ``tests/test_l10n.py`` 的分工：
- 单测负责**红绿**（英文界面残留中文就失败）；
- 这个脚本负责**告诉你差哪条**（缺的骨架 + 词典里最接近的写法，方便直接补）。

跑法：``cd examples/esp32c5-rf-monitor && python testing/chk_l10n.py``
"""
from __future__ import annotations

import difflib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTDIR = os.path.join(ROOT, "host")
if HOSTDIR not in sys.path:
    sys.path.insert(0, HOSTDIR)
import paths  # noqa: E402 - 上位机模块

paths.ensure_on_path()
import l10n  # noqa: E402
from session import HostConfig, HostSession, to_json  # noqa: E402

SCEN = ["quiet", "wifi_busy", "ble_crowded", "unknown_cw_clean", "unknown_cw",
        "video_58g", "microwave", "mixed", "wifi_suppress_ble", "hotel_scan"]
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
OUTLETS = ("tables", "describe", "stats", "sweep_state", "gain_state", "audit")


def walk(node, out: list[str]) -> None:
    if isinstance(node, str):
        if CJK.search(node):
            out.append(node)
    elif isinstance(node, dict):
        for val in node.values():
            walk(val, out)
    elif isinstance(node, (list, tuple)):
        for val in node:
            walk(val, out)


def collect() -> list[str]:
    """用英文模式跑一遍全部场景，收回「还没被翻掉」的中文原文。"""
    left: list[str] = []
    for scen in SCEN:
        session = HostSession(bins_out=256)
        try:
            session.open(HostConfig.from_json(to_json(
                {"port": "MOCK", "mock": True, "scenario": scen, "lang": "en"})))
            for _ in range(30):
                session.step(0.5, on_snapshot=lambda snap: None)
            for name in OUTLETS:
                walk(getattr(session, name)(), left)
            for line in session.render_text().split("\n"):
                if CJK.search(line):
                    left.append(line.strip())
        finally:
            session.close()
    return left


def main() -> int:
    left = collect()
    skeletons = sorted({l10n.skeleton(text) for text in left})
    missing, badcount = [], []
    for key in skeletons:
        template = l10n.TABLE.get(key)
        if template is None:
            missing.append(key)
        elif template.count("{}") != key.count("{}"):
            badcount.append((key.count("{}"), template.count("{}"), key))

    print(f"英文模式下仍未翻掉的中文串 {len(left)} 条 / 去重骨架 {len(skeletons)} 个")
    print(f"缺条目 {len(missing)} / 占位不符 {len(badcount)}")
    for key in missing:
        near = difflib.get_close_matches(key, list(l10n.TABLE), n=1, cutoff=0.5)
        print(f"\nMISS: {key}")
        if near:
            print(f"  near: {near[0]}")
            for i in range(min(len(key), len(near[0]))):
                if key[i] != near[0][i]:
                    lo = max(0, i - 12)
                    print(f"  首个不同 @{i}: want {key[lo:i + 12]!r} / mine {near[0][lo:i + 12]!r}")
                    break
    for want, got, key in badcount:
        print(f"\nCOUNT {want} vs {got}: {key}")
    return 0 if not missing and not badcount else 1


if __name__ == "__main__":
    raise SystemExit(main())