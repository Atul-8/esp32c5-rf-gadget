# -*- coding: utf-8 -*-
"""英文模式下整份下行 payload 零 CJK 的核对脚本（手工排查用；正式断言见 tests）。"""
import io
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTDIR = os.path.join(ROOT, "host")
if HOSTDIR not in sys.path:
    sys.path.append(HOSTDIR)
import paths  # noqa: E402

paths.ensure_on_path()
import l10n
from session import HostConfig, HostSession, to_json  # noqa: E402

SCEN = ["quiet", "wifi_busy", "ble_crowded", "unknown_cw_clean", "unknown_cw",
        "video_58g", "microwave", "mixed", "wifi_suppress_ble", "hotel_scan"]
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def walk(node, path, out):
    if isinstance(node, str):
        if CJK.search(node):
            out.append((path, node))
    elif isinstance(node, dict):
        for key, val in node.items():
            walk(val, f"{path}.{key}", out)
    elif isinstance(node, (list, tuple)):
        for val in node:
            walk(val, f"{path}[]", out)


def run(lang):
    hits = {}
    for scen in SCEN:
        session = HostSession(bins_out=256)
        try:
            session.open(HostConfig.from_json(to_json(
                {"port": "MOCK", "mock": True, "scenario": scen, "lang": lang})))
            for _ in range(50):
                session.step(0.5, on_snapshot=lambda snap: None)
            found = []
            for name in ("tables", "describe", "stats", "sweep_state", "gain_state", "audit"):
                walk(getattr(session, name)(), name, found)
            number = 0
            for name in ("render_text",):
                text = getattr(session, name)()
                for line in text.split("\n"):
                    if CJK.search(line):
                        found.append((name, line[:160]))
                    number += 1
        finally:
            session.close()
        for path, text in found:
            hits.setdefault((scen, path, text), 0)
            hits[(scen, path, text)] += 1
    return hits


left = run("en")
print("en 模式残留中文", len(left))
with io.open(os.path.join(tempfile.gettempdir(), "en_left.txt"), "w", encoding="utf-8", newline="\n") as fh:
    for (scen, path, text) in sorted(left):
        fh.write(f"{scen:16s} {path:40s} {text}\n")
zh = run("zh")
print("zh 模式中文条目（应当很多，确认没被误翻）", len(zh))