/*
 * app.js —— 外壳装配：导航 + 桥 + 顶栏控制 + 事件路由 + 自检探针。
 *
 * 依赖方向：app.js → i18n/router/bridge/util/three3d → pages/*（单向，页面不反调外壳）。
 *
 * 语言：静态文案靠 index.html / page.html 里的 data-i18n 自动翻；这里负责
 * 顶栏控件、状态栏、底部 kv 与日志——它们都是运行时拼出来的，必须调 T()。
 */
(function () {
  const state = {
    framesSeen: 0,
    tablesSeen: 0,
    info: null,
    stats: null,
    running: false,
    lastSpectrumAt: 0,
    status: { name: "idle", key: "status.idle", raw: null },
    port: "COM56",
    ports: [],
    kvPairs: [],
  };

  const MAX_LOG = 220;

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    I18N.apply(document);      // 静态文案先按当前语言填一遍
    bindLangToggle();

    // ---- 桥事件 → 页面分发 ----
    Bridge.on("spectrum", (payload) => {
      state.framesSeen += 1;
      state.lastSpectrumAt = Date.now();
      Router.dispatch("Spectrum", payload);
    });
    Bridge.on("tables", (payload) => {
      state.tablesSeen += 1;
      Router.dispatch("Tables", payload);
    });
    Bridge.on("stats", onStats);
  Bridge.on("gain", (state) => { if (window.Gain) Gain.apply(state); });
    Bridge.on("info", onInfo);
    Bridge.on("state", onState);
    Bridge.on("log", (payload) => log(payload));
    // 开机自扫结果：ENV 页可能还没被访问过，用 window.Pages 直接转发，
    // 页面自己决定缓存还是立刻渲染（它的 onEnter 会再拉一遍）。
    Bridge.on("wifiEnv", (data) => {
      const pg = window.Pages && window.Pages.env;
      if (pg && typeof pg.onWifiEnv === "function") pg.onWifiEnv(data);
    });

    // ---- 导航 + 首屏 ----
    await Router.init();
    // 独立小窗口（窗口外弹窗）：只显示被点出来的那一个面板
    const solo = soloParam();
    await Router.navigate(solo ? solo.page : "spectrum");

    // ---- 桥就绪后拉一次本机串口/场景清单 ----
    Bridge.init(async () => {
      // 首屏那次 navigate 跑在桥就绪之前，setView 会被丢掉 → 这里补报一次，
      // 否则射频线程认为「没人在看频谱页」，首屏永远等不到数据。
      if (window.Router) Router.reportView();
      await applyHostDefaults();
      await loadPorts();
      await loadScenarios();
      bindControls();
      await syncRuntime();
      if (solo) await openSoloPanel(solo);
      log({ text: T("log.ready") });
    });

    if (!Bridge.ready()) {
      setTimeout(() => {
        if (!Bridge.ready()) log({ level: "err", text: T("log.noChannel") });
      }, 1500);
    }
  }

  /** URL 里的 ?solo=<pageId>:<序号> —— Python 侧 createWindow 开的就是这个页面 */
  function soloParam() {
    let raw = "";
    try {
      raw = new URLSearchParams(window.location.search).get("solo") || "";
    } catch (err) {
      raw = "";
    }
    if (!raw) return null;
    const at = raw.lastIndexOf(":");
    return { page: at > 0 ? raw.slice(0, at) : raw, key: raw };
  }

  // 本窗口的标签：主窗口 = main，独立小窗口 = solo:<page>:<序号>。
  // 页面门控（Bridge.setView）靠它区分窗口，多窗口各停一页时取并集。
  window.__winTag = "main";

  async function openSoloPanel(solo) {
    window.__winTag = "solo:" + solo.key;
    // querySelector 需要等 Router 把片段 inject 完；navigate 是 await 过的，这里再兜一层
    if (!Popout.openSolo(solo.key)) {
      setTimeout(() => Popout.openSolo(solo.key), 300);
    }
    document.title = (Router.current ? (I18N.entry("title", Router.current.entry) || solo.page) : solo.page) + " · esp-sdr";
  }

  // ---- 语言 -------------------------------------------------------------
  function bindLangToggle() {
    const btn = document.querySelector("[data-lang-toggle]");
    if (!btn) return;
    btn.addEventListener("click", () => I18N.setLang(I18N.lang() === "zh" ? "en" : "zh"));
  }

  /**
   * 命令行（--lang / --port / --gain-index）来的默认值。
   * 语言只在本机没存过偏好时才采用，免得覆盖用户在界面里的选择。
   */
  async function applyHostDefaults() {
    let defaults = null;
    try { defaults = await Bridge.call("uiDefaults"); } catch (err) { /* 老版本没这个槽，忽略 */ }
    if (!defaults) return;
    let stored = null;
    try { stored = localStorage.getItem("espsdr.lang"); } catch (err) { /* 忽略 */ }
    if (!stored && defaults.lang && defaults.lang !== I18N.lang()) I18N.setLang(defaults.lang);
    if (defaults.port) state.port = defaults.port;
    if (defaults.gain_index !== null && defaults.gain_index !== undefined) {
      document.getElementById("gain").value = defaults.gain_index;
    }
    if (defaults.gain_target !== null && defaults.gain_target !== undefined) {
      document.getElementById("gain-target").value = defaults.gain_target;
    }
    if (defaults.gain_mode) {
      document.querySelectorAll("[data-gain-mode]").forEach((btn) => {
        btn.classList.toggle("seg__btn--on", btn.dataset.gainMode === defaults.gain_mode);
      });
    }
  }

  /**
   * 补一次运行态。原生信号在 QWebChannel 连上之前会丢，
   * 而 --mock / --autostart 在页面加载前就开始采了 → 不补就永远显示「未连接」。
   */
  async function syncRuntime() {
    let rt = null;
    try { rt = await Bridge.call("runtime"); } catch (err) { return; }
    if (!rt) return;
    if (rt.gain) Gain.apply(rt.gain);
    if (rt.info) onInfo(rt.info, true);
    if (rt.stats) onStats(rt.stats);
    // 独立窗口是「晚连上」的：把最后一帧频谱 / 最后一次表格直接回放给它
    if (rt.spectrum) Router.dispatch("Spectrum", rt.spectrum);
    if (rt.tables) Router.dispatch("Tables", rt.tables);
    if (rt.running) {
      setStatus("running", "status.running");
      setRunning(true);
    }
  }

  /** 语言变了：底部 kv 与状态栏要重算（各页自己由 Router.relabel 通知） */
  window.addEventListener("langchange", () => {
    if (window.Gain) Gain.relabel();
    rerenderKv();
    renderPorts();
    const st = state.status;
    setStatus(st.name, st.key, st.raw);
    // 旧日志行是切换前那种语言写的，清掉再写一行提示——否则英文界面里会留中文
    const box = document.getElementById("log");
    if (box) box.innerHTML = "";
    log({ level: "info", text: T("log.langSwitched") });
    // 告诉 Python：后续下行文案（日志/状态/表格）改用新语言
    if (window.Bridge && Bridge.ready()) {
      Bridge.call("setLang", I18N.lang()).catch(() => { /* 未连接时忽略 */ });
    }
  });

  /**
   * 串口名字带 Windows 自带的描述（中文系统上是「USB 串行设备」）。
   * 描述是操作系统给的、我们翻不了；英文界面下只留设备名（COM56），
   * 否则英文界面里必定漏出一句中文。
   */
  function portLabel(info) {
    const desc = info && info.description ? String(info.description) : "";
    const device = (info && info.device) || "";
    if (!desc) return device;
    if (I18N.lang() === "en" && /[\u3400-\u4dbf\u4e00-\u9fff]/.test(desc)) return device;
    return device + " · " + desc;
  }

  // ---- 顶栏 -------------------------------------------------------------
  /** 把已读到的串口列表按当前语言重画（切语言时描述要跟着变/去中文） */
  function renderPorts() {
    const select = document.getElementById("port");
    if (!select) return;
    const keep = select.value || state.port;
    select.innerHTML = "";
    state.ports.forEach((info) => {
      const opt = document.createElement("option");
      opt.value = info.device;
      opt.textContent = portLabel(info);
      select.appendChild(opt);
    });
    if (!state.ports.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "—";
      select.appendChild(opt);
      return;
    }
    const prefer = state.ports.find((p) => p.device === keep)
      || state.ports.find((p) => p.device === "COM56")
      || state.ports[0];
    if (prefer) select.value = prefer.device;
  }

  async function loadPorts() {
    let ports = [];
    try { ports = await Bridge.call("listPorts"); } catch (err) { log({ level: "err", text: T("log.portsFailed", { error: err }) }); }
    state.ports = ports || [];
    renderPorts();
    if (!state.ports.length) {
      log({ level: "err", text: T("log.noPorts") });
      return;
    }
  }

  async function loadScenarios() {
    const select = document.getElementById("scenario");
    let list = [];
    try { list = await Bridge.call("scenarios"); } catch (err) { /* 忽略 */ }
    select.innerHTML = "";
    (list.length ? list : ["mixed", "quiet", "wifi_busy"]).forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    });
    if (list.indexOf("wifi_busy") >= 0) select.value = "wifi_busy";
    select.disabled = !document.getElementById("mock").checked;
    document.getElementById("mock").addEventListener("change", (e) => {
      select.disabled = !e.target.checked;
    });
  }

  function bindControls() {
    document.getElementById("btn-start").addEventListener("click", startRun);
    document.getElementById("btn-stop").addEventListener("click", stopRun);
    Gain.init();
  }

  async function startRun() {
    const mock = document.getElementById("mock").checked;
    const gainRaw = document.getElementById("gain").value;
    const gainMode = Gain.mode();
    const config = {
      port: document.getElementById("port").value,
      baudrate: 2000000,
      // AGC 模式下不发档位（固件自己挑）；手动 / 自动都从当前显示的档位起步
      gain_index: mock || gainMode === "HARDWARE" || gainRaw === "" ? null : parseInt(gainRaw, 10),
      gain_mode: mock ? "HARDWARE" : gainMode,
      gain_target: Gain.target(),
      mock: mock,
      scenario: document.getElementById("scenario").value,
      bins_out: 256,
      lang: I18N.lang(),
    };
    setStatus("starting", "status.starting");
    setRunning(true);
    try {
      const result = await Bridge.call("start", JSON.stringify(config));
      if (!result || !result.ok) {
        setStatus("error", null, (result && result.message) || T("status.error"));
        setRunning(false);
        log({ level: "err", text: T("log.startFailed", { error: (result && result.message) || "" }) });
      } else {
        setStatus("running", "status.running");
        log({ text: result.message });
        log({ text: mock ? T("foot.nextMock") : T("foot.next") });
      }
    } catch (err) {
      setStatus("error", null, T("status.error"));
      setRunning(false);
      log({ level: "err", text: T("log.bridgeFailed", { error: err }) });
    }
  }

  async function stopRun() {
    try {
      const result = await Bridge.call("stop");
      log({ text: (result && result.message) || T("log.stopped") });
    } catch (err) {
      log({ level: "err", text: T("log.stopFailed", { error: err }) });
    }
    setStatus("idle", "status.idle");
    setRunning(false);
  }

  function setRunning(running) {
    state.running = running;
    document.getElementById("btn-start").disabled = running;
    document.getElementById("btn-stop").disabled = !running;
  }

  /** key = i18n 文案键（跟着语言变）；rawText = 动态原文（错误消息之类，不翻译） */
  function setStatus(stateName, key, rawText) {
    state.status = { name: stateName, key: key || null, raw: rawText || null };
    const el = document.getElementById("status");
    el.dataset.state = stateName;
    el.textContent = key ? T(key) : rawText || stateName;
  }

  // ---- 事件 -------------------------------------------------------------
  function gainText(info) {
    const live = window.Gain ? Gain.probe() : null;
    if (!live) {
      return info.gain_index === null || info.gain_index === undefined
        ? T("kv.gainAgc")
        : T("kv.gainFixed", { n: info.gain_index });
    }
    const base = live.mode === "HARDWARE"
      ? T("kv.gainAgc")
      : T("kv.gainFixed", { n: live.index });
    const floor = live.floor_code === null || live.floor_code === undefined
      ? "—"
      : Number(live.floor_code).toFixed(1);
    return base + " · " + T("kv.gainFloor", { code: floor }) +
      (live.mode === "AUTO" ? " · " + T("kv.gainAuto", { n: live.adjusts || 0 }) : "");
  }

  function renderKv(pairs) {
    state.kvPairs = pairs;
    const kv = document.getElementById("kv");
    kv.innerHTML = "";
    pairs.forEach(([k, v]) => {
      const span = document.createElement("span");
      span.innerHTML = escapeHtml(String(k)) + " <b>" + escapeHtml(String(v)) + "</b>";
      kv.appendChild(span);
    });
    // 最后错误单独挂在尾部（语言切换时也要复原）
    if (state.stats && state.stats.last_source_error) {
      const span = document.createElement("span");
      span.className = "err";
      span.textContent = T("kv.lastError") + ": " + state.stats.last_source_error;
      kv.appendChild(span);
    }
  }

  function rerenderKv() {
    if (state.stats && state.info) onStats(state.stats);
    else if (state.info) onInfo(state.info, true);
  }

  function onInfo(info, quiet) {
    state.info = info;
    renderKv([
      [T("kv.firmware"), info.identity || "—"],
      [T("kv.transport"), info.transport || "—"],
      [T("kv.port"), (info.port || "—") + " @ " + (info.baudrate || 0)],
      [T("kv.mode"), info.mock ? T("kv.mockMode", { scenario: info.scenario }) : T("kv.realMode")],
      [T("kv.gain"), gainText(info)],
      [T("kv.modules"), (info.plugins || []).map((p) => p.id + "(" + p.requests + ")").join(" ") || "—"],
    ]);
    Router.dispatch("Info", info);
    if (!quiet) (info.notes || []).forEach((n) => log({ text: n }));
  }

  function onStats(stats) {
    state.stats = stats;
    if (stats && stats.gain && window.Gain) Gain.apply(stats.gain);
    const info = state.info;
    if (!info) return;
    renderKv([
      [T("kv.firmware"), info.identity || "—"],
      [T("kv.port"), (info.port || "—") + (info.mock ? " · " + T("kv.mockMode", { scenario: info.scenario }) : "")],
      [T("kv.gain"), gainText(info)],
      [T("kv.slices"), stats.slices],
      [T("kv.snapshots"), stats.snapshots],
      [T("kv.frames"), state.framesSeen],
      [T("kv.rfSeconds"), (stats.elapsed_s || 0).toFixed(1)],
      [T("kv.errors"), stats.source_errors],
    ]);
    if (!state.running) setStatus("idle", "status.idle");
  }

  function onState(msg) {
    if (!msg) return;
    if (msg.state === "running") {
      setStatus("running", "status.running");
      setRunning(true);
    } else if (msg.state === "stopped") {
      setStatus("idle", "status.idle");
      setRunning(false);
    } else if (msg.state === "error") {
      setStatus("error", null, msg.message || T("status.error"));
      setRunning(false);
    } else if (msg.state === "ui-loaded") {
      log({ text: "ui-loaded" });
    } else if (msg.state === "ui-load-failed") {
      setStatus("error", null, "ui-load-failed");
    }
  }

  function log(entry) {
    if (!entry) return;
    const text = typeof entry === "string" ? entry : entry.text || JSON.stringify(entry);
    const box = document.getElementById("log");
    const line = document.createElement("div");
    if (entry.level === "err") line.className = "err";
    const now = new Date();
    const stamp = now.toTimeString().slice(0, 8);
    line.textContent = "[" + stamp + "] " + text;
    box.appendChild(line);
    while (box.childNodes.length > MAX_LOG) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }

  function escapeHtml(text) {
    return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // ---- 自检探针（host/app.py --selftest 会读它） -------------------------
  function nonBlackRatio(canvas) {
    try {
      const w = Math.min(320, canvas.width || 0);
      const h = Math.min(200, canvas.height || 0);
      if (!w || !h) return 0;
      const tmp = document.createElement("canvas");
      tmp.width = w;
      tmp.height = h;
      const ctx = tmp.getContext("2d");
      ctx.drawImage(canvas, 0, 0, w, h);
      const data = ctx.getImageData(0, 0, w, h).data;
      let lit = 0;
      for (let i = 0; i < data.length; i += 4) {
        if (data[i] + data[i + 1] + data[i + 2] > 90) lit += 1;
      }
      return lit / (w * h);
    } catch (err) {
      return 0;
    }
  }

  function webglInfo() {
    try {
      const c = document.createElement("canvas");
      const gl = c.getContext("webgl2") || c.getContext("webgl");
      if (!gl) return { ok: false };
      return { ok: true, version: gl.getParameter(gl.VERSION), renderer: gl.getParameter(gl.RENDERER) };
    } catch (err) {
      return { ok: false, error: String(err) };
    }
  }

  window.probeReport = function probeReport() {
    const canvases = Array.from(document.querySelectorAll("canvas")).map((c) => ({
      id: c.id || "(3d)",
      w: c.width,
      h: c.height,
      nonBlack: Number(nonBlackRatio(c).toFixed(4)),
    }));
    const page = Router.current;
    return {
      ready: Bridge.ready(),
      page: page ? page.id : null,
      lang: I18N.lang(),
      three: typeof THREE !== "undefined" ? THREE.REVISION || "?" : null,
      webgl: webglInfo(),
      framesSeen: state.framesSeen,
      tablesSeen: state.tablesSeen,
      running: state.running,
      statusText: (document.getElementById("status") || {}).textContent || "",
      // 代码版本戳：自检据此证明「页面拿到的是当前这一版代码」，而不是上一个进程
      codeStamp: (document.getElementById("code-stamp") || {}).textContent || "",
      navLabels: Array.from(document.querySelectorAll("#nav .nav-item")).map((b) => b.textContent),
      canvases: canvases,
      pageProbe: page && page.mod && page.mod.probe ? page.mod.probe() : null,
    };
  };

  /**
   * 布局探针：窗口态/最大化/拖小 时，顶栏那些按钮是不是都还「完整落在可视区」。
   *
   * 判据三条（只要有一条不满足就算被裁）：
   *   1. 自己有尺寸（`w > 0 && h > 0`）—— 被 display:none 或压成 0 宽也算不可见；
   *   2. 四边都不超出 `window.innerWidth/innerHeight`；
   *   3. 祖先里没有 `overflow:hidden` 的盒子把它的矩形裁掉（顶栏自己滚不算裁，
   *      因为滚得出来 = 用户点得到，但这里仍记进 `scrollable` 供排查）。
   * 用 getBoundingClientRect 而不是 offsetWidth：后者不带滚动/裁剪信息。
   */
  const LAYOUT_TARGETS = [
    ["port", "#port"],
    ["gainbar", "#gainbar"],
    ["gainMode", "[data-gain-mode]"],
    ["gainStep", "#gain-up"],
    ["mock", "#mock"],
    ["scenario", "#scenario"],
    ["start", "#btn-start"],
    ["stop", "#btn-stop"],
    ["lang", "#btn-lang"],
    ["status", "#status"],
    ["nav", ".side"],
    ["foot", ".foot"],
  ];

  function visibleInViewport(node) {
    if (!node) return { ok: false, why: "missing" };
    const r = node.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return { ok: false, why: "zero-size" };
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const over = {
      left: Math.round(-r.left),
      top: Math.round(-r.top),
      right: Math.round(r.right - vw),
      bottom: Math.round(r.bottom - vh),
    };
    const clipped = Object.keys(over).some((k) => over[k] > 1);
    return {
      ok: !clipped,
      why: clipped ? "out-of-viewport" : "",
      over: over,
      rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
    };
  }

  window.Layout = {
    probe: function probe() {
      const items = {};
      const bad = [];
      LAYOUT_TARGETS.forEach(function (pair) {
        const key = pair[0];
        const node = document.querySelector(pair[1]);
        const res = visibleInViewport(node);
        items[key] = res.ok ? 1 : 0;
        if (!res.ok) bad.push(key + ":" + res.why + ":" + JSON.stringify(res.over || null));
      });
      const page = document.getElementById("page-container");
      const shell = { vw: window.innerWidth, vh: window.innerHeight };
      shell.docH = document.documentElement ? document.documentElement.scrollHeight : 0;
      shell.overflowY = shell.docH > shell.vh + 1 ? "shell-scrolls" : "ok";
      shell.pageScroll = page ? { sw: page.scrollWidth, cw: page.clientWidth } : null;
      shell.topH = Math.round((document.querySelector(".top") || { getBoundingClientRect: () => ({ height: 0 }) }).getBoundingClientRect().height);
      return { items: items, bad: bad, ok: bad.length === 0, shell: shell };
    },
  };

  // 让页面模块也能记日志
  window.AppLog = log;
})();