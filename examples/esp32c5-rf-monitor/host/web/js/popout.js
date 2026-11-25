/*
 * popout.js —— 任意面板双击弹出预览（铺满窗口 / 自定义缩放）。
 *
 * 用起来：**双击任意 `.panel`**（或点面板右上角的 ⤢），面板会搬到全窗口的浮层里；
 * 工具栏可以 −/+ 缩放（75%…300%）、重置、全屏、关闭；Esc 也关。
 *
 * 为什么不是 window.open：QWebEngine 里开新窗口要 Python 侧再接
 * QWebEnginePage.createWindow，而且跨窗口传数据还得再搭一层桥。这里用**同一文档内
 * 的浮层 + 原地搬 DOM**，好处是：
 *   · canvas / WebGL 的上下文跟着 DOM 走，搬进搬出都不丢内容，不用重建；
 *   · 页面自己的 resize 钩子就能重画（缩放时把 --fit-scale 换成新的倍率，
 *     再派发一次 window resize），大图是**按新尺寸重新渲染**的，不是拿小图拉伸放大。
 *
 * 缩放的实现：
 * 为什么浮层挂在 `#page-container` 里（而不是 `document.body`）：面板被搬进浮层后，
 * 页面模块的 `root.querySelector("#spec-canvas")` 还得能找到它 —— 一搬出 root，
 * resize 钩子就拿到 null 直接抛错，画面停止重绘（这个坑现场踩过）。浮层是
 * `position: fixed`，挂在哪儿视觉上都一样。
 *
 *   · 面板宽度 = 倍率 × 100%（浮层可滚动），
 *   · 面板上设 `--fit-scale: 倍率`，`U.fitCanvas()` 与 3D 容器高度都乘这个倍率，
 *     所以横纵等比放大，坐标轴文字不会被拉变形。
 *
 * **高度自适应**（弹出/全屏后别留一大片空白）：
 *   · 量一次「面板按倍率渲染完还差多少高度到浮层底」，把这段余量除以会长的画布个数，
 *     写到面板的 `--fit-grow`；标了 `data-fit-grow="1"` 的画布/容器吃掉这段余量。
 *   · 两阶段：先归零 `--fit-grow` 派发 resize（页面按倍率重画）→ 200ms 后量高度 →
 *     写新余量再派发一次 resize。这样不会自己跟自己打架（量的是稳定后的布局）。
 *
 * **切页时弹窗怎么办**（`beforePageChange()` / `afterPageChange()`）：
 *   · 面板是**实时流**（画布/表格随帧刷新）且切去的不是它所属页面 → **不关闭**，
 *     留在浮层里**暂停刷新**，并在浮层顶部挂一条「模式已切换」提示；
 *     切回它所属的页面就自动恢复刷新（页面钩子重新开始给它喂数据）。
 *   · 面板是**汇总型**（判读说明、门槛表、出图按钮）→ 直接关闭。
 *     这类面板只是当时那一份快照的结论，跟着页面走没有意义。
 *   · 暂停为什么是免费来的：路由只把 Bridge 事件派给**当前页**的模块，
 *     面板所属页面不在前台时，它的 `onTables/onSpectrum` 根本不会被调用。
 *
 * **窗口外弹窗**（`openDocked()`）：`window.open('index.html?solo=<页面:序号>')`，
 * Python 侧 `ConsolePage.createWindow` 接住、开一个真正的独立窗口（可以拖到别的
 * 显示器），新窗口加载同一套页面 + **同一个 Bridge**，只显示那一个面板。
 */
