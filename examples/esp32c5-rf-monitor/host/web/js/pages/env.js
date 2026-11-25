/**
 * env.js —— 「哪些信道异常，谁占着它们」。
 *
 * 数据来源分两路，必须分清：
 *   1. 本页表格 = 本机无线网卡解析管理帧得到的 SSID / BSSID / 信道 /
 *      **BSS Load 频道利用率**。这是 AP 自己报的「我有多忙」。
 *   2. 板子那边 = FFT 能量 + 与本表名单做差分（串口 `RD AB?` 出清单）。那是「这个频点
 *      到底有没有人在发射」，跟 AP 自报无关。
 *
 * 两路对照才有诊断价值：
 *   AP 说忙 + 板子测到能量高 → 真的挤
 *   AP 说闲 + 板子测到能量高 → 有别人在同一频点发射（干扰 / 未知设备）
 *   AP 说忙 + 板子测到能量低 → AP 在管自己人（算不上干扰）
 *
 * 诚实边界：路由器会自动换信道（ACS），名单只是快照，过一会儿就不准，
 * 所以按钮旁边写明「建议 60~120 秒重扫重灌」。
 */
(function () {
  "use strict";

  //: last 保存最近一次扫描的完整结果（含 ssid_count 等汇总字段），
  //: 这样开机自扫推来的数据在页面还没打开时不会白丢。
  //: status 同理：自扫往往发生在本页 DOM 注入之前，状态也要缓存一份，否则进页面时
  //: 表格已经满行、状态却还写着「还没扫描」（实测踩过）。
  //: 存的是**语义**（种类 + 参数）不是拼好的文案 —— 存文案的话切语言不会重算，
  //: 英文界面上会留一句中文（自检 en_no_cjk 实测炸过）。
  const state = {
    rows: [],
    freqs: [],
    last: null,
    status: null,
    scrolled: false,
    timer: null,
  };

  function el(id) {
    return document.getElementById(id);
  }

  function t(key, fallback) {
    const i18n = window.I18N;
    if (i18n && typeof i18n.t === "function") {
      const v = i18n.t(key);
      if (v && v !== key) return v;
    }
    return fallback;
  }

  /**
   * 调 Python 的 @Slot。
   *
   * 必须走 window.Bridge.call：QWebChannel 的 backend 对象是 bridge.js 内部
   * 闭包里的私有变量，页面上取不到 window.backend；而且 Bridge.call 已经
   * 替我们把返回的 JSON 字符串 parse 成对象了，别再 parse 一次。
   */
  async function callBackend(method, arg) {
    if (!window.Bridge || typeof Bridge.call !== "function") {
      throw new Error(t("env.noBridge", "桥未就绪（QWebChannel 还没连上）"));
    }
    if (typeof Bridge.ready === "function" && !Bridge.ready()) {
      throw new Error(t("env.noBridge", "桥未就绪（QWebChannel 还没连上）"));
    }
    return arg === undefined ? Bridge.call(method) : Bridge.call(method, arg);
  }

  function fmtUtil(r) {
    // -1 表示 AP 没有上报 BSS Load —— 不能显示成 0%，那是两回事
    if (r.util === null || r.util === undefined || r.util < 0) return "—";
    return r.util + "%";
  }

  function utilClass(r) {
    if (r.util === null || r.util === undefined || r.util < 0) return "muted";
    if (r.util >= 80) return "warn";
    if (r.util >= 50) return "mid";
    return "ok";
  }

  function render() {
    const tb = el("env-tbody");
    if (!tb) return;
    if (!state.rows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="muted">' +
        t("env.empty", "没有扫描到任何 WiFi（本机无线网卡可能被禁用）") + "</td></tr>";
      return;
    }
    const frag = [];
    state.rows.forEach(function (r) {
      // 注意 data-raw 只包「真的从扫描结果里来的名字」：隐藏 SSID 的占位文案是
      // 界面文案，切英文时必须跟着翻，所以那一支**不**带 data-raw。
      const name = r.hidden
        ? '<i class="muted">' + t("env.hiddenSsid", "（隐藏 SSID）") + "</i>"
        : '<span data-raw="1">' + escapeHtml(r.ssid || "") + "</span>";
      frag.push("<tr>" +
        "<td>" + (r.mhz || "—") + "</td>" +
        "<td>" + (r.channel || "—") + "</td>" +
        "<td>" + name + "</td>" +
        '<td class="mono">' + (r.bssid || "—") + "</td>" +
        "<td>" + (r.signal >= 0 ? r.signal + "%" : "—") + "</td>" +
        '<td class="' + utilClass(r) + '">' + fmtUtil(r) + "</td>" +
        "<td>" + (r.stations >= 0 ? r.stations : "—") + "</td>" +
        "</tr>");
    });
    tb.innerHTML = frag.join("");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function renderNotes() {
    const box = el("env-notes");
    if (!box) return;
    const items = [
      t("env.note.1",
        "本表来自本机无线网卡：SSID / BSSID / 信道是解析 802.11 管理帧得到的，不是板子测的。"),
      t("env.note.2",
        "「利用率」是 AP 在 BSS Load 元素里自报的信道占用率，反映这个 AP 有多忙；板子测到的能量反映这个频段有多挤。两者不是一回事。"),
      t("env.note.3",
        "板子没有 WiFi 协议栈，拿不到 SSID。它只能回答「这个频点有能量，而且不在你灌进来的名单里」——那份名单就是板子判「未知」的差分基准。"),
      t("env.note.4",
        "路由器会自动换信道（ACS）。名单是快照，过期后会把换过信道的老 AP 误报成「未知」；单帧结论不可信，持续多轮无主才可信。"),
      t("env.note.5",
        "单天线、无相位信息，只能知道「在这个频点」不能知道「在哪个方向」。要定位只能拿手机走近实测。")
    ];
    box.innerHTML = items.map(function (x) { return '<li class="muted">' + x + "</li>"; }).join("");
  }

  /**
   * 状态行（「正在扫描…」「扫描完成：21 个热点」…）的**渲染**。
   *
   * 缓存里存的是语义（`state.status = {kind, hot, ch, err, n}`）不是拼好的文案：
   * 存文案的话，开机自扫在切英文之前发生 → 缓存里是中文，切英文后 `I18N.apply`
   * 又刷不到它（这个元素为了不被覆盖已经摘掉 data-i18n），英文界面上就留一句中文。
   *
   * 另外两道坑，都实测踩过：
   *   1. 页面片段每次进页面都重新注入 DOM，直接写只活在当前这一份 DOM 里 →
   *      `applyScan` / `onEnter` / `onLang` 都要调 `renderStatus()` 重铺；
   *   2. 这个元素**不能**挂 `data-i18n`：`Router.navigate()` 最后一步是
   *      `I18N.apply(host)`，会把带 data-i18n 的元素无条件改写回词典文案，
   *      于是出现「表格 55 行数据、状态却说还没扫描」。所以 page.html 里它没有
   *      data-i18n，初始文案由 onEnter 写。
   */
  function statusText() {
    const s = state.status;
    if (!s) return "";
    switch (s.kind) {
      case "idle":
        return t("env.act.idle", "还没扫描");
      case "scanning":
        return t("env.scanning", "正在扫描…");
      case "scanned":
        return t("env.scanOk", "扫描完成：") + (s.hot || 0) + t("env.hotspots", " 个热点") +
          " / " + (s.ch || 0) + t("env.channels", " 个频点");
      case "auto":
        return t("env.scanAuto", "已自动扫描：") + (s.hot || 0) + t("env.hotspots", " 个热点") +
          " / " + (s.ch || 0) + t("env.channels", " 个频点");
      case "scanFail":
        return t("env.scanFail", "扫描失败：") + (s.err || "unknown");
      case "pushing":
        return t("env.pushing", "正在下发…");
      case "pushFail":
        return t("env.pushFail", "下发失败：") + (s.err || "unknown");
      case "pushed":
        return t("env.pushOk", "已下发 ") + (s.n || 0) + t("env.pushOk2", " 个频点");
      case "cleared":
        return t("env.clearOk", "固件名单已清空（板子随后会把所有有能量的频点都算成「未知」）");
      default:
        return s.err || "";   // 桥/调用异常：原文照显，不硬套模板
    }
  }

  function renderStatus() {
    const st = el("env-status");
    if (st) st.textContent = statusText();
  }

  function setStatus(kind, params) {
    state.status = kind ? Object.assign({ kind: kind }, params || {}) : null;
    renderStatus();
  }

  /**
   * 把一份扫描结果铺到界面上。doScan（手动/进页面触发）和后端开机自扫
   * 推送（onWifiEnv）共用这一份，避免两边各写一遍写岔。
   */
  function applyScan(res) {
    // 数据先落状态：开机自扫往往在本页被打开之前就到了，缓存住才有意义。
    state.rows = res.rows || [];
    state.freqs = res.freqs || [];
    state.last = res;
    // DOM 可能还没注入（用户不在本页）：这时 el() 一律为 null，
    // 硬写 textContent 会抛 TypeError 打断整条信号链。只缓存，等 onEnter 再铺。
    const ssid = el("env-ssid");
    if (!ssid) return;
    ssid.textContent = res.ssid_count != null ? res.ssid_count : "—";
    el("env-bss").textContent = res.bss_count != null ? res.bss_count : "—";
    el("env-hidden").textContent = res.hidden_count != null ? res.hidden_count : "—";
    el("env-known").textContent = state.freqs.length;
    // 状态行：缓存里有就按**当前语言**重铺（开机自扫时本页 DOM 还不存在，只存了状态）
    renderStatus();
    render();
  }

  async function doScan() {
    setStatus("scanning");
    let res;
    try {
      res = await callBackend("scanWifi");
    } catch (e) {
      setStatus("scanFail", { err: String(e) });
      return;
    }
    if (!res || !res.ok) {
      setStatus("scanFail", { err: (res && res.error) || "unknown" });
      return;
    }
    applyScan(res);
    const pushBtn = el("env-push");
    if (pushBtn) pushBtn.disabled = false;
    setStatus("scanned", { hot: res.ssid_count || 0, ch: state.freqs.length });
  }

  async function doPush() {
    setStatus("pushing");
    let res;
    try {
      res = await callBackend("pushKnown", JSON.stringify({ freqs: state.freqs }));
    } catch (e) {
      setStatus("pushFail", { err: String(e) });
      return;
    }
    if (!res || !res.ok) {
      setStatus("pushFail", { err: (res && res.error) || "unknown" });
      return;
    }
    el("env-known").textContent = res.sent != null ? res.sent : "—";
    const note = el("env-known-note");
    if (note) {
      note.textContent = res.full
        ? t("env.pushFull", "已达固件上限 48，多余频点被截断")
        : t("env.card.knownNote", "当前自动下发到固件的频点数");
    }
    setStatus("pushed", { n: res.sent || 0 });
  }

  async function doClear() {
    let res;
    try {
      res = await callBackend("pushKnown", JSON.stringify({ freqs: [] }));
    } catch (e) {
      setStatus("pushFail", { err: String(e) });
      return;
    }
    el("env-known").textContent = "0";
    setStatus("cleared");
    if (!res || !res.ok) return;
  }

  // ---- 自动监测模式 ----------------------------------------------------
  // 后端 WatchWorker 在后台按周期扫 WiFi + 下发已知名单；这里只做三件事：
  // 拉状态、画倒计时、把模式/间隔传下去。倒计时每秒拉一次，不要在本地做
  // 减法 —— 用户改了周期、或后端刚好插了一轮，本地计数会立刻对不上。
  const watchState = { mode: "off", period_s: 0, next_in_s: 0, rounds: 0, detail: "" };

  async function refreshWatch() {
    try {
      const st = await callBackend("watchStatus");
      if (st && typeof st === "object") {
        watchState.mode = st.mode || "off";
        watchState.period_s = st.period_s || 0;
        watchState.next_in_s = st.next_in_s || 0;
        watchState.rounds = st.rounds || 0;
        watchState.detail = st.detail || "";
        watchState.last_freqs = st.last_freqs || 0;
        watchState.last_error = st.last_error || "";
      }
    } catch (e) {
      watchState.mode = "off";
    }
    renderWatch();
  }

  function fmtLeft(sec) {
    if (!sec || sec <= 0) return "0s";
    if (sec < 60) return Math.round(sec) + "s";
    const m = Math.floor(sec / 60);
    const r = Math.round(sec % 60);
    return r ? m + "m" + r + "s" : m + "m";
  }

  function renderWatch() {
    const st = el("env-watch-state");
    const dt = el("env-watch-detail");
    const notes = el("env-watch-notes");
    const offline = watchState.mode === "off";
    const label = offline
      ? t("env.watch.stateOff", "已关闭")
      : (watchState.mode === "burst"
          ? t("env.watch.stateBurst", "狂暴中")
          : t("env.watch.stateDaily", "日常监测中"));
    if (st) {
      st.textContent = offline
        ? label
        : label + " · " + t("env.watch.next", "下一轮") + " " + fmtLeft(watchState.next_in_s)
            + " · " + t("env.watch.rounds", "已检查") + " " + watchState.rounds;
    }
    if (dt) dt.textContent = watchState.detail || "—";
    // 回填当前生效的间隔。不回填的话，把它改成 60 秒再刷新页面，
    // 框里又变回 HTML 写死的值，接着点任意模式按钮就把旧值写回去，
    // 用户的自定义静默丢失。只在输入框没焦点时写，免得轮询抹掉正在输入的数字。
    const iv = el("env-interval");
    if (iv && document.activeElement !== iv && watchState.interval_s
        && String(iv.value) !== String(watchState.interval_s)) {
      iv.value = watchState.interval_s;
    }
    ["off", "daily", "burst"].forEach(function (m) {
      const b = el("env-mode-" + m);
      if (!b) return;
      b.classList.toggle("btn--primary", watchState.mode === m);
    });
    if (notes) {
      const items = [];
      if (watchState.last_freqs) {
        items.push(t("env.watch.lastFreqs", "上轮下发频点") + "：" + watchState.last_freqs);
      }
      if (watchState.last_error) {
        items.push(t("env.watch.lastErr", "上轮失败") + "：" + watchState.last_error);
      }
      notes.innerHTML = items
        .map(function (x) { return '<li class="muted">' + escapeHtml(x) + "</li>"; })
        .join("");
    }
  }

  async function setMode(mode) {
    const box = el("env-interval");
    const sec = box ? box.value : undefined;
    try {
      // Slot 签名是 setWatch(str)：必须自己 JSON.stringify。
      // 直接传 object 时 Qt 侧收不到 str 形参，调用会静默失败（模式永远切不过去）。
      await callBackend("setWatch", JSON.stringify({ mode: mode, interval_s: sec }));
    } catch (e) { /* 状态条会显示为 off */ }
    await refreshWatch();
  }

  async function watchNow() {
    try {
      await callBackend("watchNow");
    } catch (e) { /* 下一拍自然会重试 */ }
    await refreshWatch();
  }

  function bind() {
    const s = el("env-scan");
    const p = el("env-push");
    const c = el("env-pushclear");
    if (s) s.addEventListener("click", doScan);
    if (p) p.addEventListener("click", doPush);
    if (c) c.addEventListener("click", doClear);
    ["off", "daily", "burst"].forEach(function (m) {
      const b = el("env-mode-" + m);
      if (b) b.addEventListener("click", function () { setMode(m); });
    });
    const n = el("env-now");
    if (n) n.addEventListener("click", watchNow);
  }

  window.Pages = window.Pages || {};
  window.Pages.env = {
    // 钩子名必须是 onEnter / onLeave —— router.js 调的是这两个，
    // 写成 mount/unmount 的话永远不会被调到，按钮就静默无响应。
    onEnter: function () {
      // 页面片段是每次进页面重新注入 DOM 的，按钮监听必须在这里重挂
      state.scrolled = false;
      bind();
      renderNotes();
      render();
      // 状态行没有 data-i18n（挂上就会被 Router.navigate 末尾那次 I18N.apply 刷回
      // 初始值，见 setStatus 注释），所以初始文案得自己按当前语言写一次。
      if (!state.status) state.status = { kind: "idle" };
      renderStatus();
      // 监控状态行同理，refreshWatch 立刻跑一次把它填上（不等第一个 1s tick）
      refreshWatch();
      // 进页面还没有数据就自己扫一次：后端开机自扫可能失败（非 Windows、
      // 没无线网卡），或者这一页是在自扫结束之后才第一次打开的。
      // 有开机自扫的缓存就先铺上（立刻有数，不用等 4 秒扫描）；
      // 什么都没有才自己扫一次。要刷新时点「扫描 WiFi 环境」。
      if (state.last) applyScan(state.last);
      else if (!state.rows.length) doScan();
      // 只在页面可见时轮询：切走就停，别让隐藏页每秒发一次 IPC
      if (state.timer) clearInterval(state.timer);
      state.timer = setInterval(refreshWatch, 1000);
    },
    onLeave: function () {
      if (state.timer) clearInterval(state.timer);
      state.timer = null;
    },
    // 后端开机自扫推来的结果。可能在本页被打开之前就到，所以只缓存数据、
    // 立刻渲染即可（DOM 不在也没关系：render() 里每个 el() 都判空）。
    onWifiEnv: function (data) {
      if (!data || !data.ok) return;
      applyScan(data);
      setStatus("auto", { hot: data.ssid_count || 0, ch: state.freqs.length });
    },
    // 语言变了：状态行是按 t() 现拼的，重算一遍（否则留旧语言文案）
    onLang: function () {
      renderStatus();
    },
    update: function () { }
  };
})();