# -*- coding: utf-8 -*-
"""扫 host/web 里仍然「会显示到界面」的中文（排除注释、排除 i18n.js 的 zh 词典、排除中文字典值）。

用法：python testing/_web_cjk.py [--all]
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "host", "web")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
EXTS = (".js", ".html", ".json", ".css")


def strip_i18n_zh(path, lines):
    """i18n.js 的 zh: { ... } 块整体跳过（那是中文字典，本来就该是中文）。"""
    if not path.endswith("i18n.js"):
        return lines
    out, inside = [], False
    for line in lines:
        if re.match(r"\s*zh:\s*\{", line):
            inside = True
            out.append((line, True))
            continue
        if inside and re.match(r"\s*en:\s*\{", line):
            inside = False
            out.append((line, True))
            continue
        out.append((line, inside))
    return out


def scan():
    hits = []
    for base, _dirs, files in os.walk(WEB):
        for name in sorted(files):
            if not name.endswith(EXTS):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            raw = io.open(path, encoding="utf-8").read().split("\n")
            marked = strip_i18n_zh(path, raw)
            for index, item in enumerate(marked):
                if isinstance(item, tuple):
                    line, skip = item
                else:
                    line, skip = item, False
                if skip or not CJK.search(line):
                    continue
                stripped = line.strip()
                if stripped.startswith(("//", "*", "/*", "*/", "#")):
                    continue
                hits.append((rel, index + 1, stripped))
    return hits


def main():
    hits = scan()
    show_all = "--all" in sys.argv
    by_file = {}
    for rel, number, text in hits:
        by_file.setdefault(rel, []).append((number, text))
    print(f"含中文的代码行 {len(hits)} 处，涉及 {len(by_file)} 个文件")
    for rel in sorted(by_file):
        print(f"\n{rel}  ({len(by_file[rel])})")
        for number, text in by_file[rel][: 200 if show_all else 6]:
            print(f"  {number:4d}  {text[:150]}")


if __name__ == "__main__":
    main()