# -*- coding: utf-8 -*-
"""弹窗/页面的**快速探针**：不启射频线程，只加载网页验「切页时弹窗怎么走」。

为什么单独一个工具：整套 ``app.py --selftest`` 跑一遍 3 分半，而弹窗暂停/恢复这类
纯 DOM + 路由的行为，改一行就要验一次。这个探针 ~20 秒出结果。

跑法（在本目录 ``host/`` 下）::

    python tests/probe_popout.py            # 验 WiFi 页实时流面板 + 汇总型面板
    python tests/probe_popout.py --page ble # 换一页验（任何页都行）

判据（都要和这里打印的一致）：

* ``opened``        弹出来 → ``open:true, paused:false``
* ``paused_ble``    切走 → ``open:true, paused:true, banner:true, owner:<原页>``
* ``resumed``       切回 → ``open:true, paused:false``（面板没被重建）
* ``static_opened`` 汇总型面板能弹出来（``static:true``）
* ``static_after_switch`` 汇总型切页后 ``open:false``（直接关掉，不留暂停）

注意它**不验数据刷新**（没有射频线程），那是 ``--selftest`` 的事。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")

OUT: dict[str, str] = {}


def main() -> int:
    parser = argparse.ArgumentParser(description="弹窗切页行为快速探针")
    parser.add_argument("--page", default="wifi", help="验哪一页（默认 wifi）")
    parser.add_argument("--other", default="ble", help="切到哪一页看暂停（默认 ble）")
    parser.add_argument("--wait", type=int, default=90000, help="超时毫秒（默认 90 s）")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    view = QWebEngineView()
    settings = view.settings()
    settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
    settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
    settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebGLEnabled, True)
    settings.setAttribute(QWebEngineSettings.Accelerated2dCanvasEnabled, True)
    view.resize(1200, 800)
    view.show()
    view.load(QUrl.fromLocalFile(os.path.join(WEB, "index.html")))

    def js(code: str, tag: str = "") -> None:
        if not tag:
            view.page().runJavaScript(code, 0)
            return

        def take(result: object) -> None:
            OUT[tag] = str(result)
            print(f"[{tag}] {result}", flush=True)

        view.page().runJavaScript(code, 0, take)

    def open_live() -> None:
        js(
            "(function(){try{const p=document.querySelector('#page-container .panel');"
            "if(!p)return 'no-panel';Popout.open(p);"
            "return JSON.stringify(Popout.state());}catch(e){return 'ERR '+e;}})()",
            "opened",
        )

    def open_static() -> None:
        js(
            "(function(){try{"
            "const p=document.querySelector('#page-container .panel[data-panel-static=\"1\"]');"
            "if(!p)return 'no-static';Popout.open(p);"
            "return JSON.stringify({key:p.dataset.panelKey,st:Popout.state()});"
            "}catch(e){return 'ERR '+e;}})()",
            "static_opened",
        )

    steps = [
        (900, lambda: js(f"Router.navigate('{args.page}')")),
        (900, open_live),
        (300, lambda: js(f"Router.navigate('{args.other}')")),
        (900, lambda: js("JSON.stringify(Popout.state())", "paused_other")),
        (150, lambda: js(f"Router.navigate('{args.page}')")),
        (1000, lambda: js("JSON.stringify(Popout.state())", "resumed")),
        (300, open_static),
        (400, lambda: js(f"Router.navigate('{args.other}')")),
        (900, lambda: js("JSON.stringify(Popout.state())", "static_after_switch")),
        (200, lambda: app.quit()),
    ]
    total = 0
    for delay, fn in steps:
        total += delay
        QTimer.singleShot(total, fn)
    QTimer.singleShot(args.wait, app.quit)  # 兜底：页面加载失败也别吊死

    app.exec()

    def load(tag: str) -> dict:
        try:
            return json.loads(OUT.get(tag) or "{}")
        except Exception:  # noqa: BLE001
            return {}

    opened = load("opened").get("st") or load("opened")
    paused = load("paused_other")
    resumed = load("resumed")
    static_open = (load("static_opened").get("st") or {})
    static_after = load("static_after_switch")

    checks = {
        "opened": bool(opened.get("open")) and not bool(opened.get("paused")),
        "paused": bool(paused.get("open")) and bool(paused.get("paused")) and bool(paused.get("banner")),
        "resumed": bool(resumed.get("open")) and not bool(resumed.get("paused")),
        "static_opened": bool(static_open.get("open")) and bool(static_open.get("static")),
        "static_closed": not bool(static_after.get("open")),
    }
    print("[probe] " + json.dumps(checks, ensure_ascii=False), flush=True)
    print("全部通过" if all(checks.values()) else "有失败项：" + ", ".join(k for k, v in checks.items() if not v),
          flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())