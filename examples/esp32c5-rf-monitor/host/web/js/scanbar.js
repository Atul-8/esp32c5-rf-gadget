/*
 * scanbar.js —— 扫频进度条（**全频段地图 + 历史热力 + 滚动游标**）。
 *
 * 它回答三个问题：
 *   1. 现在读到哪一个窗了？   → 黄色游标 + 当前窗高亮
 *   2. 这一轮扫到哪儿了？     → 每个窗的青色描边（本轮已访问）
 *   3. 每个窗历史上有多挤？   → 格子底色 = 该窗的历史占用强度（热力，越亮越挤）
 *
 * 关键约定（都是踩过的坑）：
 *   · 段的边界/格子**不写死**，来自 `info.plans`（Python 侧各插件的 scan_requests()）。
 *   · 地图画的是**全体模块计划的并集**（不是某一个模块）：单射频分时下物理上就是
 *     按并集扫的，只跟一个模块会出现「扫到 5G HIGH 了但地图上没这一格」。
 *   · 帧来自所有模块，但**只有对得上自己计划频点的那一帧**才移动游标
 *     （wifi 的 2412、抓档的 5180 混进来会让游标来回跳、假回卷把轮次吹高）。
 *   · 热力用 p90（不是平均）：窗里有窄带信号时平均值会被底噪摊平，热力就看不出来。
 *   · 热力**跨轮累积**（EMA），这才是「历史滚动」的意义：一轮只扫一次的话，
 *     地图上永远只有一条游标在跑，看不出哪里一直挤。
 */
