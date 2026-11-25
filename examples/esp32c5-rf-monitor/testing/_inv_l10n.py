# -*- coding: utf-8 -*-
"""盘点：英文模式下会下发到界面的中文串（来自 tables()/describe()/status()）。"""
import json, os, re, sys, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTDIR = os.path.join(ROOT, "host")
if HOSTDIR not in sys.path:
    sys.path.append(HOSTDIR)  # 只加 host；分析层由 ensure_on_path() 插到最前面
import paths; paths.ensure_on_path()
from session import HostConfig, HostSession, to_json

CJK = re.compile(r'[\u4e00-\u9fff]')
SCEN = ["quiet","wifi_busy","ble_crowded","unknown_cw_clean","unknown_cw",
        "video_58g","microwave","mixed","wifi_suppress_ble","hotel_scan"]

def walk(node, key, out):
    if isinstance(node, str):
        if CJK.search(node): out.append((key, node))
    elif isinstance(node, dict):
        for k, v in node.items(): walk(v, f"{key}.{k}", out)
    elif isinstance(node, (list, tuple)):
        for v in node: walk(v, f"{key}[]", out)

all_hits = {}
for scen in SCEN:
    s = HostSession(bins_out=256)
    try:
        s.open(HostConfig.from_json(to_json({"port":"MOCK","mock":True,"scenario":scen,"bins_out":256,"lang":"en"})))
        for _ in range(60):
            s.step(0.5, on_snapshot=lambda snap: None)
        hits = []
        walk(s.tables(), "tables", hits)
        walk(s.describe(), "describe", hits)
        walk(s.stats(), "stats", hits)
        walk(s.sweep_state(), "sweep", hits)
        for where, text in hits:
            all_hits.setdefault((where, text), set()).add(scen)
    finally:
        s.close()
print(f"en 模式剩余中文串 {len(all_hits)} 条")
by_len = sorted(all_hits.items(), key=lambda kv: -len(kv[0]))
with io.open(os.path.join(tempfile.gettempdir(), "inv_l10n_en.txt"), "w", encoding="utf-8", newline="\n") as fh:
    for text, scens in by_len:
        fh.write(f"{len(text):3d} | {text}\n")
print("已写 inv_l10n_en.txt")
