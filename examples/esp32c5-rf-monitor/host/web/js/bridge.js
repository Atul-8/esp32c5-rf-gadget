/*
 * bridge.js —— QWebChannel 桥的封装。
 *
 * 约定（与 host/app.py 对齐）：
 *   · Python 的所有信号都发 **JSON 字符串**，这里统一 JSON.parse 后分发；
 *   · JS 调用 Python 的 @Slot 一律返回 Promise（QWebChannel 的机制）；
 *   · 页面只依赖 window.Bridge，不直接碰 qt.webChannelTransport。
 */
window.Bridge = (function () {
  const handlers = {};       // 事件名 → [fn]
  let backend = null;
  let readyCbs = [];

  function on(name, fn) {
    (handlers[name] = handlers[name] || []).push(fn);
  }

  function dispatch(name, payload) {
    (handlers[name] || []).forEach((fn) => {
      try { fn(payload); } catch (err) { console.error("[bridge]", name, err); }
    });
  }

  /** 调 Python 的 Slot：call("start", JSON.stringify(cfg)) → Promise<object> */
  function call(name, ...args) {
    return new Promise((resolve, reject) => {
      if (!backend) return reject(new Error(T("log.noBackend")));
      if (typeof backend[name] !== "function") return reject(new Error(T("log.noSlot", { name: name })));
      backend[name](...args, (result) => {
        if (typeof result === "string") {
          try { return resolve(JSON.parse(result)); } catch (e) { return resolve(result); }
        }
        resolve(result);
      });
    });
  }

  function init(cb) {
    if (typeof qt === "undefined" || !qt.webChannelTransport) {
      dispatch("log", { level: "err", text: T("log.noChannel") });
      return;
    }
    new QWebChannel(qt.webChannelTransport, (channel) => {
      backend = channel.objects.backend;
      // gain：顶栏控件按下去立刻回推一次状态（不用等下一个 stats 周期）
      ["info", "spectrum", "tables", "stats", "gain", "state", "log", "wifiEnv"].forEach((sig) => {
        backend[sig].connect((text) => {
          let data = text;
          try { data = JSON.parse(text); } catch (e) { /* 非 JSON 原样传 */ }
          dispatch(sig, data);
        });
      });
      readyCbs.forEach((fn) => fn());
      readyCbs = [];
      if (cb) cb();
    });
  }

  return {
    init,
    on,
    call,
    ready: () => !!backend,
    onReady: (fn) => (backend ? fn() : readyCbs.push(fn)),
  };
})();