# -*- coding: utf-8 -*-
"""看「原始输出」页的 ASCII 文本里，逐行翻译后还剩哪些中文片段。"""
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

SCEN = ["quiet", "wifi_busy", "ble_crowded", "unknown_cw_clean", "video_58g", "hotel_scan"]
CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff][\u3400-\u9fff0-9A-Za-z_.·%/（）()「」：:\- ]*")

left_lines = {}
frag = {}
for scen in SCEN:
    s = HostSession(bins_out=256)
    try:
        s.open(HostConfig.from_json(to_json({"port": "MOCK", "mock": True, "scenario": scen})))
        for _ in range(60):
            s.step(0.5, on_snapshot=lambda snap: None)
        text = s.render_text()
    finally:
        s.close()
    for line in text.split("\n"):
        if not l10n.has_cjk(line):
            continue
        out = l10n.translate(line)
        if l10n.has_cjk(out):
            left_lines.setdefault(line, scen)
            for run in CJK_RUN.finditer(out):
                frag[run.group(0)] = frag.get(run.group(0), 0) + 1

print("残留行", len(left_lines), "残留片段", len(frag))
with io.open(os.path.join(tempfile.gettempdir(), "ascii_left.txt"), "w", encoding="utf-8", newline="\n") as fh:
    for line in sorted(left_lines):
        fh.write(line + "\n")
with io.open(os.path.join(tempfile.gettempdir(), "ascii_frag.txt"), "w", encoding="utf-8", newline="\n") as fh:
    for f, n in sorted(frag.items(), key=lambda kv: -kv[1]):
        fh.write(f"{n:3d} | {f}\n")
print("已写 ascii_left.txt / ascii_frag.txt")