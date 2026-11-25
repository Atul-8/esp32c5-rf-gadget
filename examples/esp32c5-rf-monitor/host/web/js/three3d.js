/*
 * three3d.js —— three.js 3D 视图（本地 vendor，不联网）。
 *
 * 两个组件：
 *   Three3D.Waterfall  时间 × 频率 × 功率的滚动曲面（实时频谱页）
 *
 * 边界（和 CLI 输出保持同一套口径，不许美化）：
 *   · 高度是**相对 dB 码**（0..255），不是 dBm；
 *   · 曲面横向是频率、纵深是时间，索引用环形缓冲，越靠前越新。
 */
window.Three3D = (function () {
  const hasThree = typeof THREE !== "undefined";

  function makeRenderer(container) {
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(Math.max(320, container.clientWidth), Math.max(200, container.clientHeight), false);
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    renderer.domElement.style.display = "block";
    renderer.domElement.style.borderRadius = "8px";
    container.appendChild(renderer.domElement);
    return renderer;
  }

  function makeCamera(container, fov, distance) {
    const aspect = Math.max(320, container.clientWidth) / Math.max(200, container.clientHeight);
    const camera = new THREE.PerspectiveCamera(fov, aspect, 0.05, 200);
    camera.position.set(0, distance * 0.62, distance);
    camera.lookAt(0, 0, 0);
    return camera;
  }

  /** 把任意长度 bins 重采样到 n 个桶（取桶内最大值，保住窄带信号） */
  function resample(bins, n) {
    const out = new Float32Array(n);
    if (!bins || !bins.length) return out;
    const per = bins.length / n;
    for (let i = 0; i < n; i++) {
      const lo = Math.floor(i * per);
      const hi = Math.max(lo + 1, Math.floor((i + 1) * per));
      let m = 0;
      for (let j = lo; j < hi && j < bins.length; j++) if (bins[j] > m) m = bins[j];
      out[i] = m;
    }
    return out;
  }

  class Waterfall {
    constructor(container, opts) {
      opts = opts || {};
      this.container = container;
      this.nFreq = opts.nFreq || 96;
      this.nTime = opts.nTime || 110;
      this.amp = opts.amp || 0.85;
      this.frames = 0;
      this.dirty = true;
      // 归一化到 0..1 的坡度（用自适应范围，噪声底才不会糊成一张平面）
      this.range = new U.AutoRange(14, 0.25);
      this.heights = new Float32Array(this.nFreq * this.nTime);

      if (!hasThree) {
        container.innerHTML = '<div class="muted">' + T("spectrum.noThree") + "</div>";
        this.ok = false;
        return;
      }
      this.ok = true;

      this.scene = new THREE.Scene();
      this.scene.background = new THREE.Color(0x070a10);

      this.renderer = makeRenderer(container);
      this.camera = makeCamera(container, 45, 3.4);

      const geo = new THREE.PlaneGeometry(2.8, 2.4, this.nFreq - 1, this.nTime - 1);
      geo.rotateX(-Math.PI / 2); // 摊平到 XZ 平面，Y 轴留给功率
      const pos = geo.attributes.position;
      this.vertexCount = pos.count;
      const colors = new Float32Array(this.vertexCount * 3);
      geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      this.geo = geo;

      const mat = new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide });
      this.mesh = new THREE.Mesh(geo, mat);
      this.scene.add(this.mesh);

      // 地面网格：给纵深轴一个参照（时间方向），不画刻度文字免得挡住曲面
      const grid = new THREE.GridHelper(4.2, 12, 0x24405f, 0x162536);
      grid.position.y = -0.02;
      this.scene.add(grid);

      if (THREE.OrbitControls) {
        this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
        this.controls.enableDamping = true;
        this.controls.dampingFactor = 0.08;
        this.controls.minDistance = 1.6;
        this.controls.maxDistance = 9;
        this.controls.maxPolarAngle = Math.PI * 0.49;
        this.controls.target.set(0, 0.18, 0);
      }

      this._resize = () => this.resize();
      window.addEventListener("resize", this._resize);
      this._loop = () => {
        this.raf = requestAnimationFrame(this._loop);
        if (this.dirty) this._rebuild();
        if (this.controls) this.controls.update();
        this.renderer.render(this.scene, this.camera);
      };
      this._loop();
    }

    resize() {
      if (!this.ok) return;
      const w = Math.max(320, this.container.clientWidth);
      const h = Math.max(200, this.container.clientHeight);
      this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
      this.dirty = true;
    }

    /** 推进一帧：bins 是当前窗口的功率码，会重采样到 nFreq 后压到最新一行 */
    push(bins) {
      if (!this.ok) return;
      this.range.update(bins);
      const raw = resample(bins, this.nFreq);
      const row = new Float32Array(this.nFreq);
      for (let i = 0; i < this.nFreq; i++) row[i] = this.range.norm(raw[i]);
      // 环形缓冲：整体前移一行（越靠前越新），写入最后一行
      this.heights.copyWithin(0, this.nFreq);
      this.heights.set(row, (this.nTime - 1) * this.nFreq);
      this.last = row;
      this.frames += 1;
      this.dirty = true;
    }

    _rebuild() {
      this.dirty = false;
      const pos = this.geo.attributes.position;
      const col = this.geo.attributes.color;
      const perRow = this.nFreq;
      for (let v = 0; v < this.vertexCount; v++) {
        // PlaneGeometry 顶点顺序：iy 行（纵深/时间）× ix 列（横向/频率）
        const iy = Math.floor(v / perRow);
        const ix = v % perRow;
        const t = this.heights[iy * perRow + ix] || 0;
        pos.setY(v, t * this.amp);
        const hex = U.heatHexT(t);
        col.setXYZ(v, ((hex >> 16) & 255) / 255, ((hex >> 8) & 255) / 255, (hex & 255) / 255);
      }
      pos.needsUpdate = true;
      col.needsUpdate = true;
    }

    dispose() {
      cancelAnimationFrame(this.raf);
      window.removeEventListener("resize", this._resize);
      if (this.geo) this.geo.dispose();
      if (this.renderer) this.renderer.dispose();
      if (this.container) this.container.innerHTML = "";
    }
  }

  return { Waterfall, hasThree: hasThree, resample: resample };
})();