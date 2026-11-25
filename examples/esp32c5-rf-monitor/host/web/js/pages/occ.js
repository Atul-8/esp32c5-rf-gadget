/* pages/occ.js —— 「信道占用统计」：一整张 38 信道的表，能落 CSV。
 *
 * 数据从哪来
 * ----------
 * Python 侧 host/occupancy.py 在**射频线程里**跟着每一帧光谱一直累加（不是本页
 * 打开才算），本页只是每秒来读一次快照（``Bridge.call("occNow")``）。
 * 所以：切到别的页面统计不会断，切回来数字是接上的。
 *
 * 判据（与固件同源，改这里之前先读 host/occupancy.py 的模块注释）
 * ----------------------------------------------------------------
 * 一帧取中间一半 bin，p50 − p25 > 15 码 判「有传输」；占空比 = 命中帧 / 总帧。
 * 三个数字的分工：
 *   · 当前 = 逐帧 EMA（α=1/8，~1 s），看「现在有多热」
 *   · 峰值 = 会话内 EMA 的最高值，认**突发**协议（BLE 广播是尖的）
 *   · 平均 = 累计命中 / 累计帧，看「一直在发射吗」（图传、视频流）
 *
 * 一条不能忘的诚实边界：没扫到的信道显示「—」，**不是 0%**。
 * 宽窗扫频时一帧只代表它中心附近，别的信道压根没被测过；把它们画成 0%
 * 会得出「这些信道很空」的反向结论（板端 RD OCC? 那张表就踩过这个坑）。
 */
window.Pages = window.Pages || {};