window.ScanBar = (function () {
  const HEIGHT = 66;       // 整块高度（CSS px）
  const TRACK_TOP = 6;
  const TRACK_H = 30;
  const LERP = 0.25;
  const LERP_WRAP = 0.15;  // 回卷时稍慢一点，能看清游标往左扫回去
  const HEAT_EMA = 0.35;   // 新一帧在热力里的权重（越大越跟手、越小越偏「历史」）
  const RANGE_P10 = 0.10;  // 拉伸范围用 p10..p90，避免个别尖峰把整张图压暗
  const RANGE_P90 = 0.90;
  const RANGE_MIN_SPAN = 8; // 码值跨度下限：底噪平坦时也留出颜色层次

  class ScanBar {
    constructor(host) {
      this.host = host;
      this.canvas = document.createElement("canvas");
      this.canvas.id = "scanbar-canvas";  // 给自检探针一个可读名字
      this.canvas.className = "scanbar__canvas";
      host.innerHTML = "";
      host.appendChild(this.canvas);
      this.fit = U.fitCanvas(this.canvas, HEIGHT);
      this.plans = {};        // {module: [[center_hz, bw_hz], ...]}
      this.centers = {};      // {module: Set(整 MHz)}
      this.plan = [];         // 并集窗，按频率升序 [{center, bw, key}]
      this.groups = [];       // [{lo, hi, name, items:[{center, key, x0, x1, cx}]}]
      this.heat = new Map();  // key → {v: EMA 电平码, t: ms}
      this.samples = [];      // 最近的电平码（算拉伸范围）
      this.rangeCache = null;
      this.heatVersion = 1;   // 热力/范围一变就 +1，热力条离屏缓存靠它失效
      this.strip = null;      // 离屏热力条
      this.stripSig = "";
      this.rangeAt = 0;
      this.index = -1;        // 当前窗在 this.plan 里的下标
      this.passes = 1;
      this.visited = new Set(); // 本轮已访问的 key
      this.x = 0;
      this.target = 0;
      this.wrapping = false;
      this.raf = 0;
      this.lastCenter = null;
      this.lastModule = null;
      this.trail = [];
      this.resize();
    }

    // ---- 计划 -------------------------------------------------------------
    /**
     * 全体模块的扫描计划。``plans`` = ``{module: [[center_hz, bw_hz], ...]}``。
     * 单模块参数写法的 ``setPlan(module, windows)`` 仍然可用（自检在用）。
     */
    setPlans(plans) {
      const clean = {};
      Object.keys(plans || {}).forEach((mod) => {
        const wins = (plans[mod] || []).filter((w) => w && Number(w[0]) > 0);
        if (wins.length) clean[mod] = wins;
      });
      const key = JSON.stringify(
        Object.keys(clean).sort().map((m) => [m, clean[m].length, clean[m][0] && clean[m][0][0],
          clean[m][clean[m].length - 1] && clean[m][clean[m].length - 1][0]])
      );
      if (this.planKey === key) return;
      this.planKey = key;
      this.plans = clean;
      this.centers = {};
      // 并集：同频点（整 MHz 口径，固件 FREQ 只收整 MHz）合并，取较大带宽
      const byKey = new Map();
      Object.keys(clean).forEach((mod) => {
        const set = new Set();
        clean[mod].forEach((w) => {
          const center = Number(w[0]) || 0;
          const bw = Number(w[1]) || 0;
          const k = Math.round(center / 1e6);
          set.add(k);
          const prev = byKey.get(k);
          if (!prev || bw > prev.bw) byKey.set(k, { center: center, bw: bw, key: k });
        });
        this.centers[mod] = set;
      });
      this.plan = Array.from(byKey.values()).sort((a, b) => a.center - b.center);
      // 谁拥有并集的**第一格**：换轮判据要用它（详见 update() 里的说明）
      this.firstKeys = {};
      if (this.plan.length) {
        const firstKey = this.plan[0].key;
        Object.keys(this.centers).forEach((mod) => {
          if (this.centers[mod].has(firstKey)) this.firstKeys[mod] = true;
        });
      }
      this.seenHigh = 0;
      this.heat.clear();
      this.samples = [];
      this.rangeCache = null;
      this.rebuild();
      this.index = -1;
      this.visited.clear();
      this.passes = 1;
      this.x = 0;
      this.target = 0;
      this.draw();
    }

    /** 兼容老写法：只给一个模块的计划 */
    setPlan(module, windows) {
      const plans = {};
      if (module) plans[module] = windows || [];
      this.setPlans(plans);
    }

    rebuild() {
      const groups = [];
      const gapLimit = (this.plan[0] ? this.plan[0].bw : 20e6) * 3;
      let cur = null;
      this.plan.forEach((item) => {
        const lo = item.center - item.bw / 2;
        const hi = item.center + item.bw / 2;
        if (!cur || lo - cur.hi > gapLimit) {
          cur = { lo: lo, hi: hi };
          groups.push(cur);
        }
        cur.hi = Math.max(cur.hi, hi);
      });
      groups.forEach((g, i) => {
        const spanMHz = (g.hi - g.lo) / 1e6;
        g.name = spanMHz < 200 && g.lo > 2.3e9 && g.lo < 2.6e9 ? "2.4G" : g.lo > 5e9 ? "5G" : "#" + (i + 1);
      });
      this.groups = groups;
      this.layout();
    }

    /** 分段线性布局：每段宽度 ∝ √跨度（2.4G 只有 80 MHz，按真实比例会细成一根线） */
    layout() {
      const width = this.fit.width;
      const weights = this.groups.map((g) => Math.sqrt(Math.max(1, g.hi - g.lo)));
      const total = weights.reduce((a, b) => a + b, 0) || 1;
      let x = 0;
      this.groups.forEach((g, i) => {
        const raw = (width * weights[i]) / total;
        g.x0 = x;
        g.x1 = i === this.groups.length - 1 ? width : x + raw;
        x = g.x1;
        const span = Math.max(1, g.hi - g.lo);
        g.items = [];
        this.plan.forEach((p, planIndex) => {
          if (p.center < g.lo - 1 || p.center > g.hi + 1) return;
          const cx = g.x0 + ((p.center - g.lo) / span) * (g.x1 - g.x0);
          const half = Math.max(1.2, ((p.bw / 2) / span) * (g.x1 - g.x0));
          g.items.push({ center: p.center, key: p.key, index: planIndex, x0: cx - half, x1: cx + half, cx: cx });
        });
      });
      this.curWindow = this.findItem(this.index);
      this.target = this.curWindow
        ? this.curWindow.cx
        : (this.groups[0] && this.groups[0].items[0] ? this.groups[0].items[0].cx : 0);
      if (this.index < 0) this.x = this.target;
    }

    // ---- 数据 -------------------------------------------------------------
    /** payload 来自 spectrum 信号（center_hz / bw_hz / module / bins） */
    update(payload) {
      if (!payload) return;
      this.curWidth = payload.bw_hz;
      const mod = payload.module || "";
      // 没有计划就只画游标（比如刚启动还没收到 info）
      if (!this.plan.length) {
        this.lastCenter = payload.center_hz;
        this.lastModule = mod;
        this.draw();
        return;
      }
      const found = this.locate(mod, payload.center_hz);
      if (found) this.addHeat(found.item.key, payload.bins);
      if (!found) {
        // 对不上任何计划的帧：只更新「当前频点」文字，不动游标、不进热力
        this.lastCenter = payload.center_hz;
        this.lastModule = mod;
        this.draw();
        return;
      }
      // 换轮判据：**先到过尾部、再出现并集第 1 格**才算一轮。
      //
      // 为什么不能只看「下标变小很多」：单射频分时下，抓档/增益这类会插空做一次
      // 独立扫描（pause_around → 直接 SPEC/SPC1），那一帧的频点在 2400 MHz 附近、
      // 下标很小；此时主轮正在 5G 尾巴上（下标 80+），一比较就是「跳回大半张图」，
      // 于是每插一次空就假报一轮 —— 真板实测 40 s 吹到 49 轮（物理上一轮 5.4 s，
      // 最多 7 轮），而且 visited 被反复清空，"本轮已扫"那条永远只剩一两格。
      //
      // 换轮真正发生时的特征是固定不变的：设备每一轮都从并集第 1 格起步。所以判据取
      // 「见过尾部」+「这一帧的主人确实拥有第 1 格」，插空帧既到不了尾部也对不上主人。
      if (found.index > this.seenHigh) this.seenHigh = found.index;
      const nearEnd = this.seenHigh >= this.plan.length - Math.max(2, Math.ceil(this.plan.length * 0.05));
      const atStart = found.index <= 1 && !!(mod && this.firstKeys && this.firstKeys[mod]);
      const wrapped = nearEnd && atStart;
      if (wrapped) {
        this.passes += 1;
        this.visited.clear();
        this.wrapping = true;
        this.seenHigh = found.index;
      } else {
        this.wrapping = false;
      }
      this.index = found.index;
      this.target = found.item.cx;
      this.visited.add(found.item.key);
      this.curWindow = found.item;
      this.lastCenter = payload.center_hz;
      this.lastModule = mod;
      this.trail.push(found.index);
      if (this.trail.length > 200) this.trail.shift();
      this.start();
    }

    /**
     * 只在**该模块自己的计划窗**里找最近格。
     *
     * 为什么必须按模块过滤：物理计划是并集，wifi 扫 2412/2437…、抓档扫 5180、
     * 守卫扫 5745，这些频点在全频段图上离最近的瀑布窗可能只差几 MHz，
     * 不过滤的话游标会在这几个点之间来回跳、还时不时触发「假回卷」把轮次吹高
     * （真板实测 8 s 就吹到 12 轮）。
     */
    locate(module, centerHz) {
      const own = module && this.centers[module];
      if (!own || !own.size) return null;
      const k = Math.round((Number(centerHz) || 0) / 1e6);
      if (!own.has(k)) return null;
      let best = null;
      this.plan.forEach((p, i) => {
        if (p.key !== k) return;
        best = { index: i, item: this.findItem(i) };
      });
      if (!best) return null;
      // findItem 会按 key 精确落位；拿不到坐标说明这一段没布局好
      if (!best.item) best.item = { center: centerHz, key: k, index: best.index, cx: this.x, x0: this.x, x1: this.x };
      return best;
    }

    findItem(index) {
      let found = null;
      this.groups.forEach((g) => {
        g.items.forEach((item) => { if (item.index === index) found = item; });
      });
      return found;
    }

    // ---- 热力 -------------------------------------------------------------
    /** 一帧的「占用强度」用 p90：窗里有窄带信号时平均值会被底噪摊平 */
    static levelOf(bins) {
      if (!bins || !bins.length) return null;
      const arr = Array.prototype.slice.call(bins).sort((a, b) => a - b);
      const i = Math.min(arr.length - 1, Math.floor(arr.length * RANGE_P90));
      return arr[i];
    }

    addHeat(key, bins) {
      const code = ScanBar.levelOf(bins);
      if (code === null) return;
      const prev = this.heat.get(key);
      const v = prev ? prev.v + (code - prev.v) * HEAT_EMA : code;
      this.heat.set(key, { v: v, t: Date.now() });
      this.samples.push(code);
      if (this.samples.length > 400) this.samples.shift();
      this.rangeCache = null;
      this.heatVersion += 1;
    }

    /** 热力拉伸范围：p10..p90（缓存 500 ms，避免每帧排序） */
    heatRange() {
      const now = Date.now();
      if (this.rangeCache && now - this.rangeAt < 500) return this.rangeCache;
      const src = this.samples.slice().sort((a, b) => a - b);
      let lo = 0;
      let hi = 255;
      if (src.length >= 4) {
        lo = src[Math.floor(src.length * RANGE_P10)];
        hi = src[Math.min(src.length - 1, Math.floor(src.length * RANGE_P90))];
      }
      if (hi - lo < RANGE_MIN_SPAN) hi = lo + RANGE_MIN_SPAN;
      this.rangeCache = { lo: lo, hi: hi };
      this.rangeAt = now;
      this.heatVersion += 1;
      return this.rangeCache;
    }

    heatColor(key) {
      const rec = this.heat.get(key);
      if (!rec) return null;
      return this.rgbOf(rec.v);
    }

    /** 电平码 → 热力 RGB（用当前拉伸范围） */
    rgbOf(v) {
      const r = this.heatRange();
      const t = (v - r.lo) / (r.hi - r.lo || 1);
      return U.heatRGB(Math.max(0, Math.min(1, t)));
    }

    /** 电平码 → CSS 颜色（按像素填空力条时每列都要，按码值缓存字符串避免刷 GC） */
    heatStyle(v) {
      const key = Math.max(0, Math.min(255, Math.round(v)));
      if (this.styleCache && this.styleCacheKey === this.rangeCache) {
        const hit = this.styleCache[key];
        if (hit) return hit;
      } else {
        this.styleCache = {};
        this.styleCacheKey = this.rangeCache;
      }
      const rgb = this.rgbOf(v);
      const style = "rgb(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + ")";
      this.styleCache[key] = style;
      return style;
    }

    // ---- 动画 -------------------------------------------------------------
    start() {
      // 注意：**不能**只判 ``this.raf`` 就 return。requestAnimationFrame 在窗口被遮挡 /
      // 最小化时会被节流甚至完全不回调，这时 raf 句柄还是非零、动画却已经停了，
      // 后面每一帧都被这里挡回去 → 游标永久冻在某个位置（真板实测冻在 229px）。
      // 判据改成「一段时间内没推进就重开一条」。
      const now = typeof performance !== "undefined" ? performance.now() : Date.now();
      if (this.raf) {
        if (now - (this.lastStepAt || 0) < 300) return;
        cancelAnimationFrame(this.raf);
        this.raf = 0;
      }
      this.lastStepAt = now;
      const step = () => {
        this.lastStepAt = typeof performance !== "undefined" ? performance.now() : Date.now();
        const k = this.wrapping ? LERP_WRAP : LERP;
        this.x += (this.target - this.x) * k;
        if (Math.abs(this.target - this.x) < 0.4) {
          this.x = this.target;
          this.raf = 0;
          this.wrapping = false;
          this.draw();
          return;
        }
        this.draw();
        this.raf = requestAnimationFrame(step);
      };
      this.raf = requestAnimationFrame(step);
    }

    /** 画布被外力摘掉（例如容器 textContent 被重写）时挂回去，别让地图永久空白 */
    ensureCanvas() {
      if (this.canvas.parentNode !== this.host) {
        this.host.innerHTML = "";
        this.host.appendChild(this.canvas);
        this.fit = U.fitCanvas(this.canvas, HEIGHT);
        this.layout();
      }
    }

    resize() {
      this.ensureCanvas();
      this.fit = U.fitCanvas(this.canvas, HEIGHT);
      this.layout();
      this.draw();
    }

    // ---- 绘制 -------------------------------------------------------------
    /**
     * 把热力条画进离屏画布（只在热力 / 尺寸变化时重画）。
     *
     * 每列取「覆盖这一列的所有窗里最热的那个」：items 按中心升序，用游标往前推，
     * 每列只看可能覆盖它的那几个窗 → O(列数 + 窗数)。
     */
    paintStrip(width, stripH) {
      const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
      const sig = [width, stripH, this.planKey || "", this.heatVersion, dpr].join("|");
      if (this.strip && this.stripSig === sig) return;
      const cv = this.strip || document.createElement("canvas");
      cv.width = Math.max(1, Math.ceil(width * dpr));
      cv.height = Math.max(1, Math.ceil(stripH * dpr));
      const sctx = cv.getContext("2d");
      sctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      sctx.clearRect(0, 0, width, stripH);
      this.groups.forEach((g) => {
        const px0 = Math.max(0, Math.floor(g.x0));
        const px1 = Math.min(width, Math.ceil(g.x1));
        if (px1 <= px0) return;
        const span = Math.max(1, g.hi - g.lo);
        const gen = Math.max(1, g.x1 - g.x0);
        const items = g.items;
        const halfOf = (item) => this.plan[item.index].bw / 2;
        let head = 0;
        let lastStyle = "";
        for (let px = px0; px < px1; px++) {
          const freq = g.lo + ((px + 0.5 - g.x0) / gen) * span;
          while (head < items.length && items[head].center + halfOf(items[head]) < freq) head += 1;
          let v = null;
          for (let i = head; i < items.length; i++) {
            const item = items[i];
            if (item.center - halfOf(item) > freq) break;
            const rec = this.heat.get(item.key);
            if (rec && (v === null || rec.v > v)) v = rec.v;
          }
          const style = v === null ? "#151f31" : this.heatStyle(v);
          if (style !== lastStyle) {
            sctx.fillStyle = style;
            lastStyle = style;
          }
          sctx.fillRect(px, 0, 1, stripH);
        }
      });
      this.strip = cv;
      this.stripSig = sig;
    }

    draw() {
      this.ensureCanvas();
      const { ctx, width } = this.fit;
      ctx.clearRect(0, 0, width, HEIGHT);

      // 轨道底
      ctx.fillStyle = "#0b1220";
      roundRect(ctx, 0, TRACK_TOP, width, TRACK_H, 4);
      ctx.fill();
      ctx.strokeStyle = "#1c2a41";
      ctx.lineWidth = 1;
      ctx.stroke();

      if (!this.groups.length) {
        ctx.fillStyle = "#5f7391";
        ctx.font = "11px Consolas, monospace";
        ctx.fillText(T("scanbar.noplan"), 8, TRACK_TOP + 19);
        return;
      }

      ctx.font = "10px Consolas, monospace";
      const stripH = TRACK_H - 11;
      // ---- 热力条（离屏缓存，见 paintStrip）：一列 = 一个像素，取「覆盖这一列的所有
      // 窗里最热的那个」。按像素而不是按窗画格子：窗是 20 MHz / 步进 15 MHz，**互相
      // 重叠**，按窗画会互相盖住（2.4G 段 19 个窗挤在 150px 里糊成一块）；按列取最大
      // 保持，跟 waterfall/PNG 的「同格多窗取最大」口径一致。
      // 热力只在收到新窗 / 拉伸范围变化时才变，所以整条缓进离屏画布，游标每帧只做一次
      // drawImage。否则每帧上千次 fillRect 会占住 UI 线程，弹出预览的 setTimeout 被
      // 推迟（实测会让 popout 的三项检查读到中间态而误报失败）。
      this.paintStrip(width, stripH);
      if (this.strip) ctx.drawImage(this.strip, 0, 0, width, stripH);

      this.groups.forEach((g) => {
        // 段边界
        ctx.strokeStyle = g.lo > 5e9 ? "#6d5bd0" : "#2f7fa8";
        ctx.globalAlpha = 0.55;
        ctx.strokeRect(g.x0 + 0.5, TRACK_TOP + 0.5, Math.max(2, g.x1 - g.x0 - 1), TRACK_H - 1);
        ctx.globalAlpha = 1;

        // ---- 本轮已扫条：顶部细条，一个窗一段（和热力色分开，热力看亮、覆盖看条）----
        g.items.forEach((item) => {
          const on = this.visited.has(item.key);
          ctx.fillStyle = on ? "rgba(56,189,248,0.85)" : "rgba(56,189,248,0.12)";
          const w = Math.max(1.5, item.x1 - item.x0);
          ctx.fillRect(item.x0, TRACK_TOP + 2, w, 5);
        });

        // ---- 段名 + 频率范围 ----
        ctx.fillStyle = "#c8d6ef";
        ctx.fillText(g.name, g.x0 + 6, TRACK_TOP + 15);
        const range = (g.lo / 1e6).toFixed(0) + "-" + (g.hi / 1e6).toFixed(0) + " " + T("spectrum.mhz");
        ctx.fillStyle = "#8697b4";
        ctx.fillText(range, g.x0 + 6, TRACK_TOP + TRACK_H + 12);
      });

      // 当前窗高亮
      if (this.curWindow && this.index >= 0) {
        const w = Math.max(2, this.curWindow.x1 - this.curWindow.x0);
        ctx.strokeStyle = "rgba(56,189,248,0.95)";
        ctx.lineWidth = 2;
        ctx.strokeRect(this.curWindow.x0, TRACK_TOP + 2, w, TRACK_H - 4);
      }

      // 游标：一条竖线 + 顶部小三角
      const x = Math.round(this.x) + 0.5;
      ctx.strokeStyle = "#fbbf24";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x, TRACK_TOP - 4);
      ctx.lineTo(x, TRACK_TOP + TRACK_H + 1);
      ctx.stroke();
      ctx.fillStyle = "#fbbf24";
      ctx.beginPath();
      ctx.moveTo(x - 4, TRACK_TOP - 6);
      ctx.lineTo(x + 4, TRACK_TOP - 6);
      ctx.lineTo(x, TRACK_TOP - 1);
      ctx.closePath();
      ctx.fill();

      // 右上角：当前频点 + 轮次 + 本轮进度
      if (this.lastCenter) {
        const text = T("scanbar.now", {
          center: (this.lastCenter / 1e6).toFixed(0),
          at: Math.max(1, this.index + 1),
          total: this.plan.length,
          pass: this.passes,
        });
        ctx.fillStyle = "#8697b4";
        ctx.fillText(text, Math.max(4, width - ctx.measureText(text).width - 2), TRACK_TOP + 13);
      }

      // 右下角：热力图例（颜色 = 历史占用强度）+ 已有热力的格数
      this.drawLegend(ctx, width);

      // 底部：本轮覆盖率细条（青=本轮已扫比例，别和热力色混在一起）
      const done = this.visited.size;
      const total = Math.max(1, this.plan.length);
      ctx.fillStyle = "#16233a";
      ctx.fillRect(0, HEIGHT - 8, width, 4);
      ctx.fillStyle = "#38bdf8";
      ctx.fillRect(0, HEIGHT - 8, (width * done) / total, 4);
    }

    heatCells() {
      let n = 0;
      this.heat.forEach((rec) => { if (rec && rec.v > 0) n += 1; });
      return n;
    }

    /** 热力图例：一条渐变 + 两端标签，让人知道「亮 = 挤」 */
    drawLegend(ctx, width) {
      const labels = [T("scanbar.heatLow"), T("scanbar.heatHigh")];
      const y = TRACK_TOP + TRACK_H + 8;
      ctx.font = "10px Consolas, monospace";
      const high = labels[1];
      const low = labels[0];
      const hw = ctx.measureText(high).width;
      const lw = ctx.measureText(low).width;
      const barW = 54;
      const barH = 7;
      const right = width - 2;
      const barX = right - hw - barW - 4;
      const lowX = barX - lw - 4;
      if (lowX < width * 0.55) return;  // 位置不够就不画，别糊到频率标签上
      const grad = ctx.createLinearGradient(barX, 0, barX + barW, 0);
      for (let i = 0; i <= 4; i++) {
        const rgb = U.heatRGB(i / 4);
        grad.addColorStop(i / 4, "rgb(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + ")");
      }
      ctx.fillStyle = grad;
      ctx.fillRect(barX, y - 6, barW, barH);
      ctx.strokeStyle = "#1c2a41";
      ctx.strokeRect(barX + 0.5, y - 5.5, barW - 1, barH - 1);
      ctx.fillStyle = "#8697b4";
      ctx.fillText(low, lowX, y);
      ctx.fillText(high, barX + barW + 4, y);
    }

    dispose() {
      if (this.raf) cancelAnimationFrame(this.raf);
      this.raf = 0;
      this.host.innerHTML = "";
    }

    probe() {
      const g = this.groups[0];
      const perBand = this.groups.map((grp) => {
        let hot = 0;
        grp.items.forEach((item) => { if (this.heatColor(item.key)) hot += 1; });
        return { name: grp.name, windows: grp.items.length, hot: hot };
      });
      return {
        module: this.planKey ? Object.keys(this.plans).join("+") : null,
        plans: Object.keys(this.plans).length,
        groups: this.groups.map((x) => ({ name: x.name, lo: x.lo, hi: x.hi, windows: x.items.length })),
        windows: this.plan.length,
        index: this.index,
        passes: this.passes,
        visited: this.visited.size,
        hot: this.heatCells(),
        perBand: perBand,
        range: this.samples.length ? this.heatRange() : null,
        trail: this.trail.slice(-24),
        x: Math.round(this.x),
        targetX: Math.round(this.target),
        width: g ? Math.round(this.fit.width) : 0,
      };
    }
  }

  function roundRect(ctx, x, y, w, h, r) {
    if (typeof ctx.roundRect === "function") {
      ctx.beginPath();
      ctx.roundRect(x, y, w, h, r);
      return;
    }
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  /** 给 --selftest 用：喂一串频点，验证「到终点回卷」+「热力累积」。 */
  ScanBar.selftest = function () {
    const host = document.createElement("div");
    host.style.width = "600px";
    const bar = new ScanBar(host);
    bar.setPlans({ _selftest: [[100e6, 20e6], [120e6, 20e6], [140e6, 20e6], [160e6, 20e6]] });
    const bins = (v) => Array.from({ length: 32 }, () => v);
    const feed = (hz, v) => bar.update({ center_hz: hz, bw_hz: 20e6, module: "_selftest", bins: bins(v) });
    feed(100e6, 60); feed(120e6, 64); feed(140e6, 70); feed(160e6, 80);
    const atEnd = bar.probe();
    feed(100e6, 90);
    const wrapped = bar.probe();
    // 热力跨轮：第二轮把同一格抬 20 码，热力值必须上升（不是被重置）
    const before = bar.heat.get(100);
    feed(100e6, 110);
    const after = bar.heat.get(100);
    bar.dispose();
    return {
      plan: atEnd.windows,
      indexAtEnd: atEnd.index,
      visitedAtEnd: atEnd.visited,
      passesAfterWrap: wrapped.passes,
      visitedAfterWrap: wrapped.visited,
      xAfterWrap: wrapped.targetX,
      groups: atEnd.groups.length,
      hot: atEnd.hot,
      heatRose: !!(before && after && after.v > before.v),
    };
  };

  return ScanBar;
})();