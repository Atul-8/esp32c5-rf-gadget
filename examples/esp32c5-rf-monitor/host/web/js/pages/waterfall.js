/* pages/waterfall.js —— 全频段时频瀑布（一行 = 一轮完整轮扫）。
 *
 * 数据来自 modes/waterfall.py 的 web_payload()（经 host/session.py 打包进 tables）：
 *   {rows, cols, sweeps, frames, code_lo/code_hi, row_ts[], bands[{id,label,lo_mhz,hi_mhz,cells}]}
 *   cells 是 base64 —— 一格一字节（功率码 0..255），比 JSON 整数数组小 4 倍。
 *
 * 三条口径与 PNG（output/heatmap.py）保持一致，别在这里改：
 *   1. 同一格被多窗扫到时取**最大保持**（后端 fill_row 已经算好）；
 *   2. 0 码 = 这一格没扫到 → 画最暗底色，不当成"信号为 0"；
 *   3. 纵轴自上而下变旧；时间刻度取每轮首帧时间戳（同一行内不同频段扫到的时刻不同）。
 *
 * 界面只做显示与翻译：判决、分档、最大保持全在 Python 侧。
 */
window.Pages = window.Pages || {};

window.Pages.waterfall = (function () {
  const CSS_HEIGHT = 300;
  const PAD = { left: 46, right: 10, top: 20, bottom: 26 };
  const GAP = 12;                 // 三个频段之间的缝
  const BG = [8, 10, 16];         // 未扫到 / 背景色（与 PNG 的 Canvas 底色一致）
  const UNITY = new Uint8Array(0);
  //: 每段以**本段 p10 底噪**为 0 点，高出底噪 SPAN_CODES 码即满亮。
  //: 为什么不用 p5..p95 两端拉伸：一段里只要有强信号，p95 就被抬到信号上，
  //: 底噪全挤到最暗 → 整段看着像"没扫到"（真板 5G HIGH 就是这么被看成空白的：
  //: p10 底噪 29 码、信号到 186，按 p5..p95 拉伸后底噪只剩 2% 亮度）。
  //: 底噪口径与后端判据一致（core/classifier 的 ref_db 取 p10）。
  const SPAN_CODES = 30;
  const REF_PCT = 0.10;

  let root = null;
  let canvas = null;
  let fit = null;
  let last = null;
  let layout = null;              // {x0, segW, y0, dataH, cols, rows, cellW, cellH}
  let cells = [];                 // 每个频段一份 Uint8Array（已 base64 解码）
  let range = null;               // {lo, hi}：全图拉伸范围（平滑跟随，避免闪）
  const bandRange = {};           // 每段自己的 {ref}：本段底噪基准，见 fitBandRanges()
  const bandMeta = {};            // 每段诊断：{ref, n, max} —— 画在段标题旁，好一眼看出有没有数据
  let hover = null;               // {band, col, row}
  let resizeTimer = null;
  let report = null;
  // ---- 记忆热力（一个频段格 = 一个字节）----------------------------------
  let heatCanvas = null;
  let heatFit = null;
  let heatData = null;            // Uint8Array，长度 = 扫描计划的窗数
  let heatHot = 0;

  // ---- 生命周期 ----------------------------------------------------------
  function onEnter(host) {
    root = host;
    canvas = host.querySelector("#wf-full-canvas");
    fit = U.fitCanvas(canvas, CSS_HEIGHT);
    heatCanvas = host.querySelector("#wf-heat-canvas");
    heatFit = U.fitCanvas(heatCanvas, 56);
    bindButtons();
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeaveCanvas);
    window.addEventListener("resize", onResize);
    renderBanner();
    fillNotes();
    // 下面三处没有 `data-i18n`（它们的内容是动态的，挂上就会被 `Router.navigate`
    // 末尾那次 `I18N.apply(host)` 刷回 HTML 里的中文），所以初始文案在这里
    // 按当前语言写一次；有数据时 render()/showReport() 会盖掉。
    if (!last) {
      setText("#wf-sweep-note", T("wf.card.wait"));
      setText("#wf-heat-info", T("wf.heat.idle"));
    }
    if (!report) setText("#wf-files", T("wf.report.idle"));
    if (last) render(last);
    if (report) showReport(report);
  }

  function onLeave() {
    window.removeEventListener("resize", onResize);
    if (canvas) {
      canvas.removeEventListener("mousemove", onMove);
      canvas.removeEventListener("mouseleave", onLeaveCanvas);
    }
    root = null;
    canvas = null;
    fit = null;
    heatCanvas = null;
    heatFit = null;
    layout = null;
    hover = null;
  }

  function onResize() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!root || !canvas) return;
      fit = U.fitCanvas(canvas, CSS_HEIGHT);
      if (last) render(last);
    }, 180);
  }

  // ---- 数据 --------------------------------------------------------------
  function onTables(tables) {
    const payload = pickPayload(tables);
    if (!payload) return;
    last = payload;
    if (root) {
      // 说明列表只在数据换代时重建（跟鼠标读数一样高的频率重建 DOM 没必要）
      fillNotes();
      render(payload);
    }
  }

  function onLang() {
    if (!root) return;
    renderBanner();
    fillNotes();
    if (last) render(last);
    if (report) showReport(report);
  }

  function pickPayload(tables) {
    if (!tables || !tables.plugins) return null;
    const item = tables.plugins.find((p) => p.payload && p.payload.kind === "waterfall");
    return item ? item.payload : null;
  }

  function decode(b64) {
    const bin = atob(b64 || "");
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  // ---- 渲染 --------------------------------------------------------------
  function render(payload) {
    if (!root || !canvas) return;
    if (!fit) fit = U.fitCanvas(canvas, CSS_HEIGHT);
    const ctx = fit.ctx;
    if (!ctx) return;
    const W = fit.width;
    const H = fit.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#070a10";
    ctx.fillRect(0, 0, W, H);

    const rows = payload.rows || 0;
    const cols = payload.cols || 0;
    const bands = payload.bands || [];
    updateCards(payload);
    fitRange(payload);

    if (!rows || !cols || !bands.length) {
      centreText(ctx, W, H, T("wf.wait"));
      layout = null;
      return;
    }

    cells = bands.map((b) => (b.cells ? decode(b.cells) : UNITY));
    fitBandRanges(bands, cells);

    const dataW = W - PAD.left - PAD.right - GAP * (bands.length - 1);
    const segW = Math.max(40, Math.floor(dataW / bands.length));
    const dataH = Math.max(40, H - PAD.top - PAD.bottom);
    layout = {
      x0: PAD.left,
      segW: segW,
      y0: PAD.top,
      dataH: dataH,
      cols: cols,
      rows: rows,
      segments: bands.length,
      cellW: segW / cols,
      cellH: dataH / rows,
    };

    bands.forEach((band, index) => {
      const x0 = Math.round(PAD.left + index * (segW + GAP));
      drawBand(ctx, band, cells[index], x0, PAD.top, segW, dataH, cols, rows);
      drawBandAxis(ctx, band, x0, segW, PAD.top + dataH + 4);
      const meta = bandMeta[band.id];
      if (meta && !meta.n) {
        centreTextIn(ctx, x0, PAD.top, segW, dataH, T("wf.band.emptyBox"));
      }
    });
    drawLegend(ctx, W, H);
    drawTimeAxis(ctx, payload, PAD.top, dataH);
    markIncomplete(ctx, payload);
    drawCursor(ctx);
    drawHeatMemory(payload);
  }

  // ---- 记忆热力 ----------------------------------------------------------
  //: 后端只维护一个 bytes 数组：扫到信号 +1（封顶 255）、扫不到 -1（触底 0）。
  //: 这里直接按 0..255 线性染色，不做拉伸、不做 EMA —— 值本身就是“多久没扫到过信号”。
  function drawHeatMemory(payload) {
    if (!heatCanvas || !heatFit) return;
    const ctx = heatFit.ctx;
    if (!ctx) return;
    const W = heatFit.width;
    const H = heatFit.height;
    heatData = payload && payload.heat ? decode(payload.heat) : null;
    heatHot = (payload && payload.heat_hot) || 0;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#070a10";
    ctx.fillRect(0, 0, W, H);
    if (!heatData || !heatData.length) {
      centreText(ctx, W, H, T("wf.heat.idle"));
      setText("#wf-heat-info", T("wf.heat.idle"));
      return;
    }
    const n = heatData.length;
    const cellW = W / n;
    let max = 0;
    for (let i = 0; i < n; i += 1) {
      const v = heatData[i];
      if (v > max) max = v;
      // 三色阶：冷（深蓝）→ 温（青绿）→ 热（黄白），按 v/255 线性插值
      const t = v / 255;
      const r = Math.round(10 + t * (250 - 10));
      const g = Math.round(20 + t * (240 - 20));
      const b = Math.round(45 + t * (120 - 45));
      ctx.fillStyle = "rgb(" + r + "," + g + "," + b + ")";
      const x = Math.round(i * cellW);
      const w = Math.max(1, Math.round((i + 1) * cellW) - x - (cellW > 3 ? 1 : 0));
      ctx.fillRect(x, 0, w, H);
    }
    setText("#wf-heat-info", T("wf.heat.info", { n: n, max: max, hot: heatHot }));
  }

  function drawBand(ctx, band, data, x0, y0, w, h, cols, rows) {
    const img = ctx.createImageData(w, h);
    const px = img.data;
    const rng = bandRange[band.id] || range;
    for (let y = 0; y < h; y++) {
      const row = Math.min(rows - 1, Math.floor((y / h) * rows));
      const base = row * cols;
      for (let x = 0; x < w; x++) {
        const col = Math.min(cols - 1, Math.floor((x / w) * cols));
        const code = data.length ? data[base + col] : 0;
        const rgb = colorFor(code, rng);
        const i = (y * w + x) * 4;
        px[i] = rgb[0];
        px[i + 1] = rgb[1];
        px[i + 2] = rgb[2];
        px[i + 3] = 255;
      }
    }
    ctx.putImageData(img, x0, y0);
    ctx.strokeStyle = "#22303f";
    ctx.strokeRect(x0 + 0.5, y0 + 0.5, w - 1, h - 1);
  }

  /**
   * 0 码 = 没扫到（最暗底色）；否则按「高出本段底噪多少码」映射到热力色带。
   *
   * 底噪 → 最暗，底噪 + SPAN_CODES → 满亮，中间线性。这样：
   *   - 各段之间视觉一致：谁的底噪高就整体平移，不会因为"这段没信号"反而更花；
   *   - 和判据口径一致（后端相对抬升也是拿 p10 底噪当参考）。
   */
  function colorFor(code, rng) {
    if (!code) return BG;
    const ref = (rng && typeof rng.ref === "number") ? rng.ref : 0;
    const t = (code - ref) / SPAN_CODES;
    return U.heatRGB(Math.max(0, Math.min(1, t)));
  }

  /**
   * 每个频段**各自**拉伸（直方图 p5..p95，最小跨度 8 码），并做慢速平滑。
   *
   * 为什么不能全图共用一条范围：三个频段的绝对电平差得很远（2.4G 底噪高、
   * 5G HIGH 靠窗沿衰减），共用一条范围时电平低的那一段整片压到最暗，
   * 看着就像"5G HIGH 没扫到"（用户真板就是这么报的）。分段拉伸以后每段都能看见
   * 自己的结构；跨段比绝对值请看每段标题旁标出来的码值范围。
   */
  function fitBandRanges(bands, arrays) {
    bands.forEach((band, i) => {
      const data = arrays[i] || UNITY;
      const hist = new Uint32Array(256);
      let n = 0;
      for (let k = 0; k < data.length; k += 3) {   // 抽样 1/3，够稳且快
        const v = data[k];
        if (!v) continue;
        hist[v] += 1;
        n += 1;
      }
      if (!n) {
        // 这一轮这段真的没扫到：保留上一次的底噪基准，别把颜色跳成一片黑；
        // 但诊断信息要如实清零 —— 段标题上会写出「本段暂无可视数据」
        bandMeta[band.id] = { ref: bandRange[band.id] ? Math.round(bandRange[band.id].ref) : 0, n: 0, max: 0 };
        return;
      }
      const pick = (frac) => {
        const want = Math.max(1, Math.round(n * frac));
        let acc = 0;
        for (let v = 0; v < 256; v++) {
          acc += hist[v];
          if (acc >= want) return v;
        }
        return 255;
      };
      const ref = pick(REF_PCT);
      const prev = bandRange[band.id];
      const smooth = prev && typeof prev.ref === "number"
        ? prev.ref + (ref - prev.ref) * 0.3
        : ref;
      bandRange[band.id] = { ref: smooth };
      // 诊断用：非零格数 / 最大值 —— 段标题旁标出来，"没数据"和"数据太暗"就分得清了
      let mx = 0;
      for (let v = 255; v > 0; v--) {
        if (hist[v]) { mx = v; break; }
      }
      bandMeta[band.id] = { ref: Math.round(smooth), n: n, max: mx };
    });
  }

  /** 色带图例（右上角）：说明「亮 = 挤」以及每段是各自拉伸的 */
  function drawLegend(ctx, W, H) {
    const ids = last && last.bands ? last.bands.map((b) => b.id) : [];
    if (!ids.length) return;
    const barW = 66;
    const barH = 8;
    const x = W - PAD.right - barW;
    const y = PAD.top + 2;
    const grad = ctx.createLinearGradient(x, 0, x + barW, 0);
    for (let i = 0; i <= 4; i++) {
      const rgb = U.heatRGB(i / 4);
      grad.addColorStop(i / 4, "rgb(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + ")");
    }
    ctx.save();
    ctx.fillStyle = grad;
    ctx.fillRect(x, y, barW, barH);
    ctx.strokeStyle = "#22303f";
    ctx.strokeRect(x + 0.5, y + 0.5, barW - 1, barH - 1);
    ctx.font = "9px ui-monospace, Consolas, monospace";
    ctx.textBaseline = "top";
    ctx.fillStyle = "#8697b4";
    const title = T("wf.legend.title");
    const tw = ctx.measureText(title).width;
    ctx.fillText(title, Math.max(PAD.left, x - tw), y - 1);
    ctx.restore();
  }

  function fitRange(payload) {
    const next = { lo: Number(payload.code_lo) || 0, hi: Number(payload.code_hi) || 0 };
    if (next.hi - next.lo < 8) next.hi = next.lo + 8;
    if (!range) {
      range = next;
      return;
    }
    range.lo += (next.lo - range.lo) * 0.25;
    range.hi += (next.hi - range.hi) * 0.25;
    if (range.hi - range.lo < 8) range.hi = range.lo + 8;
  }

  function drawBandAxis(ctx, band, x0, w, y) {
    ctx.font = "10px ui-monospace, Consolas, monospace";
    ctx.fillStyle = "#8697b4";
    ctx.textBaseline = "top";
    const meta = bandMeta[band.id];
    let label = band.label;
    if (meta) {
      // 有值：底噪 / 最大值；没值：直接说这段没有可视数据（不要去猜）
      label += meta.n
        ? "  " + T("wf.band.meta", { ref: meta.ref, max: meta.max })
        : "  " + T("wf.band.empty");
    }
    ctx.fillText(label, x0, 4);
    const lo = Number(band.lo_mhz) || 0;
    const hi = Number(band.hi_mhz) || 0;
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const x = x0 + Math.round((w * i) / ticks);
      const mhz = lo + ((hi - lo) * i) / ticks;
      ctx.strokeStyle = "#22303f";
      ctx.beginPath();
      ctx.moveTo(x + 0.5, y - 3);
      ctx.lineTo(x + 0.5, y + 1);
      ctx.stroke();
      const text = mhz.toFixed(0);
      ctx.fillStyle = "#8697b4";
      const tw = ctx.measureText(text).width;
      ctx.fillText(text, Math.min(x0 + w - tw, Math.max(x0, x - tw / 2)), y + 3);
    }
  }

  /** 最后一行经常是"扫了一半"，缺的格子在后端已经是 0 码；这里只标出来，不猜数据 */
  function incompleteRows(payload) {
    const total = payload.windows || 0;
    const rows = payload.row_frames || [];
    return rows.map((n, i) => (n < total ? i : -1)).filter((i) => i >= 0);
  }

  function drawTimeAxis(ctx, payload, y0, h) {
    ctx.font = "10px ui-monospace, Consolas, monospace";
    ctx.fillStyle = "#8697b4";
    ctx.textBaseline = "middle";
    const ts = payload.row_ts || [];
    const rows = payload.rows || 0;
    if (!rows || !ts.length) return;
    const picks = Math.min(5, rows);
    for (let i = 0; i < picks; i++) {
      const row = Math.round((i * (rows - 1)) / Math.max(1, picks - 1));
      const y = y0 + ((row + 0.5) * h) / rows;
      const text = "+" + mmss(ts[row] || 0);
      ctx.fillText(text, 4, y);
      ctx.strokeStyle = "#22303f";
      ctx.beginPath();
      ctx.moveTo(PAD.left - 4, y + 0.5);
      ctx.lineTo(PAD.left - 1, y + 0.5);
      ctx.stroke();
    }
  }

  function markIncomplete(ctx, payload) {
    if (!layout) return;
    const bad = incompleteRows(payload);
    if (!bad.length) return;
    ctx.save();
    ctx.strokeStyle = "rgba(251,191,36,.55)";
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 1;
    bad.forEach((row) => {
      const y = layout.y0 + row * layout.cellH + 0.5;
      ctx.beginPath();
      ctx.moveTo(layout.x0, y);
      const right = layout.x0 + layout.segments * (layout.segW + GAP) - GAP;
      ctx.lineTo(right, y);
      ctx.stroke();
    });
    ctx.restore();
  }

  function drawCursor(ctx) {
    if (!hover || !layout) return;
    const x = layout.x0 + hover.band * (layout.segW + GAP);
    const cx = x + (hover.col + 0.5) * layout.cellW;
    const cy = layout.y0 + (hover.row + 0.5) * layout.cellH;
    ctx.strokeStyle = "rgba(184,236,255,.75)";
    ctx.lineWidth = 1;
    ctx.strokeRect(Math.round(cx) - 3.5, layout.y0 + 0.5, 7, layout.dataH);
    ctx.strokeRect(x + 0.5, Math.round(cy) - 0.5, layout.segW, 1);
  }

  function centreTextIn(ctx, x0, y0, w, h, text) {
    ctx.save();
    ctx.font = "11px ui-monospace, Consolas, monospace";
    ctx.fillStyle = "rgba(134,151,180,.85)";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, x0 + w / 2, y0 + h / 2);
    ctx.restore();
  }

  function centreText(ctx, W, H, text) {
    ctx.font = "12px ui-monospace, Consolas, monospace";
    ctx.fillStyle = "#8697b4";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, W / 2, H / 2);
    ctx.textAlign = "start";
  }

  // ---- 交互 --------------------------------------------------------------
  function onMove(ev) {
    if (!layout || !last) return;
    const rect = canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const y = ev.clientY - rect.top;
    const bandIndex = Math.floor((x - layout.x0) / (layout.segW + GAP));
    const bandX = x - (layout.x0 + bandIndex * (layout.segW + GAP));
    if (bandIndex < 0 || bandIndex >= last.bands.length || bandX < 0 || bandX > layout.segW) {
      onLeaveCanvas();
      return;
    }
    const rowY = y - layout.y0;
    if (rowY < 0 || rowY > layout.dataH) {
      onLeaveCanvas();
      return;
    }
    const col = Math.max(0, Math.min(layout.cols - 1, Math.floor((bandX / layout.segW) * layout.cols)));
    const row = Math.max(0, Math.min(layout.rows - 1, Math.floor((rowY / layout.dataH) * layout.rows)));
    hover = { band: bandIndex, col: col, row: row };
    const band = last.bands[bandIndex];
    const lo = Number(band.lo_mhz) * 1e6;
    const hi = Number(band.hi_mhz) * 1e6;
    const freq = lo + ((col + 0.5) / layout.cols) * (hi - lo);
    const data = cells[bandIndex] || UNITY;
    const code = data.length ? data[row * layout.cols + col] : 0;
    const ts = (last.row_ts || [])[row] || 0;
    const label = root.querySelector("#wf-cursor");
    if (label) {
      label.textContent = T("wf.cursor", {
        band: band.label,
        freq: U.fmtHz(freq),
        code: code,
        row: row + 1,
        rows: layout.rows,
        t: mmss(ts),
      });
    }
    if (fit && fit.ctx) {
      render(last);
    }
  }

  function onLeaveCanvas() {
    hover = null;
    if (root) {
      const label = root.querySelector("#wf-cursor");
      if (label) label.textContent = T("wf.cursor.idle");
    }
    if (last) render(last);
  }

  // ---- 卡片 / 说明 -------------------------------------------------------
  function updateCards(payload) {
    // 卡片显示**实测墙钟**周期（多模块分时会比"窗数×驻留"长），没有两轮以上就退回射频时间
    const wall = Number(payload.row_period_s) || 0;
    const rf = Number(payload.sweep_s) || 0;
    setText("#wf-sweep-s", (wall > 0 ? wall : rf).toFixed(1) + " s");
    const note = T("wf.card.sweepNote", {
      windows: payload.windows || 0,
      dwell: payload.dwell_ms || 0,
      rf: rf.toFixed(1),
    });
    setText("#wf-sweep-note", wall > 0 ? note + " · " + T("wf.card.sweepWall", { wall: wall.toFixed(1) }) : note);
    setText("#wf-sweeps", String(payload.sweeps || 0));
    // 「当前这一轮扫到哪了」：扫满才画成一行，所以这里要单独说进度，
    // 否则用户只能看到「已扫 2 轮」而图上只有 2 行，以为丢了数据
    const pending = Number(payload.pending_windows) || 0;
    setText(
      "#wf-sweeps-note",
      pending > 0
        ? T("wf.card.sweepsPending", { done: pending, total: payload.windows || 0 })
        : T("wf.card.sweepsNote")
    );
    setText("#wf-windows", String(payload.windows || 0));
    setText("#wf-window-note", T("wf.card.windowsNote", {
      bw: payload.bw_mhz || 0,
      step: payload.step_mhz || 0,
    }));
    setText("#wf-buffer", String(payload.ring_frames || 0));
    setText("#wf-buffer-note", T("wf.card.bufferNote", { frames: payload.frames || 0 }));
  }

  function renderBanner() {
    const banner = root && root.querySelector("#wf-banner");
    if (!banner) return;
    banner.innerHTML = "";
    [T("wf.banner.row"), T("wf.banner.code"), T("wf.banner.sweep")].forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      banner.appendChild(li);
    });
  }

  function fillNotes() {
    const notes = root && root.querySelector("#wf-notes");
    if (!notes || !last) return;
    notes.innerHTML = "";
    const cover = (last.cover || []).map((c) => c.label + " " + c.windows + " " + T("wf.notes.window")).join(" / ");
    const lines = [
      T("wf.notes.rows", { rows: last.rows || 0, sweeps: last.sweeps || 0 }),
      // 一轮扫满才落一行：把「当前这轮已经扫了多少窗」说清楚，别让人以为 5G HIGH 丢了
      T("wf.notes.pending", {
        done: Number(last.pending_windows) || 0,
        total: last.windows || 0,
      }),
      T("wf.notes.cover", { cover: cover }),
      T("wf.notes.code", {
        lo: Math.round(last.code_lo || 0),
        hi: Math.round(last.code_hi || 0),
        span: SPAN_CODES,
      }),
      T("wf.notes.png"),
    ];
    const partial = incompleteRows(last);
    if (partial.length) {
      const missing = Math.max(0, (last.windows || 0) - (last.row_frames[partial[0]] || 0));
      lines.push(T("wf.notes.partial", { rows: partial.length, missing: missing }));
    }
    lines.forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      notes.appendChild(li);
    });
  }

  // ---- 出图按钮 ----------------------------------------------------------
  function bindButtons() {
    const btn = root.querySelector("#wf-render");
    const open = root.querySelector("#wf-open");
    const clear = root.querySelector("#wf-heat-clear");
    if (clear) {
      clear.addEventListener("click", () => {
        if (!window.Bridge || !Bridge.ready()) return;
        clear.disabled = true;
        Bridge.call("clearHeatMemory")
          .then((res) => {
            clear.disabled = false;
            if (!res || !res.ok) {
              setText("#wf-heat-info", T("wf.heat.fail", { error: (res && res.error) || "?" }));
              return;
            }
            if (heatData) heatData = new Uint8Array(heatData.length);
            heatHot = 0;
            setText("#wf-heat-info", T("wf.heat.cleared", { n: res.cleared }));
            if (last) render(last);
          })
          .catch((err) => {
            clear.disabled = false;
            setText("#wf-heat-info", T("wf.heat.fail", { error: String((err && err.message) || err) }));
          });
      });
    }
    if (btn) {
      btn.addEventListener("click", () => {
        if (!window.Bridge || !Bridge.ready()) return;
        btn.disabled = true;
        const prompt = T("wf.report.btnBusy");
        const original = T("wf.report.btn");
        btn.textContent = prompt;
        Bridge.call("renderWaterfall")
          .then((res) => {
            btn.disabled = false;
            btn.textContent = original;
            if (!res || !res.ok) {
              setText("#wf-files", T("wf.report.fail", { error: (res && res.error) || "?" }));
              return;
            }
            report = res;
            showReport(res);
          })
          .catch((err) => {
            btn.disabled = false;
            btn.textContent = original;
            setText("#wf-files", T("wf.report.fail", { error: String(err && err.message || err) }));
          });
      });
    }
    if (open) {
      open.addEventListener("click", () => {
        if (!report || !report.index) return;
        Bridge.call("openFile", report.index).then((res) => {
          if (!res || !res.ok) setText("#wf-files", T("wf.report.fail", { error: (res && res.error) || "?" }));
        });
      });
    }
  }

  function showReport(res) {
    if (!root) return;
    const open = root.querySelector("#wf-open");
    if (open) open.disabled = !res || !res.index;
    setText("#wf-files", T("wf.report.done", {
      pages: (res && res.pages) || 0,
      png: (res && res.png) || "—",
      html: (res && res.html) || "—",
    }));
  }

  // ---- 小工具 ------------------------------------------------------------
  function setText(sel, text) {
    if (!root) return;
    const node = root.querySelector(sel);
    if (node) node.textContent = text;
  }

  function mmss(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    return Math.floor(total / 60) + ":" + String(total % 60).padStart(2, "0");
  }

  function probe() {
    let painted = 0;
    let total = 0;
    if (fit && fit.ctx && layout) {
      const w = Math.min(fit.width, layout.segW);
      const h = Math.min(fit.height - PAD.top - PAD.bottom, layout.dataH);
      const img = fit.ctx.getImageData(layout.x0, layout.y0, Math.max(1, w), Math.max(1, h)).data;
      total = img.length / 4;
      for (let i = 0; i < img.length; i += 4) {
        if (img[i] !== BG[0] || img[i + 1] !== BG[1] || img[i + 2] !== BG[2]) painted += 1;
      }
    }
    const bandStats = [];
    if (last && last.bands) {
      last.bands.forEach((band, i) => {
        const data = cells[i] || UNITY;
        const ref = bandRange[band.id] ? bandRange[band.id].ref : 0;
        let nz = 0;
        let sum = 0;
        let bright = 0;   // 高出底噪 1/4 满亮跨度的格数 —— 这格"看得见"就是靠它
        for (let k = 0; k < data.length; k++) {
          const v = data[k];
          if (!v) continue;
          nz += 1;
          sum += v;
          if ((v - ref) / SPAN_CODES > 0.25) bright += 1;
        }
        bandStats.push({
          id: band.id,
          cells: data.length,
          nonzero: nz,
          ratio: data.length ? Math.round((nz / data.length) * 1000) / 1000 : 0,
          mean: nz ? Math.round(sum / nz) : 0,
          bright: bright,
          brightRatio: data.length ? Math.round((bright / data.length) * 1000) / 1000 : 0,
          // range 保持 [lo, hi] 形状（老探针/浸泡脚本读它）：lo = 本段底噪基准，hi = 满亮码
          range: bandRange[band.id]
            ? [Math.round(bandRange[band.id].ref), Math.round(bandRange[band.id].ref) + SPAN_CODES]
            : null,
          ref: bandMeta[band.id] ? bandMeta[band.id].ref : null,
          peak: bandMeta[band.id] ? bandMeta[band.id].max : null,
        });
      });
    }
    return {
      rows: (last && last.rows) || 0,
      sweeps: (last && last.sweeps) || 0,
      frames: (last && last.frames) || 0,
      bands: (last && last.bands && last.bands.length) || 0,
      bandStats: bandStats,
      canvas: { w: fit ? fit.width : 0, h: fit ? fit.height : 0 },
      painted: total ? Math.round((painted / total) * 1000) / 10 : 0,
      range: range ? [Math.round(range.lo), Math.round(range.hi)] : null,
      notes: (root && root.querySelectorAll("#wf-notes li").length) || 0,
      // 记忆热力探针：格子数 / 最高值 / 被烧热的格子数 / 画布尺寸 / 信息行文字
      heat: (function () {
        if (!heatData) return null;
        let peak = 0;
        let hot = 0;
        let sum = 0;
        for (let i = 0; i < heatData.length; i += 1) {
          const v = heatData[i];
          sum += v;
          if (v > peak) peak = v;
          if (v >= 128) hot += 1;
        }
        return {
          cells: heatData.length,
          peak: peak,
          hot: hot,
          sum: sum,
          canvas: { w: heatFit ? heatFit.width : 0, h: heatFit ? heatFit.height : 0 },
          info: (root && root.querySelector("#wf-heat-info")) ? root.querySelector("#wf-heat-info").textContent : "",
        };
      })(),
    };
  }

  return { onEnter, onLeave, onTables, onLang, probe };
})();