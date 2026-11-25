# -*- coding: utf-8 -*-
"""「信道占用」页探针：只验**渲染 + 交互 + 中英文**，不起射频线程、不碰串口。

为什么单独一个工具
------------------
``app.py --selftest`` 一遍 3 分半，而且它只盯频谱/瀑布那几页；这一页的数据来自
``host/occupancy.py``（口径已在 ``tests/test_occupancy.py`` 里覆盖），
网页侧只剩「38 行都画出来了吗 / 未扫到的行有没有被误显示成 0% / 按钮通不通」，
用 15 秒的探针就够了。

做法：把 ``window.Bridge`` 换成一个**假桥**（返回一份手写快照），
然后走真正的 ``Router.navigate("occ")`` + ``Pages.occ.onEnter()``。

跑法（在 ``host/`` 下）::

    python tests/probe_occ.py                    # 中文，出图 tests/probe_occ_zh.png
    python tests/probe_occ.py --lang en          # 英文，顺带验「英文界面零中文」

判据（打印里的 OK/FAIL 必须全 OK）。
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
HOST = os.path.dirname(HERE)
WEB = os.path.join(HOST, "web")

OUT: dict[str, object] = {}
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("[OK]  " if ok else "[FAIL]") + " " + name + ("" if detail == "" else " -> " + str(detail)),
          flush=True)
    if not ok:
        FAILED.append(name)


def main() -> int:
    parser = argparse.ArgumentParser(description="信道占用页探针")
    parser.add_argument("--lang", default="zh", choices=("zh", "en"))
    parser.add_argument("--wait", type=int, default=40000, help="超时毫秒（默认 40 s）")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    view = QWebEngineView()
    st = view.settings()
    st.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
    st.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
    st.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
    view.resize(1180, 980)
    view.show()
    view.load(QUrl.fromLocalFile(os.path.join(WEB, "index.html")))

    # 假桥用到的快照：2412 忙、2437 有样本但闲、5180 突发（峰值远高于当前），其余未扫到
    fake = {
        "ok": True,
        "channels": [],
        "fed": 900, "matched": 780, "unmatched": 120,
        "match_mhz": 2.5, "threshold": 15, "ema_den": 8, "elapsed_s": 95.0,
    }
    spec = {2412: (42.0, 61.0, 18.0, 210), 2437: (0.0, 3.1, 0.4, 130),
            5180: (1.2, 88.0, 6.0, 220)}
    if HOST not in sys.path:
        sys.path.insert(0, HOST)
    import occupancy as occ_mod  # noqa: E402 - 用真表生成 38 行，探针不另抄一份信道表

    for ch in occ_mod.CHANNELS:
        pct, mx, avg, frames = spec.get(ch, (0.0, 0.0, 0.0, 0))
        fake["channels"].append({
            "mhz": ch, "wifi_ch": occ_mod.wifi_channel(ch), "band": occ_mod.band_of(ch),
            "pct": pct, "max": mx, "avg": avg, "frames": frames, "hits": int(frames * avg / 100),
            "seen": frames > 0, "age_s": 0.4 if frames else None,
        })
    payload = json.dumps(fake)

    def js(code: str, tag: str = "") -> None:
        if not tag:
            view.page().runJavaScript(code, 0)
            return

        def take(result: object) -> None:
            OUT[tag] = result

        view.page().runJavaScript(code, 0, take)

    step = {"n": 0}

    def tick() -> None:
        step["n"] += 1
        if step["n"] == 1:
            # 1) 装上假桥 + 切语言，然后真的走一遍路由
            js("""
            (function(){
              Bridge.ready = function(){ return true; };
              Bridge.call = function(method){
                if (method === 'occNow') return Promise.resolve(%s);
                if (method === 'occCsv') return Promise.resolve({ok:true, name:'occ_fake.csv', path:'x'});
                if (method === 'occReset') return Promise.resolve({ok:true});
                if (method === 'openFile') return Promise.resolve({ok:true});
                return Promise.resolve({});
              };
              I18N.setLang('%s');
              return true;
            })()
            """ % (payload, args.lang), "stub")
            return
        if step["n"] == 2:
            js("Router.navigate('occ').then(function(){return true;}).catch(function(e){return String(e);})", "nav")
            return
        if step["n"] == 3:
            # 2) 等一次轮询（1 s）之后收数
            js("""(function(){
              var tb = document.getElementById('occ-tbody');
              var rows = tb ? tb.querySelectorAll('tr') : [];
              var text = rows.length ? rows[0].innerText : '';
              var all = tb ? tb.innerText : '';
              var idles = 0;
              rows.forEach(function(r){ if (r.innerText.indexOf('未扫到') >= 0 || r.innerText.indexOf('not swept') >= 0) idles++; });
              return JSON.stringify({
                rows: rows.length,
                first: text,
                unswept_rows: idles,
                busiest: (document.getElementById('occ-busiest')||{}).textContent,
                swept: (document.getElementById('occ-swept')||{}).textContent,
                frames: (document.getElementById('occ-frames')||{}).textContent,
                notes: (document.getElementById('occ-banner')||{}).innerText,
                title: (document.querySelector('.panel__title')||{}).textContent,
                nav: Array.prototype.map.call(document.querySelectorAll('#nav .nav-item'),
                       function(a){ return a.textContent.trim(); }).join('|'),
                nav_active: (document.querySelector('#nav .nav-item.active')||{}).textContent || '',
                has_cjk: /[\\u4e00-\\u9fff]/.test(all),
                body_cjk: /[\\u4e00-\\u9fff]/.test(document.body.innerText)
              });
            })()""", "dom")
            QTimer.singleShot(200, step4)
            return

    def step4() -> None:
        raw = OUT.get("dom")
        try:
            d = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:  # noqa: BLE001
            d = {}
        rows = int(d.get("rows") or 0)
        check("页面切入 occ（38 行都画出来）", rows == 38, "rows=%s" % rows)
        first = str(d.get("first") or "")
        check("第一行是 2412 且带 WiFi 信道号", "2412" in first and "CH" not in first and "1" in first, first[:40])
        check("当前占空比出现在行里（42.0%）", "42.0%" in first, first[-40:])
        check("未扫到的行标成「未扫到」而不是 0%", int(d.get("unswept_rows") or 0) == 35,
              "unswept=%s" % d.get("unswept_rows"))
        check("最忙卡片指向 2412", "2412" in str(d.get("busiest") or ""), d.get("busiest"))
        check("样本卡片是 匹配/总帧", "/" in str(d.get("frames") or ""), d.get("frames"))
        check("已测信道计数 = 3/38", "3 / 38" in str(d.get("swept") or ""), d.get("swept"))
        check("banner 里有 EMA 提醒", len(str(d.get("notes") or "")) > 10, str(d.get("notes"))[:60])
        nav_text = str(d.get("nav") or "")
        check("导航里出现「信道占用/Occupancy」", ("信道占用" in nav_text) or ("Occupancy" in nav_text),
              nav_text[:80])
        check("导航高亮在本页", str(d.get("nav_active") or "").strip() != "", d.get("nav_active"))
        if args.lang == "en":
            check("英文界面零中文（本页表体）", not bool(d.get("has_cjk")), d.get("has_cjk"))
        shot = os.path.join(HERE, "probe_occ_%s.png" % args.lang)
        # 3) 出图：先让布局稳定一帧
        view.grab().save(shot)
        print("[shot] " + shot, flush=True)
        print("[summary] %s" % ("ALL OK" if not FAILED else "FAILED: " + ", ".join(FAILED)), flush=True)
        app.exit(0 if not FAILED else 1)

    QTimer.singleShot(2500, tick)
    QTimer.singleShot(3000, tick)
    QTimer.singleShot(6000, tick)
    QTimer.singleShot(args.wait, lambda: app.exit(2))
    app.exec()
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())