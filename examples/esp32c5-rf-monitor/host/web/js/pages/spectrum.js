/* pages/spectrum.js —— 实时频谱：2D 曲线 + 3D 瀑布 + 2D 瀑布。
 *
 * 2D 瀑布的实现要点（之前坏过，别再改回去）：
 *   · 数据缓冲区是**独立画布** `wf.data`（宽 = 画布物理宽，高 = WF_ROWS 行），
 *     每帧把整块下移 1 行、顶部写新行；缓冲里**只放颜色，不画任何文字**。
 *   · 合成到可见画布时用 `setTransform(1,0,0,1,0,0)` 走**物理像素**再 drawImage。
 *     旧版按 CSS 像素拷贝自身，dpr=2 时每帧只拷左上 1/4 再放大 → 画面糊成一团、
 *     文字被反复复制成重影。
 *   · 文字（"最新"、频率刻度、色码范围）在合成之后才画，永远不参与滚动。
 *   · 对比度按本帧 min/max 自适应（噪声底 20~70 码时固定 0..255 会全变深蓝）。
 */
window.Pages = window.Pages || {};

window.Pages.spectrum = (function () {
  const WF_ROWS = 240;      // 2D 瀑布保留的帧数（1 帧 = 1 行）
  const PEAK_DECAY = 0.35;  // 峰值保持每帧衰减（码）

  let root = null;
  let spec = null;          // {canvas, ctx, width, height, dpr}
  let scanbar = null;       // 扫频进度条（游标）
  let info = null;          // 最近一次 info（含各模块的扫描计划）
  let wf2d = null;          // 见 makeWaterfall()
  let waterfall3d = null;
  let peak = null;
  let lastPayload = null;
  let drawn = 0;
  let resizeTimer = null;

  function onEnter(host) {
    root = host;
    spec = U.fitCanvas(host.querySelector("#spec-canvas"), 210);
    wf2d = makeWaterfall(host.querySelector("#wf-canvas"), 150);
    peak = null;

    scanbar = new ScanBar(host.querySelector("#scanbar"));
    applyPlan();

    waterfall3d = new Three3D.Waterfall(host.querySelector("#wf-3d"), { nFreq: 96, nTime: 110 });

    root.querySelector("#wf2d-title").textContent = T("spectrum.wf2d.title", { rows: WF_ROWS });
    if (lastPayload) onSpectrum(lastPayload);
    else drawSpectrum(null);

    window.addEventListener("resize", onResize);
  }

  function onResize() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!root) return;
      spec = U.fitCanvas(root.querySelector("#spec-canvas"), 210);
      wf2d = resizeWaterfall(wf2d, root.querySelector("#wf-canvas"), 150); // 保留历史，按新宽度重采样
      if (waterfall3d && waterfall3d.ok) waterfall3d.resize();
      if (scanbar) scanbar.resize();
      drawSpectrum(lastPayload);
      drawWaterfall2D(lastPayload);
    }, 180);
  }

  function onLeave() {
    window.removeEventListener("resize", onResize);
    if (waterfall3d) waterfall3d.dispose();
    waterfall3d = null;
    if (scanbar) scanbar.dispose();
    scanbar = null;
    wf2d = null;
    root = null;
  }

  // ---- 2D 瀑布 -----------------------------------------------------------
  function makeWaterfall(canvas, cssHeight) {
    const fit = U.fitCanvas(canvas, cssHeight);
    const data = document.createElement("canvas");
    data.width = Math.max(1, canvas.width);
    data.height = WF_ROWS;
    const dctx = data.getContext("2d");
    dctx.fillStyle = "#070a10";
    dctx.fillRect(0, 0, data.width, data.height);
    return {
      fit: fit,
      data: data,
      dctx: dctx,
      row: dctx.createImageData(data.width, 1),
      range: new U.AutoRange(14, 0.25),
      frames: 0,
    };
  }

  /** 画布尺寸变了：把已有历史按新宽度缩放过去，不清空（弹出预览/缩放时有用） */
  function resizeWaterfall(old, canvas, cssHeight) {
    const fit = U.fitCanvas(canvas, cssHeight);
    const data = document.createElement("canvas");
    data.width = Math.max(1, canvas.width);
    data.height = WF_ROWS;
    const dctx = data.getContext("2d");
    dctx.fillStyle = "#070a10";
    dctx.fillRect(0, 0, data.width, data.height);
    if (old && old.data && old.data.width > 0) {
      dctx.imageSmoothingEnabled = false;
      dctx.drawImage(old.data, 0, 0, old.data.width, WF_ROWS, 0, 0, data.width, WF_ROWS);
    }
    return {
      fit: fit,
      data: data,
      dctx: dctx,
      row: dctx.createImageData(data.width, 1),
      range: old ? old.range : new U.AutoRange(14, 0.25),
      frames: old ? old.frames : 0,
      axis: old ? old.axis : null,
    };
  }

  function pushWaterfall(payload) {
    if (!wf2d || !payload || !payload.bins || !payload.bins.length) return;
    const bins = payload.bins;
    const W = wf2d.data.width;
    wf2d.range.update(bins);
    const px = wf2d.row.data;
    for (let x = 0; x < W; x++) {
      const idx = Math.min(bins.length - 1, Math.floor((x / W) * bins.length));
      const c = U.heatRGB(wf2d.range.norm(bins[idx]));
      const o = x * 4;
      px[o] = c[0]; px[o + 1] = c[1]; px[o + 2] = c[2]; px[o + 3] = 255;
    }
    // 整体下移一行（自拷贝，缓冲区里没有文字，不会留下重影）
    wf2d.dctx.drawImage(wf2d.data, 0, 0, W, WF_ROWS - 1, 0, 1, W, WF_ROWS - 1);
    wf2d.dctx.putImageData(wf2d.row, 0, 0);
    wf2d.frames += 1;
  }

  function drawWaterfall2D(payload) {
    if (!wf2d) return;
    const { ctx, width, height, dpr } = wf2d.fit;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#070a10";
    ctx.fillRect(0, 0, width, height);
    ctx.restore();

    const padT = 15;
    const padB = 19;  // 频率轴：轴线 + 刻度 + 标签，别和刻度挤在一起
    const plotH = Math.max(8, height - padT - padB);

    // 数据：物理像素直出，关闭插值，保证一行一帧
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(
      wf2d.data,
      0, 0, wf2d.data.width, WF_ROWS,
      0, Math.round(padT * dpr), Math.round(width * dpr), Math.round(plotH * dpr)
    );
    ctx.restore();

    ctx.font = "10px Consolas, monospace";
    // 顶部左侧：最新
    ctx.fillStyle = "rgba(7,10,16,0.72)";
    const latest = T("spectrum.latest");
    ctx.fillRect(3, 3, ctx.measureText(latest).width + 8, 12);
    ctx.fillStyle = "#8697b4";
    ctx.fillText(latest, 7, 12);

    if (!payload || !payload.bins || !payload.bins.length) return;

    // 顶部右侧：本帧自适应范围（只影响显示，判决仍用相对抬升口径）
    const [lo, hi] = wf2d.range.span;
    const rangeText = T("spectrum.range", { lo: lo, hi: hi });
    ctx.fillStyle = "rgba(7,10,16,0.72)";
    ctx.fillRect(width - ctx.measureText(rangeText).width - 11, 3, ctx.measureText(rangeText).width + 8, 12);
    ctx.fillStyle = "#8697b4";
    ctx.fillText(rangeText, width - ctx.measureText(rangeText).width - 7, 12);

    // 横轴频率轴（简易版）：轴线 + 5 个刻度 + MHz 标签 + 中心频点标记
    const fLo = payload.freq_lo_hz;
    const fHi = payload.freq_hi_hz;
    const axisY = height - padB + 2;
    const ticks = [];
    ctx.strokeStyle = "rgba(134,151,180,0.35)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, axisY + 0.5);
    ctx.lineTo(width, axisY + 0.5);
    ctx.stroke();
    ctx.textAlign = "center";
    for (let k = 0; k <= 4; k++) {
      const x = Math.round((width * k) / 4) + 0.5;
      const f = fLo + ((fHi - fLo) * k) / 4;
      ticks.push(Math.round(f / 1e6));
      ctx.beginPath();
      ctx.moveTo(x, axisY);
      ctx.lineTo(x, axisY + 4);
      ctx.stroke();
      ctx.fillStyle = k === 2 ? "#9db3d4" : "#5f7391";
      const label = (f / 1e6).toFixed(0);
      ctx.fillText(label, Math.min(width - 16, Math.max(16, x)), height - 2);
    }
    // 单位 + 中心频点（轴只描述**最新一行**；扫频时旧行的窗可能不同）
    ctx.textAlign = "left";
    ctx.fillStyle = "#5f7391";
    ctx.fillText(T("spectrum.mhz"), 2, height - padB - 3);
    wf2d.axis = {
      lo: Math.round(fLo / 1e6),
      hi: Math.round(fHi / 1e6),
      center: Math.round((fLo + fHi) / 2 / 1e6),
      ticks: ticks,
    };

    // 当前帧峰值位置的竖线（和曲线上的黄点对齐）
    let maxIdx = 0;
    for (let i = 1; i < payload.bins.length; i++) if (payload.bins[i] > payload.bins[maxIdx]) maxIdx = i;
    const px = (maxIdx / Math.max(1, payload.bins.length - 1)) * width;
    ctx.strokeStyle = "rgba(251,191,36,0.55)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, padT + 2);
    ctx.lineTo(px, padT + plotH - 2);
    ctx.stroke();
  }

  // ---- 扫描计划（来自 Python 的 scan_requests，前端不写死频段）-------------
  /**
   * 进度条要画**全频段**：把 `info.plans` 里所有模块的计划一起交给 ScanBar，
   * 由它做并集、按模块认领自己的频点。早期只跟「最宽的那一个模块」，
   * 结果是别的模块（wifi 2412 / 抓档 5180 / 守卫 5745）扫到的窗在这张图上
   * 根本没有格子，用户看到的就是「没在扫全频段」。
   */
  function applyPlan() {
    if (!scanbar || !info) return;
    const plans = info.plans || {};
    if (!Object.keys(plans).length) return;
    scanbar.setPlans(plans);
  }

  function onInfo(payload) {
    info = payload;
    applyPlan();
  }

  // ---- 数据入口 ----------------------------------------------------------
  function onSpectrum(payload) {
    if (!payload || !payload.bins) return;
    lastPayload = payload;
    drawn += 1;

    if (!peak || peak.length !== payload.bins.length) {
      peak = payload.bins.slice();
    } else {
      for (let i = 0; i < peak.length; i++) peak[i] = Math.max(payload.bins[i], peak[i] - PEAK_DECAY);
    }

    // 所有模块的帧都喂进度条：ScanBar 内部按「该模块自己的计划窗」认领，
    // 对不上就只更新频点文字、不动游标（不会像早期那样来回跳）。
    if (scanbar) scanbar.update(payload);
    if (waterfall3d && waterfall3d.ok) waterfall3d.push(payload.bins);
    pushWaterfall(payload);
    drawWaterfall2D(payload);
    drawSpectrum(payload);
  }

  // ---- 2D 曲线 -----------------------------------------------------------
  function drawSpectrum(payload) {
    if (!spec) return;
    const { ctx, width, height } = spec;
    const padL = 46, padR = 8, padT = 10, padB = 22;
    const plotW = Math.max(10, width - padL - padR);
    const plotH = Math.max(10, height - padT - padB);

    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#070a10";
    ctx.fillRect(0, 0, width, height);
    ctx.font = "10px Consolas, monospace";

    if (!payload || !payload.bins || !payload.bins.length) {
      ctx.fillStyle = "#5f7391";
      ctx.fillText(T("wifi.card.wait"), padL, padT + 16);
      return;
    }

    const bins = payload.bins;
    let lo = 255, hi = 0;
    for (const v of bins) { if (v < lo) lo = v; if (v > hi) hi = v; }
    lo = Math.max(0, Math.min(lo, hi - 8) - 4);
    hi = Math.min(255, hi + 6);
    if (hi - lo < 12) hi = Math.min(255, lo + 12);
    const xOf = (i) => padL + (i / Math.max(1, bins.length - 1)) * plotW;
    const yOf = (v) => padT + plotH - ((v - lo) / (hi - lo)) * plotH;

    // 网格 + 纵轴刻度
    ctx.strokeStyle = "#16233a";
    ctx.fillStyle = "#5f7391";
    ctx.lineWidth = 1;
    for (let k = 0; k <= 4; k++) {
      const v = lo + ((hi - lo) * k) / 4;
      const y = yOf(v);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(width - padR, y); ctx.stroke();
      ctx.fillText(v.toFixed(0), 6, y + 3);
    }
    // 横轴刻度（频率）
    const fLo = payload.freq_lo_hz, fHi = payload.freq_hi_hz;
    for (let k = 0; k <= 4; k++) {
      const x = padL + (plotW * k) / 4;
      const f = fLo + ((fHi - fLo) * k) / 4;
      ctx.strokeStyle = "#16233a";
      ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + plotH); ctx.stroke();
      ctx.fillStyle = "#5f7391";
      const label = (f / 1e6).toFixed(1);
      ctx.fillText(label, x - ctx.measureText(label).width / 2, height - 7);
    }
    ctx.fillStyle = "#5f7391";
    ctx.fillText(T("spectrum.mhz"), width - padR - 24, height - 7);

    // 峰值保持线
    if (peak) {
      ctx.strokeStyle = "#7c3aed";
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      peak.forEach((v, i) => (i ? ctx.lineTo(xOf(i), yOf(v)) : ctx.moveTo(xOf(i), yOf(v))));
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // 当前帧：填充 + 描边
    const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
    grad.addColorStop(0, "rgba(56,189,248,0.42)");
    grad.addColorStop(1, "rgba(56,189,248,0.02)");
    ctx.beginPath();
    ctx.moveTo(xOf(0), padT + plotH);
    bins.forEach((v, i) => ctx.lineTo(xOf(i), yOf(v)));
    ctx.lineTo(xOf(bins.length - 1), padT + plotH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    bins.forEach((v, i) => (i ? ctx.lineTo(xOf(i), yOf(v)) : ctx.moveTo(xOf(i), yOf(v))));
    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 1.4;
    ctx.stroke();

    // 峰值点标记
    let maxIdx = 0;
    for (let i = 1; i < bins.length; i++) if (bins[i] > bins[maxIdx]) maxIdx = i;
    const px = xOf(maxIdx), py = yOf(bins[maxIdx]);
    ctx.fillStyle = "#fbbf24";
    ctx.beginPath(); ctx.arc(px, py, 3, 0, Math.PI * 2); ctx.fill();
    const peakFreq = fLo + ((fHi - fLo) * maxIdx) / Math.max(1, bins.length - 1);
    const text = T("spectrum.peak", { freq: (peakFreq / 1e6).toFixed(2) + " MHz", code: bins[maxIdx].toFixed(0) });
    ctx.fillStyle = "#fbbf24";
    ctx.fillText(text, Math.min(px + 6, width - padR - ctx.measureText(text).width), Math.max(12, py - 8));

    // 角标：模块 / 中心 / 带宽 / 点数
    ctx.fillStyle = "#8697b4";
    ctx.fillText(
      T("spectrum.tag", {
        module: payload.module || "-",
        center: (payload.center_hz / 1e6).toFixed(1) + " MHz",
        bw: (payload.bw_hz / 1e6).toFixed(0) + " MHz",
        raw: payload.raw_bins,
        out: bins.length,
      }),
      padL + 4,
      padT + 11
    );
  }

  /**
   * 2D 瀑布自检：直接从**数据缓冲**读像素，证明「每帧真的多了一行、旧行真的在下移」。
   * 只给 selftest 用，正常运行时不会调用（读 2MB 像素很贵）。
   */
  function wfStats() {
    if (!wf2d) return null;
    const W = wf2d.data.width;
    const img = wf2d.dctx.getImageData(0, 0, W, WF_ROWS).data;
    const rowMean = (row) => {
      let sum = 0;
      const base = row * W * 4;
      for (let x = 0; x < W; x += 7) sum += img[base + x * 4] + img[base + x * 4 + 1] + img[base + x * 4 + 2];
      return Math.round(sum / Math.ceil(W / 7));
    };
    const means = [];
    let painted = 0;
    let distinct = 0;
    let prev = null;
    for (let r = 0; r < WF_ROWS; r++) {
      const m = rowMean(r);
      means.push(m);
      if (m > 60) painted += 1;                    // 背景 #070a10 三通道和 ≈ 33，画过的行明显更高
      if (prev !== null && Math.abs(m - prev) > 1) distinct += 1;
      prev = m;
    }
    const avg = (arr) => arr.reduce((a, b) => a + b, 0) / Math.max(1, arr.length);
    return {
      rows: WF_ROWS,
      frames: wf2d.frames,
      paintedRows: painted,
      distinctRows: distinct,
      topMean: Math.round(avg(means.slice(0, 8))),      // 最新 8 行
      bottomMean: Math.round(avg(means.slice(-8))),     // 最旧 8 行
      flatRows: means.filter((m) => m <= 40).length,
      imagesTooMany: img.length / 4 !== W * WF_ROWS,
    };
  }

  // ---- 语言切换 -----------------------------------------------------------
  function onLang() {
    if (!root) return;
    root.querySelector("#wf2d-title").textContent = T("spectrum.wf2d.title", { rows: WF_ROWS });
    drawSpectrum(lastPayload);
    drawWaterfall2D(lastPayload);
    if (scanbar) scanbar.draw();
  }

  return {
    onEnter,
    onLeave,
    onSpectrum,
    onLang,
    onInfo,
    probe: () => ({
      frames: drawn,
      scanbar: scanbar ? scanbar.probe() : null,
      wf2d: wf2d ? Object.assign({ rows: WF_ROWS, range: wf2d.range.span }, wfStats()) : null,
      wf2dAxis: wf2d ? wf2d.axis : null,
      three: waterfall3d && waterfall3d.ok ? { pushFrames: waterfall3d.frames } : { ok: false },
    }),
  };
})();