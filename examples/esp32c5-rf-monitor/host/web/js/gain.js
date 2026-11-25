/* gain.js —— 顶栏增益控件（AGC / 手动 / 自动）+ 实时档位与底噪读数。
 *
 * 为什么要在界面上做这个：宽带占用率**会随增益走**。同房间同脚本，增益 20/40/60
 * → 宽带占用 27%/61%/85%（docs/03-ble-risk.md §6.1）。所以「占用 85%」这句话，
 * 不给增益档位就是废话。三个按钮 + 加减档，看一眼就知道现在的数是在哪个档位下测的。
 *
 * 三条模式（对应 Python 侧 host/gain.py）：
 *   AGC   → 固件 `GAIN HARDWARE`（自己挑档；测「有没有信号」用它）
 *   手动   → `GAIN MANUAL n`（固定档；横向比较必须固定）
 *   自动   → 主机闭环：每帧看底噪参考码（bins 的 p10），把档位调到目标附近
 *
 * 所有命令都是「写意图」：Python 侧只在两次扫描之间落地（扫描中途插 GAIN 会打乱串口）。
 */
window.Gain = (function () {
  let state = null;         // 最近一次 Python 给的 gain 状态
  let bound = false;
  let pendingTimer = null;

  function $(sel) {
    return document.querySelector(sel);
  }

  function init() {
    if (bound) return;
    bound = true;
    document.querySelectorAll("[data-gain-mode]").forEach((btn) => {
      btn.addEventListener("click", () => send({ action: "mode", mode: btn.dataset.gainMode }));
    });
    const down = $("#gain-down");
    const up = $("#gain-up");
    if (down) down.addEventListener("click", () => send({ action: "step", delta: -1 }));
    if (up) up.addEventListener("click", () => send({ action: "step", delta: 1 }));
    const input = $("#gain");
    if (input) {
      // 手动输入：回车 / 失焦才提交，别每敲一个数字就发一条命令
      input.addEventListener("change", () => {
        const value = parseInt(input.value, 10);
        if (Number.isFinite(value)) send({ action: "index", index: value });
      });
    }
    const target = $("#gain-target");
    if (target) {
      target.addEventListener("change", () => {
        const value = parseFloat(target.value);
        if (Number.isFinite(value)) send({ action: "target", target: value });
      });
    }
  }

  async function send(payload) {
    if (!window.Bridge || !Bridge.ready()) return;
    try {
      const next = await Bridge.call("gainControl", JSON.stringify(payload));
      if (next && next.kind === "gain") apply(next);
    } catch (err) {
      if (window.AppLog) AppLog({ level: "err", text: T("gain.failed", { error: err }) });
    }
  }

  /** Python 的 stats / runtime 里带 `gain`，外壳 onStats 会转进来 */
  function apply(next) {
    if (!next || next.kind !== "gain") return;
    state = next;
    render();
  }

  function mode() {
    return (state && state.mode) || "MANUAL";
  }

  function index() {
    if (state && Number.isFinite(state.index)) return state.index;
    const input = $("#gain");
    const raw = input ? parseInt(input.value, 10) : NaN;
    return Number.isFinite(raw) ? raw : 60;
  }

  function target() {
    if (state && Number.isFinite(state.target)) return state.target;
    const input = $("#gain-target");
    const raw = input ? parseFloat(input.value) : NaN;
    return Number.isFinite(raw) ? raw : 4;
  }

  function render() {
    if (!state) return;
    document.querySelectorAll("[data-gain-mode]").forEach((btn) => {
      btn.classList.toggle("seg__btn--on", btn.dataset.gainMode === state.mode);
    });
    const input = $("#gain");
    if (input) input.value = state.index;
    const targetInput = $("#gain-target");
    if (targetInput) targetInput.value = state.target;

    const eff = state.effective === null || state.effective === undefined ? "—" : state.effective;
    const floor = state.floor_code === null || state.floor_code === undefined
      ? "—"
      : T("gain.codes", { n: Number(state.floor_code).toFixed(1) });
    const read = $("#gain-read");
    if (read) {
      read.textContent = T("gain.read", { index: state.index, eff: eff, floor: floor });
      read.title = state.note || "";
      read.dataset.mode = state.mode;
    }
    const note = $("#gain-note");
    if (note) {
      const parts = [];
      if (state.note) parts.push(state.note);
      if (state.mode === "AUTO" && state.adjusts) parts.push(T("gain.adjusts", { n: state.adjusts }));
      note.textContent = parts.join(" · ");
    }
  }

  /** 语言切换：读数里的中文量词要跟着变 → 重新渲一次 */
  function relabel() {
    if (state) render();
  }

  return { init, apply, mode, index, target, relabel, probe: () => state };
})();