window.Pages.occ = (function () {
  "use strict";

  const POLL_MS = 1000;
  //: 排序模式：false = 按频率升序（跟圆屏第二页一致），true = 按当前占用降序
  let sortByPct = false;
  let timer = null;
  let last = null;      // 最近一次快照（切语言要重画，所以缓存下来）
  let status = null;    // {kind, args} —— 存语义不存文案，切语言才重算得出来
  let busy = false;

  function el(id) {
    return document.getElementById(id);
  }

  function t(key, fallback) {
    const i18n = window.I18N;
    if (i18n && typeof i18n.t === "function") {
      const v = i18n.t(key);
      if (v && v !== key) return v;
    }
    return fallback === undefined ? key : fallback;
  }

  function callBackend(method, arg) {
    if (!window.Bridge || typeof Bridge.call !== "function") {
      return Promise.reject(new Error(t("occ.noBridge", "桥未就绪（QWebChannel 还没连上）")));
    }
    if (typeof Bridge.ready === "function" && !Bridge.ready()) {
      return Promise.reject(new Error(t("occ.noBridge", "桥未就绪（QWebChannel 还没连上）")));
    }
    return arg === undefined ? Bridge.call(method) : Bridge.call(method, arg);
  }

  // ---- 渲染 --------------------------------------------------------------
  function cls(pct) {
    if (pct >= 35) return "warn";
    if (pct >= 15) return "mid";
    if (pct >= 5) return "ok";
    return "muted";
  }

  function barHtml(pct) {
    const w = Math.max(0, Math.min(100, pct));
    return '<div class="bar"><i style="width:' + w.toFixed(0) + '%;background:' +
      (w >= 35 ? "#ef4444" : w >= 15 ? "#f59e0b" : "#22c55e") + '"></i></div>';
  }

  function num(v) {
    return (v === null || v === undefined) ? "—" : (Math.round(v * 10) / 10).toFixed(1) + "%";
  }

  function rowsOf(snap) {
    const rows = (snap && snap.channels) || [];
    const copy = rows.slice();
    if (sortByPct) {
      copy.sort(function (a, b) {
        if (b.pct !== a.pct) return b.pct - a.pct;
        return a.mhz - b.mhz;
      });
    }
    return copy;
  }

  function render() {
    const tb = el("occ-tbody");
    if (!tb) return;
    const snap = last;
    const rows = rowsOf(snap);
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="muted">' +
        t("occ.empty", "还没有数据：先在上方点「开始」让板子扫起来") + "</td></tr>";
      return;
    }
    const seen = rows.filter(function (r) { return r.seen; });
    tb.innerHTML = rows.map(function (r) {
      if (!r.seen) {
        // 未测 ≠ 空闲：整行压暗、百分比全给「—」，别让 0% 混进比较里
        return '<tr class="muted"><td>' + r.mhz + ' MHz</td><td>' + (r.wifi_ch || "—") +
          "</td><td>" + r.band + '</td><td>—</td><td>—</td><td>—</td><td>' +
          t("occ.th.unswept", "未扫到") + "</td></tr>";
      }
      return "<tr><td>" + r.mhz + ' MHz</td><td>' + (r.wifi_ch || "—") + "</td><td>" + r.band +
        '</td><td class="' + cls(r.pct) + '">' + num(r.pct) +
        '</td><td class="' + cls(r.max) + '">' + num(r.max) +
        '</td><td class="' + cls(r.avg) + '">' + num(r.avg) +
        "</td><td>" + barHtml(r.pct) + "</td></tr>";
    }).join("");

    // 汇总卡片
    setText("#occ-swept", String(seen.length) + " / " + rows.length);
    const top = seen.slice().sort(function (a, b) { return b.pct - a.pct; })[0];
    setText("#occ-busiest", top && top.pct > 0
      ? top.mhz + " MHz (" + (top.wifi_ch ? "CH" + top.wifi_ch : top.band) + ") " + num(top.pct)
      : t("occ.card.idle", "全都很安静"));
    setText("#occ-frames", snap ? (snap.matched + " / " + snap.fed) : "—");
    setText("#occ-frames-note", snap
      ? t("occ.card.framesNote2", "对上信道的帧 / 进入统计的帧 · 已统计 {s} 秒").replace("{s}",
        String(Math.round(snap.elapsed_s || 0)))
      : t("occ.card.framesNote", "对上信道的帧数 / 进入统计的帧数"));
    renderStatus();
  }

  function setText(sel, text) {
    const node = el(sel.replace("#", ""));
    if (node) node.textContent = text;
  }

  function renderStatus() {
    const node = el("occ-status");
    if (!node) return;
    if (!status) { node.textContent = "—"; return; }
    if (status.kind === "csv") {
      node.textContent = t("occ.st.csv", "已导出 {name}").replace("{name}", status.name || "");
    } else if (status.kind === "reset") {
      node.textContent = t("occ.st.reset", "统计已清零");
    } else if (status.kind === "error") {
      node.textContent = t("occ.st.error", "失败：{error}").replace("{error}", status.error || "");
    } else {
      node.textContent = "—";
    }
  }

  function renderNotes() {
    // 静态说明在 page.html 的 data-i18n 里，Router.navigate 会统一翻；
    // 这里只填 banner 那三条动态提醒（例如硬件 AGC 正在跑）。
    const box = el("occ-banner");
    if (!box) return;
    box.innerHTML = "";
    const items = [t("occ.banner.ema", "「当前」约 1 秒时间常数；板端是逐窗 EMA（τ ≈ 24 秒），两者别逐位比。")];
    if (last && last.unmatched > 0) {
      items.push(t("occ.banner.unmatched", "有 {n} 帧没落到任何已知信道上（宽窗中心之外的频率）：这些帧不计入任何一行。")
        .replace("{n}", String(last.unmatched)));
    }
    items.forEach(function (text) {
      const li = document.createElement("li");
      li.textContent = text;
      box.appendChild(li);
    });
  }

  // ---- 刷新 --------------------------------------------------------------
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const snap = await callBackend("occNow");
      if (snap && snap.ok) {
        last = snap;
        render();
        renderNotes();
      }
    } catch (err) {
      status = { kind: "error", error: (err && err.message) || String(err) };
      renderStatus();
    } finally {
      busy = false;
    }
  }

  function bind() {
    const sortBtn = el("occ-sort");
    if (sortBtn) {
      sortBtn.onclick = function () {
        sortByPct = !sortByPct;
        sortBtn.textContent = sortByPct
          ? t("occ.act.sortFreq", "按频率排序")
          : t("occ.act.sort", "按占用排序");
        render();
      };
      sortBtn.textContent = sortByPct
        ? t("occ.act.sortFreq", "按频率排序")
        : t("occ.act.sort", "按占用排序");
    }
    const csvBtn = el("occ-csv");
    if (csvBtn) {
      csvBtn.onclick = async function () {
        try {
          const res = await callBackend("occCsv");
          if (res && res.ok) {
            status = { kind: "csv", name: res.name };
            if (window.Bridge && res.path) {
              // 顺手用系统默认程序打开：CSV 在 Excel 里看一眼就知道该盯哪个信道
              Bridge.call("openFile", res.path).catch(function () {});
            }
          } else {
            status = { kind: "error", error: (res && res.error) || "unknown" };
          }
        } catch (err) {
          status = { kind: "error", error: (err && err.message) || String(err) };
        }
        renderStatus();
      };
    }
    const resetBtn = el("occ-reset");
    if (resetBtn) {
      resetBtn.onclick = async function () {
        try {
          await callBackend("occReset");
          status = { kind: "reset" };
        } catch (err) {
          status = { kind: "error", error: (err && err.message) || String(err) };
        }
        await refresh();
        renderStatus();
      };
    }
  }

  // ---- 生命周期 ----------------------------------------------------------
  return {
    onEnter: function (host) {
      // 片段每次进页面重新注入，按钮监听必须在这里重挂
      bind();
      renderNotes();
      render();
      refresh();
      // 只在页面可见时轮询：切走就停，别让隐藏页每秒发一次 IPC
      if (timer) clearInterval(timer);
      timer = setInterval(refresh, POLL_MS);
      void host;
    },
    onLeave: function () {
      if (timer) clearInterval(timer);
      timer = null;
    },
    // 语言变了：状态行和 banner 是现拼的，重算一遍（否则留旧语言文案）
    onLang: function () {
      bind();
      renderNotes();
      render();
    },
    update: function () {}
  };
})();