window.Popout = (function () {
  const ZOOMS = [0.75, 1, 1.25, 1.5, 2, 3];
  const OVERLAY_CLASS = "popout";

  let overlay = null;
  let stage = null;
  let panel = null;
  let home = null;        // {parent, next} 原位置，关闭时搬回去
  let zoom = 1;
  let resizeTimer = null;
  let growTimer = null;
  let bar = null;
  let solo = false;          // 本窗口就是「独立小窗口」：关掉就没有内容了，不给关
  let fsOn = false;          // 当前是不是（由 Python 侧窗口 API 做的）全屏
  let suppress = 0;          // 自己派发合成 resize 时别自我递归
  let pausedFor = null;      // 暂停中：面板所属页面 id（切回这一页就恢复刷新）
  let closeSeq = 0;          // close() 被调用过几次（排障用）
  let lastCloseTrace = "";   // 最近一次 close() 的调用栈（排障用）
  let activePage = "";       // 当前页面 id（由 Router 在切页时告知）
  let pauseBar = null;       // 「模式已切换」提示条

  // 汇总型面板（**不需要**暂停/恢复那套）：值 = 该页 page.html 里 .panel 的下标顺序。
  // 判读说明、出图按钮这类「当时那一份快照的结论」跟着页面走，切页直接关掉才对；
  // 只有实时流面板（画布/随帧刷新的表）才值得留着暂停。
  // 新页面要加面板时**只改这一张表**，别在 page.html 里散落标记。
  // 注：wifi / ble / capture 三页已删，对应配置一并清掉。
  const STATIC_PANELS = {
    waterfall: [0, 1, 3],   // 0 周期快照说明 / 1 出图按钮 / 3 判读说明
  };

  // ---- 装饰：每个面板右上角一个 ⤢ ------------------------------------------
  function decorate(root) {
    if (!root || !root.querySelectorAll) return;
    const pageId = (window.Router && Router.current && Router.current.id) || "page";
    root.querySelectorAll(".panel").forEach((node, i) => {
      if (!node.dataset.panelKey) node.dataset.panelKey = pageId + ":" + i;
      // **已经弹出去的面板不许再改归属**：它被搬进了浮层，而浮层就挂在
      // #page-container 里，切页后这里会把它也遍历到。若照常改写，一个原本属于
      // wifi 页的面板会被标成「ble 页的」，于是切回 wifi 时被判成「跨页」而
      // 反复暂停（实测：切回来永远恢复不了）。
      if (node.classList.contains("panel--popped")) return;
      // 所属页 + 是否汇总型：切页时靠这两个字段决定「关掉」还是「留着暂停」
      node.dataset.panelOwner = pageId;
      node.dataset.panelStatic = (STATIC_PANELS[pageId] || []).indexOf(i) >= 0 ? "1" : "0";
      if (node.querySelector(":scope > .panel__zoom")) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "panel__zoom";
      btn.textContent = "⤢";
      btn.dataset.i18nTitle = "popout.dblHint";
      btn.title = T("popout.dblHint");
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        toggle(node);
      });
      node.appendChild(btn);
    });
  }

  let observer = null;
  let decorateTimer = null;

  function watch(root) {
    if (!root || observer) return;
    observer = new MutationObserver((records) => {
      const grew = records.some((rec) => rec.addedNodes && rec.addedNodes.length);
      if (!grew || decorateTimer) return;
      decorateTimer = setTimeout(() => {
        decorateTimer = null;
        decorate(root);
      }, 500);
    });
    observer.observe(root, { childList: true, subtree: true });
  }

  function attach(root) {
    if (!root || root.__popoutBound) {
      decorate(root);
      return;
    }
    root.__popoutBound = true;
    root.addEventListener("dblclick", (ev) => {
      // 表格里双击选中文字是常事，别一选就弹
      const sel = window.getSelection && String(window.getSelection());
      const panelNode = ev.target.closest && ev.target.closest(".panel");
      if (!panelNode || !root.contains(panelNode)) return;
      if (sel && sel.length > 1) return;
      ev.preventDefault();
      toggle(panelNode);
    });
    decorate(root);
    watch(root);
  }

  // ---- 浮层 ---------------------------------------------------------------
  function build() {
    overlay = document.createElement("div");
    overlay.className = OVERLAY_CLASS;
    overlay.innerHTML =
      '<div class="popout__bar">' +
      '  <span class="popout__title"></span>' +
      '  <span class="popout__hint" data-i18n="popout.hint">双击任意面板可放大预览；Esc 关闭</span>' +
      '  <span class="popout__tools">' +
      '    <span class="popout__label" data-i18n="popout.zoom">缩放</span>' +
      '    <button type="button" class="btn btn--mini" data-act="out" title="-">−</button>' +
      '    <span class="popout__val">100%</span>' +
      '    <button type="button" class="btn btn--mini" data-act="in" title="+">+</button>' +
      '    <button type="button" class="btn btn--mini" data-act="reset" data-i18n="popout.reset">重置</button>' +
      '    <button type="button" class="btn btn--mini" data-act="full" data-i18n="popout.full">全屏</button>' +
      '    <button type="button" class="btn btn--mini" data-act="window" data-i18n="popout.window">独立窗口</button>' +
      '    <button type="button" class="btn btn--mini btn--ghost" data-act="close" data-i18n="popout.close">关闭</button>' +
      '  </span>' +
      "</div>" +
      '<div class="popout__paused" hidden>' +
      '  <span class="popout__paused-dot"></span>' +
      '  <span class="popout__paused-text"></span>' +
      "</div>" +
      '<div class="popout__stage"></div>';
    // 挂到页面容器里：搬走的面板仍然是页面 root 的后代，页面钩子照常能找到画布
    (document.getElementById("page-container") || document.body).appendChild(overlay);
    stage = overlay.querySelector(".popout__stage");
    bar = overlay.querySelector(".popout__bar");
    pauseBar = overlay.querySelector(".popout__paused");
    overlay.addEventListener("click", (ev) => {
      const act = ev.target.dataset && ev.target.dataset.act;
      if (act) {
        ev.preventDefault();
        if (act === "in") step(1);
        else if (act === "out") step(-1);
        else if (act === "reset") setZoom(1);
        else if (act === "full") fullscreen();
        else if (act === "window") openDocked();
        else if (act === "close") close();
      }
    });
    window.addEventListener("keydown", onKey);
    window.addEventListener("langchange", relabel);
    I18N.apply(bar);  // 只翻工具栏，别碰搬进来的面板（里面有动态内容）
  }

  function open(node) {
    if (!node || panel === node) return;
    if (panel) close();
    // 切页会把 #page-container 的 innerHTML 换掉，浮层可能已经不在文档里了
    if (!overlay || !overlay.isConnected) build();
    panel = node;
    pausedFor = null;
    setPaused(false);
    home = { parent: node.parentNode, next: node.nextSibling };
    stage.appendChild(node);
    node.classList.add("panel--popped");
    overlay.querySelector(".popout__title").textContent = titleOf(node);
    overlay.hidden = false;
    applyChrome();
    setZoom(zoom, true);
    refit();
  }

  function close() {
    // 排障用：谁关的、关了几次。state() 里带出去，自检 / 手敲 console 都能看
    closeSeq += 1;
    lastCloseTrace = String((new Error().stack || "")).split(String.fromCharCode(10)).slice(1, 4).join(" | ");
    setPaused(false);
    pausedFor = null;
    // 浮层和面板都不在文档里 = 真被上一页的 innerHTML 一起清掉了，只复位状态
    if (overlay && !overlay.isConnected && !(panel && panel.isConnected)) {
      clearTitle();
      overlay = null;
      stage = null;
      bar = null;
      pauseBar = null;
      panel = null;
      home = null;
      return;
    }
    if (!overlay || !panel) return;
    if (fsOn) fullscreen(false);
    panel.classList.remove("panel--popped");
    panel.style.width = "";
    panel.style.removeProperty("--fit-scale");
    panel.style.removeProperty("--fit-grow");
    const anchorOk = !!(home && home.next && home.parent && home.next.parentNode === home.parent);
    if (home && home.parent && home.parent.isConnected) {
      // home.next 可能已经被上一页的 innerHTML 连根拔掉（只剩个野指针）。
      // 直接 insertBefore 会抛 NotFoundError，而抛出时状态已经改了一半 → 面板卡死。
      home.parent.insertBefore(panel, anchorOk ? home.next : null);
    } else {
      // 中途切过页：原位置（旧页面的 DOM）已经不在文档里。
      // 同一页重新注入后面板顺序不变 → 换掉当前页里同 key 的那个空壳即可。
      const key = panel.dataset.panelKey;
      const holder = key
        ? document.querySelector('#page-container [data-panel-key="' + key + '"]')
        : null;
      if (holder && holder !== panel && holder.parentNode) holder.parentNode.replaceChild(panel, holder);
      else if (panel.parentNode) panel.parentNode.removeChild(panel);
      home = null;
    }
    overlay.hidden = true;
    // 标题是面板标题的一份副本；面板已经搬回去了，副本留着会变成「幽灵文案」
    // （浮层隐藏着没人看见，但自检扫 DOM 会扫到，英文界面里就漏出一句中文）。
    clearTitle();
    const was = panel;
    panel = null;
    home = null;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    refit();
    if (was) was.scrollTop = 0;
  }

  function toggle(node) {
    if (panel === node) close();
    else open(node);
  }

  // ---- 切页：实时流暂停 / 汇总型关闭 --------------------------------------
  function ownerOf(node) {
    return (node && node.dataset && node.dataset.panelOwner) || "";
  }

  function isStatic(node) {
    return !!(node && node.dataset && node.dataset.panelStatic === "1");
  }

  function pageTitle(pageId) {
    if (!pageId) return "";
    const entry = ((window.Router && Router.pages) || []).find((p) => p.id === pageId);
    return entry ? (I18N.entry("title", entry) || pageId) : pageId;
  }

  /** 显示/隐藏「模式已切换」提示条（暂停时面板保持最后一帧，不遮内容）。 */
  function setPaused(on, page) {
    if (!overlay) return;
    overlay.classList.toggle("popout--paused", !!on);
    if (pauseBar) {
      pauseBar.hidden = !on;
      if (on) {
        const text = pauseBar.querySelector(".popout__paused-text");
        if (text) text.textContent = T("popout.paused", { page: pageTitle(page) || page || "" });
      }
    }
    if (on) applyChrome();
  }

  /** 浮层挂在这一页的容器里；切页前先把它摘下来，别跟着 innerHTML 一起被清掉。 */
  function detachOverlay() {
    if (overlay && overlay.isConnected) overlay.remove();
  }

  /** Router 在换页**之前**调：汇总型/同页面 → 关闭；实时流 → 留在浮层里暂停。 */
  function beforePageChange(nextPage) {
    if (!panel) {
      detachOverlay();
      return;
    }
    if (pausedFor) {
      // 已经在暂停中（面板被搬到浮层里晾着）：这次切页只把浮层摘下来，
      // 归属和提示都别重算，等 afterPageChange 决定「继续暂停」还是「恢复刷新」。
      detachOverlay();
      return;
    }
    const owner = ownerOf(panel);
    if (isStatic(panel) || !owner || owner === nextPage) {
      close();
      detachOverlay();
      return;
    }
    if (fsOn) fullscreen(false);
    pausedFor = owner;
    setPaused(true, nextPage);
    detachOverlay();
  }

  /** Router 在换页**之后**调：把浮层接回新页面，并在切回所属页时恢复刷新。 */
  function afterPageChange(pageId) {
    activePage = pageId;
    const host = document.getElementById("page-container");
    if (overlay && host && !overlay.isConnected) host.appendChild(overlay);
    if (!panel) return;
    if (pausedFor && pageId === pausedFor) {
      // 切回面板所属页：页面钩子会重新喂数据，这里只要把提示收掉
      pausedFor = null;
      setPaused(false);
      refit();
    } else if (pausedFor) {
      setPaused(true, pageId);
    }
  }

  function titleOf(node) {
    const t = node.querySelector(".panel__title");
    return t ? t.textContent.trim() : "panel";
  }

  /** 浮层标题是从面板标题抄下来的一份副本，面板标题换了语言就得再抄一次。 */
  function refreshTitle() {
    if (!overlay || !panel) return;
    const node = overlay.querySelector(".popout__title");
    if (node) node.textContent = titleOf(panel);
  }

  /** 关掉浮层时把标题清空：面板已经搬回去，副本留着就是幽灵文案。 */
  function clearTitle() {
    if (!overlay) return;
    const node = overlay.querySelector(".popout__title");
    if (node) node.textContent = "";
  }

  // ---- 缩放 ---------------------------------------------------------------
  function setZoom(next, quiet) {
    zoom = Math.min(ZOOMS[ZOOMS.length - 1], Math.max(ZOOMS[0], next));
    if (panel) {
      panel.style.width = zoom * 100 + "%";
      panel.style.setProperty("--fit-scale", String(zoom));
    }
    if (overlay) {
      const val = overlay.querySelector(".popout__val");
      if (val) val.textContent = Math.round(zoom * 100) + "%";
    }
    if (!quiet) refit();
  }

  function step(dir) {
    let i = 0;
    let best = Infinity;
    ZOOMS.forEach((z, k) => {
      const d = Math.abs(z - zoom);
      if (d < best) { best = d; i = k; }
    });
    setZoom(ZOOMS[Math.min(ZOOMS.length - 1, Math.max(0, i + dir))]);
  }

  /**
   * 尺寸变了：让页面自己的 resize 钩子重画（画布按新尺寸重新渲染，不是拉伸），
   * 然后第二阶段把余量高度分给会长的画布 —— 全屏/放大后不该还留一大块空白。
   */
  function refit() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      resizeTimer = null;
      setGrow(0);
      dispatchResize();
      if (!panel) return;
      if (growTimer) clearTimeout(growTimer);
      // 等页面把画布按新倍率重画完（各页的 resize 钩子是防抖的），再量高度
      growTimer = setTimeout(applyGrow, 260);
    }, 120);
  }

  /** 派发一次合成 resize：自己派发的不要反过来触发 refit（会无限递归） */
  function dispatchResize() {
    suppress += 1;
    try {
      window.dispatchEvent(new Event("resize"));
    } finally {
      suppress = 0;
    }
  }

  // 窗口真的被拉大/拉小（包括 Python 侧把窗口设为全屏）：余量高度要重算
  window.addEventListener("resize", () => {
    if (suppress) return;
    if (panel) refit();
  });

  function setGrow(px) {
    if (!panel) return;
    if (px > 0) panel.style.setProperty("--fit-grow", Math.round(px) + "px");
    else panel.style.setProperty("--fit-grow", "0px");
  }

  /** 把浮层里剩下的高度分给标了 data-fit-grow="1" 的画布/容器 */
  function applyGrow() {
    growTimer = null;
    if (!panel || !stage) return;
    const growers = panel.querySelectorAll('[data-fit-grow="1"]').length;
    if (!growers) return;
    const avail = stage.clientHeight - 24;              // stage 上下 padding
    const used = panel.getBoundingClientRect().height;
    const extra = Math.max(0, avail - used);
    const per = extra / growers;
    if (per < 2) return;                                 // 已经塞满，别来回抖
    setGrow(per);
    dispatchResize();
  }

  /**
   * 全屏。
   *
   * 合成点击（element.click() / 自动化）没有**用户手势**，Chromium 会直接拒
   * `requestFullscreen()` —— 于是「点全屏没反应，布局跟普通一模一样」。所以这里
   * 优先走 Python：`setFullscreen` 由宿主调 `showFullScreen()`，不需要手势。
   * 没有 Bridge（纯浏览器打开网页）时才退回 DOM API，再不行就退化成铺满窗口。
   */
  function fullscreen(force) {
    if (!overlay) return;
    const want = typeof force === "boolean" ? force : !fsOn;
    if (window.Bridge && Bridge.ready && Bridge.ready()) {
      Bridge.call("setFullscreen", JSON.stringify({ on: want, target: solo ? "popout" : "main" }))
        .then((res) => {
          fsOn = !!(res && res.fullscreen);
          markFull();
          refit();
        })
        .catch(() => fallbackFullscreen(want));
      return;
    }
    fallbackFullscreen(want);
  }

  /** 纯浏览器（没有 Bridge）兜底：DOM 全屏，被拒就铺满窗口 */
  function fallbackFullscreen(want) {
    if (want) {
      if (overlay && overlay.requestFullscreen) {
        overlay.requestFullscreen().catch(() => overlay.classList.add("popout--max"));
      } else if (overlay) {
        overlay.classList.add("popout--max");
      }
      fsOn = true;
    } else {
      if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
      if (overlay) overlay.classList.remove("popout--max");
      fsOn = false;
    }
    markFull();
    refit();
  }

  function markFull() {
    if (!overlay) return;
    const btn = overlay.querySelector('[data-act="full"]');
    if (btn) btn.textContent = T(fsOn ? "popout.exitFull" : "popout.full");
  }

  /** 独立小窗口：隐藏「关闭」（关掉就只剩空白）与「独立窗口」（已经独立了） */
  function applyChrome() {
    if (!overlay) return;
    overlay.classList.toggle("popout--solo", solo);
    const closeBtn = overlay.querySelector('[data-act="close"]');
    const dockBtn = overlay.querySelector('[data-act="window"]');
    const hint = overlay.querySelector(".popout__hint");
    if (closeBtn) closeBtn.hidden = solo;
    if (dockBtn) dockBtn.hidden = solo;
    if (hint) hint.hidden = solo;
  }

  function setSolo(on) {
    solo = !!on;
    document.body.classList.toggle("solo", solo);
    applyChrome();
  }

  /** 开一个**真正的独立窗口**（Python 侧 createWindow 接住，可拖到别的显示器） */
  function openDocked(key) {
    const node = key && typeof key === "string" ? document.querySelector('[data-panel-key="' + key + '"]') : panel;
    const grower = document.querySelector('#page-container [data-fit-grow="1"]');
    const target = node || (grower && grower.closest(".panel")) || document.querySelector("#page-container .panel");
    if (!target) return "";
    const panelKey = target.dataset.panelKey || "";
    const url = "index.html?solo=" + encodeURIComponent(panelKey);
    window.open(url, "_blank", "width=1024,height=680");
    return panelKey;
  }

  /** 独立窗口里自报家门（自检探针用） */
  function openSolo(key) {
    setSolo(true);
    const node = document.querySelector('[data-panel-key="' + key + '"]');
    if (!node) {
      overlay = null;
      return false;
    }
    open(node);
    return true;
  }

  function onKey(ev) {
    if (!panel) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      close();
    }
  }

  function relabel() {
    document.querySelectorAll(".panel__zoom").forEach((b) => {
      b.title = T("popout.dblHint");
    });
    if (!overlay) return;
    I18N.apply(bar);
    applyChrome();
    markFull();
    refreshTitle();
    // 暂停提示是动态文案（带页面名），I18N.apply 翻不到，得按当前语言重写一遍
    if (pausedFor) setPaused(true, activePage || pausedFor);
  }

  /** 给 --selftest 用：开 → 放大 → 关，把三段的实测值记到 window.__popout。 */
  function selftest() {
    const out = { steps: [] };
    window.__popout = out;
    const host = document.getElementById("page-container");
    // 挑一个「会长高」的面板（2D 瀑布 / 频谱），才能验到高度自适应
    const grower = host && host.querySelector('[data-fit-grow="1"]');
    const target = (grower && grower.closest(".panel")) || (host && host.querySelector(".panel"));
    if (!target) {
      out.error = "no panel";
      out.done = true;
      return out;
    }
    const canvasOf = (node) => node.querySelector("canvas");
    const sizeOf = (node, key) => (node ? Math.round(node.getBoundingClientRect()[key]) : 0);
    open(target);
    const c0 = canvasOf(target);
    out.open = {
      overlay: !!document.querySelector(".popout"),
      opened: target.classList.contains("panel--popped"),
      inOverlay: target.parentElement === stage,
      width: c0 ? sizeOf(c0, "width") : sizeOf(target, "width"),
      height: c0 ? sizeOf(c0, "height") : 0,
      zoom: zoom,
    };
    setZoom(1.5);
    setTimeout(() => {
      const c1 = canvasOf(target);
      out.zoom = {
        zoom: zoom,
        width: c1 ? sizeOf(c1, "width") : sizeOf(target, "width"),
        height: c1 ? sizeOf(c1, "height") : 0,
        fitScale: target.style.getPropertyValue("--fit-scale"),
        fitGrow: target.style.getPropertyValue("--fit-grow"),
        stageH: sizeOf(stage, "height"),
        panelH: sizeOf(target, "height"),
      };
      close();
      setTimeout(() => {
        out.close = {
          overlay: !!(overlay && !overlay.hidden),  // 关闭后元素还在，但必须 hidden
          hidden: overlay ? overlay.hidden : true,
          backHome: !target.classList.contains("panel--popped") && host.contains(target),
        };
        out.done = true;
      }, 500);
    }, 700);
    return out;
  }

  return {
    attach,
    open,
    close,
    toggle,
    beforePageChange,
    afterPageChange,
    state: function () {
      return {
        open: !!panel,
        paused: !!pausedFor,
        owner: panel ? ownerOf(panel) : "",
        static: panel ? isStatic(panel) : false,
        page: activePage,
        key: panel ? (panel.dataset.panelKey || "") : "",
        banner: !!(pauseBar && !pauseBar.hidden),
        connected: !!(overlay && overlay.isConnected),
        closes: closeSeq,
        closeTrace: lastCloseTrace,
      };
    },
    setZoom,
    setSolo,
    openSolo,
    openDocked,
    selftest,
    refreshTitle,
    get current() { return panel; },
    get zoom() { return zoom; },
  };
})();