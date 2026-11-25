/**
 * round.js —— 「圆屏轮播」：控制真板固件的显示层自动循环翻页（RD VIEWSEC）。
 *
 * 链路：按钮 → backend.roundControl（Slot，只写意图）→ 射频线程两次扫描之间
 * 发 `RD VIEWSEC a b c` → 发完立刻读回 `RD VIEWSEC?` 验证 → 状态显示。
 * 全 0 = 关轮播；秒数 0 = 跳过该页。
 *
 * 本页不需要射频数据推送，只要 Slot 的同步返回值，所以没有 dispatch/订阅。
 */
(function () {
  "use strict";

  const state = { last: null };

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

  async function callBackend(method, arg) {
    if (!window.Bridge || typeof Bridge.call !== "function") {
      throw new Error(t("round.noBridge", "桥未就绪（QWebChannel 还没连上）"));
    }
    return arg === undefined ? Bridge.call(method) : Bridge.call(method, arg);
  }

  function readSecs() {
    return [0, 1, 2].map((i) => {
      const input = el("round-sec" + i);
      const v = parseInt(input && input.value, 10);
      return Number.isFinite(v) ? Math.max(0, Math.min(255, v)) : 0;
    });
  }

  function render(res) {
    const box = el("round-state");
    const notes = el("round-notes");
    if (!box || !notes) return;
    state.last = res || state.last;
    const r = state.last;
    if (!r) {
      box.textContent = "—";
      return;
    }
    // 状态行：板上确认的秒数 + 开关态 + 落地/确认计数
    const secs = Array.isArray(r.secs) ? r.secs.join(" / ") : "—";
    const onTxt = r.on ? t("round.on", "轮播运行中") : t("round.off", "轮播已关");
    const acked = Number.isFinite(r.acked) ? r.acked : 0;
    const sent = Number.isFinite(r.sent) ? r.sent : 0;
    box.textContent = t("round.stateFmt", "板子秒数 {s}（{on}）· 已下发 {n} 次 / 读回确认 {a} 次")
      .replace("{s}", secs)
      .replace("{on}", onTxt)
      .replace("{n}", String(sent))
      .replace("{a}", String(acked));
    // 提示条：message / note / ok=false；另加一条简单提醒——
    // 轮播命令随采集间隙下发，没在采集时点了不生效（用户实测“停止不了”的主场景）
    const items = [];
    if (r.ok === false && r.note) items.push(r.note);
    if (r.message) items.push(r.message);
    if (r.note && r.ok !== false) items.push(r.note);
    if (!r.acked) items.push(t("round.hintNeedRun",
      "提醒：轮播命令随采集间隙发上板子——若点了没生效，先点顶栏「开始采集」再试，或点「读回板子状态」确认。"));
    notes.innerHTML = items.length
      ? items.map((s) => "<li>" + String(s) + "</li>").join("")
      : "";
    // 落地成功后把输入框同步成板子确认值（读回优先，防止手输错没被纠正）
    if (Array.isArray(r.secs) && r.secs.length === 3) {
      [0, 1, 2].forEach((i) => {
        const input = el("round-sec" + i);
        if (input && document.activeElement !== input) input.value = r.secs[i];
      });
    }
  }

  async function send(payload) {
    try {
      const res = await callBackend("roundControl", JSON.stringify(payload));
      render(res);
    } catch (err) {
      render({
        kind: "round",
        ok: false,
        note: String(err && err.message ? err.message : err),
      });
    }
  }

  function bind() {
    const start = el("round-start");
    const stop = el("round-stop");
    const query = el("round-query");
    if (!start || start.dataset.bound) return;
    start.dataset.bound = "1";
    start.addEventListener("click", () => send({ action: "start", secs: readSecs() }));
    stop.addEventListener("click", () => send({ action: "stop" }));
    query.addEventListener("click", () => send({ action: "status" }));
    // 进页面先读一次板上状态（未连接时 Slot 会返回 not_connected，照常显示）
    send({ action: "status" });
  }

  window.Pages = window.Pages || {};
  // 钩子名必须是 onEnter / onLeave —— router.js 调的是这两个；语言切换走 onLang
  window.Pages.round = {
    onEnter: function () {
      bind();
    },
    onLang: function () {
      render(null);
    },
  };
})();
