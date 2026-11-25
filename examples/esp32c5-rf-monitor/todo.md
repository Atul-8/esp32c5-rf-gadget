# todo.md — 唯一任务事实源

规则（来自 `docs/开发任务书.md`）：

1. 同一时刻只有一个 `[DOING]`；只干 `[DOING]` 那一条。
2. 每阶段结束是硬 STOP：先把「验收材料 + 验证命令 + 实测输出」交给人类，
   人类回「确认完成」才把 `[DOING]` 标成 `[DONE]` 并把下一条改成 `[DOING]`。
3. 功能分支不许改 `core/`；接口不够用，写进本文件「阻塞与需求」，等人裁决。
4. 分支名固定为 `feat/<阶段号>-<短名>`。

## 阶段表

| # | 分支 | 内容 | 状态 |
| --- | --- | --- | --- |
| 0 | `feat/0-foundation` | `core/` 地基 + mock/假固件 + 43 条自测 | `[REVIEW]` 待人类验收 |
| 1 | `feat/1-wifi-check` | WiFi 信道体检 | `[REVIEW]` 待人类验收（mock 判据 + 真机实测均已通过） |
| 2 | `feat/2-waterfall` | 频谱瀑布图（PNG + HTML 翻页） | `[REVIEW]` 待人类验收 |
| 3 | `feat/3-ble-risk` | BLE 风险与环境评估 | `[REVIEW]` 待人类验收（六档判词 + 仿真佐证 + 增益对照） |
| 4 | `feat/4-guard-58g` | 5.8G 频段守卫 | `[REVIEW]` 待人类验收（13 窗滑扫 + 五类判词 + 告警日志 + 事件落库；真板空环境 1 条杂散 + 回放注入自证） |
| 5 | `feat/5-capture` | 可疑信号抓拍（含分类器） | `[REVIEW]` 待人类验收（四道门 + 真板 4 根杂散报清单不抓 + 3 条存证 + 抓档页/一键体检/场景矩阵 9/9 + 判据回放 7/7） |
| T | `feat/host-gui` | 图形上位机（WebView，跨阶段工具，不占功能阶段） | `[REVIEW]` 待人类验收（含双击弹出预览 + 高度自适应 + 真全屏 + 独立窗口 + 2D 瀑布频率轴 + 周期瀑布图页 + 抓档页 + 一键体检） |
| 6 | `integration` | 五合一合并 + 总验收报告 + tag `v1.0.0` | `[REVIEW]` 待人类整体验收（六步合并逐个跑单测 58→271、矩阵 10/10、判据回放 7/7、整场仿真 OK、自检 44 checks 全绿） |
| 7 | `feat/7-fsm` | 设备端扫频状态机（方案 A）+ 上位机英文界面零中文 | `[REVIEW]` 待人类验收（固件 `scan_fsm` 真板 16.89 窗/秒；`engine=host\|device` 双引擎自检 52 checks 全绿；英文界面 6 页 DOM 扫描 0 中文） |

---

## [REVIEW] 0. 地基（feat/0-foundation）

交付物：

- `core/serial_link.py` `core/snapshot.py` `core/scheduler.py` `core/store.py`
  `core/registry.py` `core/classifier.py`（接口）
- `output/format.py`、`tests/`（mock 场景 + 假固件 + 自测）、`app/demo.py` `app/live.py`
- `docs/00-foundation.md`（验收材料）、`README.md`

验收标准 → 结果：

| 验收项 | 判据 | 结果 |
| --- | --- | --- |
| 自测全绿 | `python -m unittest discover -s tests -t .` 0 失败 | ✅ 43 passed |
| mock 全链路 | `python -m app.demo` 有统计表、有入库行数、无错误 | ✅ 30 片/60 帧/入库 60 行 |
| 权重生效 | `--weights 4:3:2:1` 占比 ≈ 40/30/20/10 | ✅ 40.0/30.0/20.0/10.0 |
| 协议解析 | 假固件跑真机同一条路径，状态 0、CRC 校验通过 | ✅ `app.live --mock` 错误 0 |
| 坏帧不静默 | CRC 错/截断帧抛 `SnapshotError` 并标记链路失效 | ✅ 有对应测试 |
| 模块隔离 | 一个模块抛异常不影响其它模块 | ✅ 有对应测试 |
| `modes/` 为空 | 阶段 0 不许提前实现功能 | ✅ `discover("modes")` 返回空且有测试守着 |
| 边界声明 | 产出物标明「周期快照、非连续实时」「功率码不是 dBm」 | ✅ README + 输出页脚 |

未覆盖（必须诚实标注）：**真机联调未做**（本机未接 ESP32-C5）；
`classify()` 仍抛 `NotImplementedError`（任务 5 负责）。

## [REVIEW] 1. WiFi 信道体检（feat/1-wifi-check）

- 只加 `modes/wifi_check.py` + `tests/test_wifi_check.py`（未动 `core/`）。
- 产出：2.4G ch1–13 / 5G ch36–165 表格（按相对抬升降序）、推荐信道、建议避开前 3、干扰源列表。

### 扫描计划：20MHz 窗 / 15MHz 步进重叠滑扫（本轮定稿）

```python
SCAN_BW_HZ   = 20_000_000   # 窗宽 = 采样率（20MS/s 实测满带宽，整窗可用）
USABLE_HALF_HZ = 10_000_000 # 窗内可用半宽
SCAN_STEP_HZ = 15_000_000   # 步进 < 窗宽 ⇒ 相邻窗重叠 5MHz，频段无空洞
BAND_24_HZ   = (2_402_000_000, 2_482_000_000)   # → 5 个窗：2412/2427/2442/2457/2472
BAND_5_HZ    = (5_170_000_000, 5_835_000_000)   # → 44 个窗：5180…5825，步进正好 15MHz
```

覆盖率按「信道 20MHz 内每个 MHz 是否真的收到能量（码 > 0）」求并集，跨窗累加：
`coverage < 60% ⇒ 分档「无覆盖」，不参与推荐、不计入干扰源`（防止宽窗边缘 0 码把信道
误判成空闲）。

### 判决口径（本轮改过一次，原因写在下面）

信道中位电平比「同频段最安静信道」高 `8/15/25 dB` ⇒ 轻度 / 拥挤 / 重度。

**为什么不用「高于噪声底的 bin 比例」**：实板实测同一帧内噪声底自身抖动 ±10~18 dB
（AGC 与量化台阶：`20*log10(Q15 power)` 只有 6 dB 一格），任何绝对门限都会把纯噪声判成
忙——实测 2.4G/5G 全部信道 100% 忙、推荐变成「无干净信道」。改成同频段相对抬升后，
安静环境全档位归零，有 AP 的信道高出 20~40 dB，结果可解释。

前提：**横向比信道必须固定增益**。硬件 AGC 会把每个信道拉到相近电平（实测各信道
`p50` 全在 62~77 之间），占用差异被吃掉。因此 `app/live.py` 增加 `--gain-index N`
（实测定稿 **60**：安静段 `p50≈28`、忙段 `p50≈68`，无饱和）。

### 验收标准 → 结果

| 验收项 | 判据 | 结果 |
| --- | --- | --- |
| 自测全绿 | `python -m unittest discover -s tests -t .` | ✅ **58 passed**（原 43 + 本轮 12 + 3 调整） |
| 零误报 | `quiet` 场景：全部「空闲」、干扰源 0、推荐 ch1 | ✅ 干扰源 `无`、推荐 ch1 |
| 塞高占用能被抓 | `wifi_busy`：ch1/ch6 判「拥挤」以上，推荐落在非重叠干净信道 | ✅ ch1/ch2/ch3 进「建议避开」，推荐 **ch11** |
| 宽带污染可分辨 | `microwave`：中段抬升、ch1 干净 | ✅ ch4–ch13 抬升，推荐 ch1 |
| BLE 不误判成 WiFi 负载 | `ble_crowded`：不产生 WiFi 干扰源 | ✅ 干扰源 `无`，推荐落在 1/6/11 |
| 表格按占用排序 | 输出「比最静」列严格降序 | ✅ 有测试守着 |
| 无覆盖不冒充空闲 | 覆盖 <60% ⇒ 「无覆盖」且不推荐 | ✅ 有测试守着 |
| 真机可跑 | `python -m app.live --port COM56 --duration 60 --gain-index 60` | ✅ 17880 帧 / 60.0s / **错误 0** |

### 真机实测（2026-11-18，COM56，`--gain-index 60`，60s / 17880 帧 / 错误 0）

| 排名 | 信道 | 比最静 | 分档 |
| --- | --- | --- | --- |
| 1 | ch36 (5.18G) | +41 dB | 重度 |
| 2 | ch40 | +40 dB | 重度 |
| 3 | ch44 | +34 dB | 重度 |
| 4 | ch48 | +28 dB | 重度 |
| 5 | ch153 (5.765G) | +26 dB | 重度 |
| 6 | ch1 (2.412G) | +22 dB | 拥挤 |
| 8–11 | ch8 / ch9 / ch2 | +19 dB | 拥挤 |
| 27 | ch100 (5.5G) | +0 dB | 空闲 |

推荐：2.4G **ch11**（+3 dB），5G **ch100**（+0 dB）；建议避开 2.4G ch1/ch8/ch9、5G ch36/ch40/ch44。
覆盖列全部 100%，没有出现假「空闲」（上一轮 5G ±20MHz 之外假空闲的问题已消失）。

### 遗留

- 5G 一次全扫需要 44 个窗（≈11s/轮），单模块独占时 60s 只跑 4~5 轮，样本仍偏少；
  后续可在 `todo.md` 定权重要求，或按需 `include_5g=False` 只扫 2.4G（5 个窗 ≈1.3s/轮）。
- 「相对抬升」只在同一次固定增益扫描内可比；换增益/换天线后历史数据不可直接比较。
- 仍未做形式化人工确认（STOP 门禁），等人回「确认完成」再开阶段 2。

## [REVIEW] 2. 频谱瀑布图（feat/2-waterfall）

- 只加 `modes/waterfall.py` + 自己的渲染（`output/png.py` `output/heatmap.py`）+ `app/waterfall.py`。
- 产出：时频热力图（PNG 774×376 每页 + `index.html` 翻页 + HTML 页内导航），
  横轴频率（等宽、各自刻度）、纵轴时间（**一行 = 一轮轮扫 ≈ 4.4 s**），页时长 300 s。
- 判据：mock 跑 10 分钟出图，**能肉眼分辨 WiFi 突发块与微波炉宽带噪声** → 见下表实测。

验收材料（`docs/02-waterfall.md` 有完整版）：

```bash
python -m unittest discover -s tests -t .        # Ran 103 tests / OK（含 feat/host-gui 合并；纯本分支 82）
python -m app.waterfall --minutes 10 --scenario microwave --db .data/wf/microwave.db --out .data/wf/microwave --ascii
python -m app.waterfall --minutes 10 --scenario mixed     --db .data/wf/mixed.db     --out .data/wf/mixed     --ascii
python -m app.waterfall --render-only --db .data/wf/mixed.db --out .data/wf/mixed   # 只重渲染
```

实测输出（本机 Python 3.12.10，mock 虚拟时间）：

| 验收项 | 判据 | 结果 |
| --- | --- | --- |
| 自测全绿 | `unittest` 0 失败 | ✅ 103 passed（新增 `test_png` 10 / `test_heatmap` 15 / `test_waterfall` 20） |
| 轮扫覆盖 | 三频段首尾压边、步进 ≤ 15 MHz、相邻窗重叠 | ✅ 2.4G 6 窗 / 5G LOW 13 / 5G HIGH 25，共 **44 窗一轮** |
| 快照顺路入库 | 10 分钟 mock 入库帧数 == 生成帧数 | ✅ 5984 / 5984，`source_errors 0` |
| 分页不丢帧 | 10 分钟 → 2 页，页帧数之和 == 总数 | ✅ 3036 + 2948 = 5984 |
| PNG 合法且保真 | 尺寸/颜色类型正确，**像素与矩阵逐行一致** | ✅ 774×376 / RGB8；逐行比对测试通过 |
| 微波炉宽带 | 连续抬升 ≥ 40 MHz | ✅ 2430–2483.5 连续（ASCII `====`） |
| WiFi 突发块 | 窄（< 30 MHz）+ 时间轴有暗行 | ✅ 2432–2442，min 22 / max 137 |
| 无假信号 | `quiet` 不出现连续亮带 | ✅ `runs_above(p50+8)` 为空 |
| 边界声明 | 「周期快照，非连续实时」+「相对码不是 dBm」 | ✅ PNG 页眉 / HTML banner / 页脚 / CLI 四处 |
| **上位机真板联调**（合入 `feat/host-gui` 后） | 自检 29 项全绿 + 瀑布页真在滚动 | ✅ `--selftest --real --port COM56 --gain-index 60 --seconds 40` → 29/29、`rows 5 / sweeps 5 / painted 100%`、`share {waterfall 0.5, wifi_check 0.5}`、实测一轮 `12.4 s` |
| 单射频公平配比 | 两个模块各拿一半射频 | ✅ `slice_weight` 都改 1.0；改前实测 share `{waterfall 0.016, wifi_check 0.984}`（瀑布一轮 5 分钟） |

产物（`examples/esp32c5-rf-monitor/.data/wf/`，不入库）：

```text
microwave/waterfall-p1.png  774×376  124,975 B     mixed/waterfall-p1.png  774×376  131,932 B
microwave/index.html        2.3 KB                 mixed/index.html        2.3 KB
microwave/waterfall-p1.html 2.9 KB                 mixed/waterfall-p1.html 2.9 KB
```

未覆盖（诚实标注）：

1. **没上真机跑瀑布图** —— 本分支只做宿主侧，`app.waterfall` 目前只有 mock 源；
   真机数据同样进 `Store`，接口一致但**未实测**。
2. **虚拟时间**：10 分钟数据 40~50 s 生成，时间轴是等效时间不是墙钟（产出物已注明）。
3. **mock 逐 bin 抖动**：`SpectrumSynthesizer._is_on()` 按 bin 掷骰子，图上噪点感是 mock 特性。
4. **上位机页已落地（已合并进 `feat/host-gui`）**：`host/web/pages/waterfall/`（注册表一行 + 片段 + `js/pages/waterfall.js`），
   数据走本模块的 `web_payload()`（一行 = 一轮、一格一字节 base64、0 码 = 未扫到），
   页上还能直接「生成 PNG + HTML」。真板 40 s 自检已验。
5. **环形缓冲按「窗」存**（同窗连帧最大保持合并）：默认 1200 窗 ≈ 27 轮 ≈ 5 分钟；
   按帧存时 1200 帧连一轮都装不下（真板一次 `scan()` 回 30+ 帧），页面只画得出一行半。
6. **`row_period_s` 只用扫满的轮次估**：残轮会把周期算小（实测 1.8 s < 射频 4.4 s）；
   凑不齐两整轮返回 0，前端退回射频时间。小时级历史仍走 `app/waterfall.py` 读 SQLite。

## [REVIEW] 3. BLE 风险与环境评估（feat/3-ble-risk）

- 只加 `modes/ble_risk.py` + `tests/test_ble_risk.py` + `docs/03-ble-risk.md`（未动 `core/`、未动其它模块）。
- 产出：BLE 广播信道（2402/2426/2480）活动度、拥挤度评分、与 WiFi 的共存提示。
- 判据：`ble_crowded` 场景评分必须高于 `quiet`；`wifi_suppress_ble` 场景必须给出「被压制」判读。

**已完成（数字都是实跑）**：

- [x] 扫描计划 8 窗（3 广播信道中心对准 + 数据区 5 窗重叠滑扫），`data_slots()` 37 个数据信道栅格。
- [x] 判据三原则：电平取 **p80**（脉冲不铺满 2 MHz）、窄带占比 ≥ 50%（防 WiFi 斜坡摊肩误判）、
      肩部取左右 max（防不对称斜坡误判）。
- [x] 宽带占用率改在**时间均值频谱**上算（单帧噪声抖动会让 37% 纯噪声 bin 越线）。
- [x] WiFi 共存用双半宽（测电平 ±9 MHz / 判重叠 ±10 MHz），「在用」判据改成占用比例 ≥50%。
- [x] mock 8 场景：quiet 4.9 → crowded **82.7**（密度型）→ suppress **23.8**（压制型）→ mixed 42.8 →
      microwave 31.7 → wifi_busy 21.2 → unknown_cw 19.3 → video_58g 4.8。
- [x] 实板 COM56 固定增益 60：45 s / 372 时间片 / 13262 帧 / 源错误 0，
      判「WiFi 压制型（48.5）」，宽带占用 85%（房间本身 2.4G 很忙，见 `docs/03-ble-risk.md` §6）。
- [x] `python -m unittest discover -s tests -t .` → **76 条 OK**（43 地基 + 33 本功能）。
- [x] 顺手修地基里两条「`modes/` 必须为空」的断言（与功能分支共存冲突，功能 1 分支同款修法）。

**本轮（人类要求「用仿真佐证输出逻辑完备」）**：

- [x] 判词从 4 档扩到 **6 档 + 2 过渡档**：`无明显风险` / `轻微干扰` / `复杂噪音（多源混合）` /
      `蓝牙设备密度型` / `WiFi 压制型` / `宽带常开/形状疑似`（+ `混合型`、`无数据`）。
      核心是**先判「分不分得清」再判「是哪一类」**，分不清就明说分不清，不许冒认 WiFi。
- [x] 新增 4 条判据：`rise_floor`（本机噪声自己的抬升零点，实测标定；样本池含数据区 37 个栅格）、
      `contrast_db`（抬升频点在时间上的开关幅度，p80；≥8 码才算信号）、
      `shallow_ratio`（浅抬升占比，自成一档，需 ≥60 帧样本）、`slots_persistent`（常亮栅格 ⇒ CW 指纹）。
- [x] **仿真佐证脚本** `tests/sim_ble_verdict.py`：用合成功率造 6 档输入，
      五档现实输入 → **5 个互不相同的判词**（完全安静 / 浅干扰 / 复杂噪音 / 静态形状 / 开关型宽带），
      病态输入（整段一起动）落到兜底「宽带常开/形状疑似」。跑法：
      `python tests/sim_ble_verdict.py --scenarios`。
- [x] 断言固化为 `DiscriminationTest`（7 条）→ `tests/test_ble_risk.py` 41 条，
      全量 `discover` **145 条 OK**。
- [x] 实板增益对照实验（同房间同脚本，只改增益）：
      增益 20/40/60 → 宽带占用 **27% / 61% / 85%**，判词「复杂噪音 29.0」→「WiFi 压制型 40.7」→「WiFi 压制型 47.3」。
      ⇒ 高增益下的宽带占用主要是**前端动态范围**，不能当环境真值；新增 `headroom_warning` 自动提示。
- [x] 前端形状取证（24 帧 / 每桶 32 bin）：2442 MHz 窗是平滑「碗形」（两端高 14~27 码、中间 0 码），
      时间抖动恒定 39–45 码、不随电平变化 ⇒ 是前端固有起伏，不是「13 个 WiFi 信道全在用」。

**未验证 / 待人类确认**：

- 没在**安静房间**复核实板（房间是活的，清不了场）；安静侧靠 mock `quiet`（4.9 / 0% / 0%）+
  仿真六档守。
- 本分支 `app/live.py` 还没有 `--gain-index`（属功能 1 分支），实板验证用了不提交的临时脚本。
- **没有自动增益标定**：宽带占用随增益走这件事目前只出告警，不自动降增益复测（需人类定操作口径）。

## [REVIEW] 4. 5.8G 频段守卫（feat/4-guard-58g）

- 产出：5700–5895 MHz 巡检、持续发射源告警、事件入库（`store.add_event`）。
- 判据：`video_58g` 场景必须触发报警；`quiet` 场景必须不报警（误报率受控）。
- 文件：`modes/guard_58g.py`、`tests/test_guard_58g.py`、`testing/guard_replay.py`、
  `docs/04-guard-58g.md`、`app/__init__.py`（`bind_store`）、`app/demo.py`/`app/live.py`（各 3 行注入）。
  未动 `core/`、未动其它模块。

**频段口径修正（先说清楚）**：任务书写 5700–5900，todo 原写 5150–5850，**实际实现 5700–5895**：

- 5895 是 C5 调谐上限（`docs/rx-controls.md`），5900 那段物理上测不到，写进去就是假数据；
- 5150–5700 是 5G WiFi 低段，`wifi_check` 已经在管，守卫重复扫只是抢时间片。

**已完成（数字都是实跑）**：

- [x] 扫描计划 13 窗（20 MHz 窗 / 均分 14.58 MHz 步进 / 首尾压边），每窗 40 帧环形缓冲。
- [x] 判据五条：时间均值抬升 ≥10 码、单帧占空比 ≥20%（≥75% 记「持续」）、连续 ≥8 帧、
      段宽 100 kHz–16 MHz、交替跳频判互斥（相关系数 ≤ -0.4）。
- [x] 五类判词：`持续窄带` / `持续宽带` / `规律跳频` / `持续单频点` / `间歇单频点`
      （单频点 = ≤0.5 MHz，实板环境里是本振/时钟杂散，与图传分开报）。
- [x] 告警日志：`G58-NNN` / 在发-已停 / 类型 / 频点 / 带宽 / 峰值抬升 / 占空比 / 持续时长 / 首次出现。
- [x] 存活判定按**最新帧时间**（最近 60 s 无新帧 → 已停），不按墙钟也不按分析次数。
- [x] 跨窗聚类（中心 6 MHz 内合并）：重叠窗重复看到的同一个源只留 1 条（13 条 → 1 条）。
- [x] 事件落库：`app/__init__.py:bind_store()` 注入 store，首次出现写 1 条 `events`，续时长不重复写；
      真板 45 s 跑出 `事件：guard_58g 1 条`，`events` 表 payload 带 id/kind/center_hz/bw_mhz。
- [x] **实板 COM56 增益 60 / 45 s**：245 时间片 / 本模块 3682 帧 / 源错误 0；
      空环境仅 1 条 `持续单频点 5776.1 MHz / 0.2 MHz / 88%`（本振杂散）。
- [x] **误报根因两轮排查**（`docs/04-guard-58g.md` §5）：
      ① 第一版 13 条假「持续窄带」，频点逐个等于窗中心 ⇒ 窗中心 ±250 kHz 是本机直流/LO 泄漏，挖掉；
      ② 剩两根 0.1 MHz 凸起，两侧一格就掉回底噪 ⇒ 新增「单频点」类别而不是抬门限。
- [x] **回放注入自证**（`testing/guard_replay.py`，真板数据 + 合成信号）：
      5795/4 MHz/+25 → 抓到 4.0 MHz / 96% / 持续 9.4 s；5720/8 MHz/+20 → 8.0 MHz；
      5830/2 MHz/+12 → 抓到但宽度低估成 0.9 MHz；5880/4 MHz/+8 → **按设计漏报**（低于 10 码门槛）。
- [x] `python -m unittest tests.test_guard_58g -v` → **41 条 OK**；
      `python -m unittest discover -s tests -t .` → **186 条 OK**。
- [x] `python -m app.demo --duration 14` mock 全链路：3 条告警 + 3 条事件落库 + 判据原文打印。

**边界（产出物与文档里都写了）**：

- 周期快照非连续实时：一轮 13 窗 ≈ 1.3 s（真板约 3~4 s），「持续」是按采样到的帧算的。
- 功率码是相对 dB 码，不是 dBm；增益会改占用率读数，跨会话比较先固定档位。
- 5895–5925 MHz 测不到（芯片上限）；跨窗跳频不追（只判同窗内交替）；不做设备归因。
- 弱信号下宽度会低估（+12 码时 2 MHz 报成 0.9 MHz）。
- 窗中心 ±250 kHz 与窗沿 ±0.5 MHz 不判（直流泄漏 / 滚降），代价是正落中心的源抓不到。

**未验证 / 待人类确认**：

- **没有真实 5.8G 图传**做正样本：抓得到靠回放注入自证，实物信号等有条件再补一条实测。
- 空环境只有 1 条杂散作误报基线，没在**多房间/多时段**复测误报率。
- 5.8G 频段守卫暂无专属 GUI 页面：告警表已能在上位机「原始输出」页（`pages/raw/`）看到。

## [REVIEW] 5. 可疑信号抓拍（feat/5-capture）

任务书口径：按启发式分类（窄带连续 / 宽带突发 / 跳频样）触发 `CAP20` 抓档，
落盘原始 I/Q + 元数据 + 分类结论。任务书授权本阶段动 `core/` 的**只有** `core/classifier.py`。

交付物：

- `core/classifier.py`：纯函数启发式分类（**无外部依赖**）。`SignalFeatures`（中心/宽度/占空/时长/峰值）
  → `Classification(label, confidence, reason, notes)`。判定顺序 = **先判「分不分得清」再判「是哪一类」**：
  `ambiguous → analog_video → wifi → ble → CW → unknown`。门槛全部集中在文件顶部常量表
  （`AMBIGUOUS_MIN_BW_HZ=15e6` / `VIDEO_BW_RANGE_HZ=(4e6,8e6)` / `WIFI_MIN_BW_HZ=8e6` /
  `BLE_BW_RANGE_HZ=(1e6,2.5e6)` / `CW_MAX_BW_HZ=500_000` 等）。
- `modes/capture.py`：6 个 20MHz 重叠窗（`2410.0 / 2422.7 / … / 2473.5 MHz`，15MHz 步进）常驻缓冲，
  均值谱平滑后按「相对抬升 + 占空 + 连续段宽度」判候选，`unknown` 才排队抓档。
  四道过滤器：抽帧 `FRAME_MIN_GAP_S=0.2` / 最小跨度 `MIN_SPAN_S=3.0` / 环境饱和 `BUSY_FILL=0.35`
  / 本机杂散 `SPUR_MAX_HZ=500 kHz & STEADY_FILL=0.75`。sidecar 带前后各 30s 上下文。
- `app/capture_link.py`：`CaptureAwareSource`（抓档命令只在**两次扫描之间**落地）+ `FakeCapture`
  （假固件用，三种「长相」`cw/wideband/burst` 按场景选，存档 IQ 本身带场景特征）。
- `app/audit.py`：一分钟体检报告（`python -m app.audit --port COM56`）+ **只读当前会话**的
  `summary()`（上位机「一键体检」用，不重扫、不新抓，`done` 永远 `False`）。
- `testing/capture_real.py`：真板抓档实测脚本；`testing/scenario_matrix.py`：场景 × 4 模块矩阵（阶段 5 时 9 场景，阶段 6 加入 `hotel_scan` 后 10 场景）；
  `testing/capture_replay.py`：合成快照过四道门（判据自证，不依赖实物）。
- `docs/05-capture.md`：判据推导、四道门、真板实测、边界与未验证项。

验收标准 → 结果：

| 验收项 | 判据 | 结果 |
| --- | --- | --- |
| 单测 | `python -m unittest discover -s . -p "test_*.py"` 0 失败 | ✅ **271 条 OK**（`test_capture` 43 / `test_capture_link` 14 / `test_classifier` / `test_audit` 28 合计） |
| `unknown_cw_clean` 必抓 | 分类「持续凸起」+ 落盘 1 条 | ✅ `caps 1`、`trig 77`、sidecar `unknown 0.65` |
| `quiet` 不抓 | 0 条 + 0 触发 | ✅ `caps 0 / trig 0` |
| 环境饱和不抓 | `wifi_busy` 抓 0 条 | ✅ `skipped_busy 81`、`caps 0` |
| 本机杂散报清单不抓 | `mixed` 出现 `spurs[]` 条目且 `skipped_spur > 0` | ✅ `skipped_spur 41`，`spurs[]` 逐根列 2455/2465/2427/2468 MHz |
| 场景矩阵 | `python -m testing.scenario_matrix` 场景全 PASS | ✅ 9/9 PASS（当时 9 场景；阶段 6 加 `hotel_scan` 后 **10/10**） |
| 判据自证 | `python -m testing.capture_replay` 7 用例全符合预期 | ✅ 7/7 PASS（clean_cw→抓；pure_spur→杂散；short_span→跨度门；crowded→饱和门；weak_narrow / edge_clipped→不抓；wideband_known→判 microwave 故不抓） |
| 真板 CAP20 | 样本数 / 线上字节 / 存档字节三方自洽 + sha256 一致 | ✅ 16380 样本 = 979 µs @ 20 MS/s；线上 **40950 B**；存档 **65520 B**；`sha256` 一致；分类 `unknown 0.65` |
| 上位机抓档页 | 表格 ≥1 行 / 过滤档 ≥7 / 门槛 ≥10 / 说明 ≥3 / 边界条 >30 字 / 探针 kind | ✅ `rows 1 / filters 7 / thresholds 16 / notes 13 / warn 175 / probe.kind capture / span > 0` |
| 上位机一键体检 | 点按钮出四档结论 + 5 列来源表 + 4 条边界 | ✅ 探针 `audit "发现可疑信号并已抓档" / auditRows 6 / auditBoundary 4` |
| 单射频配比公平 | 5 模块各 `slice_weight=1.0`，`share` 各 ≈0.2 | ✅ `waterfall_share` 门槛 0.3→0.15 且要求 `share["capture"] ≥ 0.15` |

真板实测（2026-11-19，COM56，`--gain-index 60`，4 轮 60s 合计 15074 快照 / 266 片 / 错误 0）：

- 4 根本机杂散（`real2.db`）：2455.0 / 2465.0 / 2427.0 / 2468.0 MHz，宽 0.156–0.27 MHz，fill 0.89–1.00，
  peak 30–35 码，持续 37–52 s —— **全部落进 `spurs[]` 清单，只报不抓**。
- spur 过滤后仍抓 3 条（`real3.db`）：2468.0 / 2410.0 / 2462.0 MHz，宽 **1.17 / 2.81 / 1.87 MHz**，
  fill 0.88–0.92 → 都 **> `SPUR_MAX_HZ`(500 kHz)**，按设计不该被杂散门拦，属「比板载杂散宽的真可疑源」，
  已存 IQ 待人工复核（`py -m testing.capture_real` 可回读）。
- 体检报告 `.data/audit/audit-20261119-003957.json`：`headline 发现可疑信号并已抓档`、
  `why` 逐个列频点并附「这不是确认是摄像头」、`sources` 17 条、`captures` 3 条、`boundaries` 4 条。

**边界（产出物与文档里都写了）**：

- 周期快照非连续实时：一轮 6 窗 ≈ 1 s（真板 3~4 s），「持续」是按采样到的帧算的。
- 功率码是相对 dB 码，**不是 dBm**；不做任何 dBm 换算。
- 单射频分时：抓档要独占射频 ~1 ms，只能安排在两次扫描之间，`max_per_slice=1`。
- 分类器是**启发式**，不是识别器：只给「分得清/分不清 + 大概像什么」，BLE 只评概率不做设备级归因。
- 抓档只收 `unknown`：16 MHz 平顶会被先判成 `microwave`（已知大类）→ **不抓**（`capture_replay` 有专门用例钉住）。
- 窄带窃听器（CW）与板载杂散**物理上不可区分**：宽度 ≤500 kHz 一律当杂散，只报清单；
  真可疑源只能靠「搬机靠近看会不会跟着变强」人工复核。
- 抓档频率取整 MHz（固件 `FREQ` 只收整 MHz），偏 0.5 MHz 由 sidecar 里的 `candidate.center_hz` 兜底。
- 「已存证」≠「确认是摄像头」：这句边界永远显示在抓档页顶部，不允许折进说明里。

**未验证 / 待人类确认**：

- **没有真实偷拍设备**做正样本：抓得到靠 mock `unknown_cw_clean` + 分类器判据 + 合成回放自证。
- 一次抓档的 IO 开销（65520 B 落盘 + 串口 40950 B 传输）对**扫速**的影响只测过单次，没做长稳跑。
- 抓档页只验过假固件自检 + 真板 `app.live`，**没在真板 + 上位机同时长跑**（自动增益 + 抓档 + 5 模块共存）验过。
- `tests/fake_firmware.py` 的 `CAP` 系列仍是桩：自检跑通的是「应用层链路」，不是假固件真出 IQ。
- 本阶段动了 `core/classifier.py`（任务书授权范围内），**仍需人类二次确认**。

## [REVIEW] T. 图形上位机 / WebView（feat/host-gui）

人类需求原文：「做一个测试上位机，参考 `MagicRobot_V0.1/host` 用 webview 3D 引擎，
以后换别的实验只需要换网页」。

上位机**可以脱离仓库独立运行**：`host/` 工作副本可放在仓库外任意位置，
靠 `host/paths.py` 定位分析层（`ESPSDR_SRC` 环境变量 > `host/src` 自包含副本 > 向上 4 层找
`repo/examples/esp32c5-rf-monitor`）。`ESPSDR_SRC` 一旦设了就是权威值，指错立刻抛错不静默回退。

交付物：

- `host/app.py`：`QWebEngineView` + `QWebChannel` 桥（所有 Python→JS 信号都是 JSON 字符串）
  + `QThread` 采集线程（频谱 20fps / 表格 1s / 单片 0.35s）
- `host/session.py`：纯 Python 逻辑层（无 Qt）：`HostConfig` 校验、`HostSession`
  `open/step/spectrum/tables/render_text`、`downsample_bins`（按桶取最大值，保窄带峰）
- `host/web/`：`index.html` + 路由 + 四个页面（实时频谱 / 周期瀑布图 / WiFi 体检 / 原始输出）+
  本地 three.js r128（离线可用）
- 周期瀑布图页（`web/pages/waterfall/` + `js/pages/waterfall.js`）：三频段并排的时频热力图，
  一行 = 一轮完整轮扫、一格一字节、0 码画最暗色；鼠标读频率/功率码；按钮走
  `Bridge.renderWaterfall()` → `produce(force=True)` 在 `.data/waterfall/` 出 PNG + HTML 翻页，
  `Bridge.openFile()` 只在 `.data/` 内允许打开（越界直接拒）。
  `--selftest` 新增 `waterfall_rows/painted/cards/banner/notes/sweep/render/share` 等。
- **增益调控（本轮新增，`host/gain.py`）**：顶栏 `AGC | 手动 | 自动` 三段按钮 +
  `− 档号 +` 步进 + `目标` 输入框 + `档 N · 实测 M · 底噪 X 码` 读数 + 自动调整次数。
  `GainControl`（GUI 线程只写意图）+ `GainAwareSource`（`scan()` 开头落地命令、轮末结算）。
  档位范围问固件 `GAIN?`（实板 0..81），不写死。
- **BLE 风险评估页（本轮新增，`web/pages/ble/` + `js/pages/ble.js`）**：告警条 + 5 张判定卡 +
  6 行证据链 + 9 列广播信道表 + 3D 信道柱 + 2.4G 在用信道 + `hints`/`notes` 原文 + payload 存底。
  门槛全部来自 `payload.thresholds`，前端零硬编码判据。
- `host/web/js/i18n.js` 补 `gain.*`（2 语言 × 10 键）与整页 `ble.*`（约 40 键 × 2 语言）。
- `host/paths.py`：定位分析层（`ESPSDR_SRC` 权威值 > `host/src` > 向上 4 层）
- `host/web/js/i18n.js` + `--lang zh|en`：外壳文案中英切换（模块自产表格沿用模块语言）
- `host/web/js/scanbar.js`：扫频进度条（频段地图 + 回卷游标，数据来自 `info.plans`）
- `host/web/js/popout.js`：**双击任意面板弹出预览**（铺满窗口 / 75%…300% 自定义缩放 /
  真全屏 / **独立窗口**（`window.open` → `ConsolePage.createWindow`，可拖到别的显示器、
  小窗口自己全屏）/ Esc 关闭，原地搬 DOM 不重建 canvas，缩放按新尺寸重画）
- **弹出后高度自适应**：`--fit-grow` 两阶段量高度（余量 ÷ 标了 `data-fit-grow="1"`
  的画布个数），`U.fitCanvas()` 高度 = `cssHeight × fitScale + fitGrow()`，
  3D 容器 = `calc(300px * var(--fit-scale,1) + var(--fit-grow,0px))`
- `host/web/js/pages/spectrum.js`：2D 瀑布底部**简易频率轴**（轴线 + 5 刻度 + MHz + 中心标亮）
- `host/tests/test_session.py`：23 条无窗口自测
- `host/README.md`：跑法、数据契约、**新增实验页的四步**、边界、踩坑表

验收标准 → 结果：

| 验收项 | 判据 | 结果 |
| --- | --- | --- |
| 逻辑层自测 | `cd host && python -m unittest discover -s tests -t tests` 0 失败 | ✅ 23 passed |
| 增益逻辑自测 | `cd host && python tests/test_gain.py` 0 失败 | ✅ **32 passed**（含闭环热身/死区/确认/边界/退化帧/属性透传） |
| 无人工自检（假固件） | `python app.py --selftest --seconds 8 --shot out.png` 退出码 0 | ✅ 退出码 0，**40/40 checks**（连跑 2 次稳定） |
| 频谱页真的在画 | 收到帧 > 0 且 2D/3D 画布非黑比例 > 1% | ✅ 188 帧；spec 24.7% / 3D 10.1% / 2D 瀑布 11.6% |
| **2D 瀑布真的在滚** | 缓冲行数 240；`paintedRows ≥ 10`、`distinctRows ≥ 0.9×frames`、顶部均值 > 底部 + 100 | ✅ `paintedRows 35 / frames 35 / distinctRows 32 / topMean 291 / bottomMean 33` |
| **扫频进度条：分段** | 从 `info.plans` 自动切段，至少 2 段、总窗数 > 10 | ✅ `2.4G`（2402–2482，5 窗）+ `5G`（5170–5835，44 窗）= 49 窗 |
| **扫频进度条：游标** | 游标在轨道内、已走过 ≥ 3 个窗 | ✅ `index 44 / visited 18 / x 1043 ≤ 1208`；像素实测琥珀色游标列 `1172 → 1673 → 343（回卷）→ 938` |
| **扫频进度条：回卷** | 喂到最后一窗再喂第一窗：轮次 +1、本轮覆盖清零 | ✅ `passesAfterWrap 2 / visitedAfterWrap 1 / indexAtEnd 3` |
| **中英切换** | 切 `en` 后导航、状态栏、扫频条文案都变；`level` 数据值仍走显示层翻译 | ✅ `navLabels` 含 `Live spectrum`；`shell = ["Scan progress (band map)", "now 5180 MHz · window 6/49 · pass 2", "Heavy"]` |
| **2D 瀑布有频率轴** | 5 个刻度、升序、`lo < 中线 < hi` | ✅ 探针 `wf2dAxis {lo 5710, hi 5730, center 5720, ticks [5710,5715,5720,5725,5730]}`；像素带实测 H-30 处整行 2114 px 轴线 + 5 处标签笔画 |
| **双击弹出预览（开）** | 浮层出现、面板搬进浮层、画布宽度 > 0 | ✅ `open {overlay true, inOverlay true, width 1391}` |
| **双击弹出预览（缩放）** | 150% 后画布宽度 > 1.35× 原宽 | ✅ `zoom {1.5, width 2099} = 1.51×`，`--fit-scale 1.5` |
| **双击弹出预览（关）** | 关闭后浮层不可见、面板回到页面原位 | ✅ `close {overlay false, hidden true, backHome true}` |
| **换页也能弹** | WiFi 页不写代码也能弹（通用逻辑 + ⤢ 按钮） | ✅ `popoutAny {ok true, themed true, back true}` |
| **弹出后高度自适应** | 面板高度 ≥ 浮层高度 90%，画布比 100% 时高 1.3 倍以上 | ✅ `stageH 865 / panelH 825`（95%），画布 `210 → 776`，`--fit-grow 461px` |
| **全屏是真的全屏** | `win.isFullScreen()` 为真，且全屏下面板仍吃满高度 | ✅ `full true`；`stageH 1032 / panelH 993`（96%），`--fit-grow 629`，按钮变「退出全屏」 |
| **窗口外弹窗** | 新开一个独立窗口，里面只有那块面板、画布按窗口尺寸渲染 | ✅ `popout_window {solo true, panel true, key spectrum:1, windows 1}`；`spec-canvas 1463×858`；截图存活 |
| **独立窗口自己全屏** | 在小窗口里点全屏 → 小窗口全屏、主窗口不动 | ✅ 探针：`child_full true / main_full false`，`innerW 1707 × innerH 1067`，面板 1008 高，退出后 `false` |
| WiFi 页真的在判 | 表格行 > 5、推荐信道有值、说明 ≥ 3 条 | ✅ 38 行；推荐 ch11 +17dB 拥挤；4 条说明 |
| 原始页真的在输出 | 至少 1 个模块面板 + ASCII 文本 > 40 字符 | ✅ 1 面板 / 1 表 / 4364 字符 |
| 换实验不改 Python | 新页面只加 `web/pages/<id>/page.html` + `registry.json` 一行 | ✅ 三页共用同一个 `app.py`，`router.js` 不认具体页名 |
| 独立目录 | 上位机放项目根 `host/`，不依赖 `repo/` 的相对位置 | ✅ `paths.find_source()` 解析到 `…
epo\examples\esp32c5-rf-monitor`；`ESPSDR_SRC=/tmp` 正确抛错 |
| 真板可跑 | `--selftest --real --port COM56` 退出码 0 | ✅ 40 s → **29/29 checks 全绿**（598 频谱帧）；瀑布页 `rows 5 / sweeps 5 / painted 100%`、`share {waterfall 0.5, wifi_check 0.5}`、实测一轮 `12.4 s`；弹出 `open 1391 → 2099 → 关闭复位`；全屏 `1032/993`；独立窗口 `spec-canvas 1463×858` |
| 边界一致 | 界面上「功率码不是 dBm」「周期快照」「固定增益才可比」 | ✅ 纵轴/提示/说明页脚都写了 |
| **单射频配比公平** | 两个模块各拿一半射频（`share` 各 ≈0.5） | ✅ 两边 `slice_weight=1.0`；改前实测 `{waterfall 0.016, wifi_check 0.984}`（瀑布一轮被拉成 5 分钟） |
| **进度条只跟一个计划** | 多模块同跑时不因换计划把 `visited` 清零 | ✅ `planModule` 固定；真板 40 s 探针 `visited 15 / passes 1` |
| **重叠窗不误判换轮** | 「索引变小」加幅度门槛（跳回大半张图才算换轮） | ✅ `scanbar.js` 用 `index - found > plan.length/2` |
| **进度条跟最宽计划** | 多计划并存时 5G 整段不能消失 | ✅ 假固件 `scanbar {groups 3, windows 44, visited 13}`；修前跟 `ble_risk` 只剩 2.4G 8 窗、`visited 5` |
| **BLE 页真的在判** | 表格 ≥3 行、判词非空、证据链 ≥6 行、hints ≥1、notes ≥3、3D 有柱 | ✅ `rows 3 / risk WiFi 压制型 / evidence 6 / hints 3 / notes 8 / bars3d true` |
| **增益：手动生效（真板）** | 档位回读一致 + 底噪随档位变 | ✅ `GAIN?` 回 `GAIN MANUAL 60 0 81 1`；0→81 逐档实测底噪 `0.0 … 63.0` 码（跨度 63），峰值 `46 → 133` |
| **增益：自动闭环（真板）** | 起始 60 → 向目标 8 码收敛 | ✅ 14 轮 `60→58→56→54→52→50→48`，平滑底噪 `39.0→14.4`，每 2 轮一档 |
| **增益：界面三项自检** | 手动/`+`/自动 三个动作都改到后端状态 | ✅ `gain_manual`/`gain_step`(+1)/`gain_auto`(`AUTO`)/`gain_on_button`(`["AUTO"]`)/`gain_readback`(含「底噪」) |

未覆盖（必须诚实标注）：

- **只在 Windows + Qt 6.12 上验过**；Linux/macOS 没测。
- 「独立窗口」是**同一进程**里的第二个 `QWebEngineView`（共用同一个 `Bridge`），
  不是新进程；主窗口退出时一起关闭，不能单独留一个小窗口活着。
- 独立窗口的 `?solo=<页面:序号>` 里的序号是**面板在页面里的出现顺序**，页面动态
  插拔面板后序号会变——只影响「已开着的旧窗口」重载，重新打开就好。
- 全屏靠 Python 侧 `showFullScreen()`（合成点击没有用户手势，DOM Fullscreen 会被拒）；
  只有纯浏览器打开网页时才会退化成 `popout--max`（铺满窗口）。
- 3D 视图**没有自动对比度/色标校准**，只是同一色带映射；不做定量判读。
- 页面刷新率受采集线程节流（20fps），**不是**固件原始帧率。
- **自动增益闭环是主机侧**（改档位 → 等一轮 → 再量），不是固件内环；一轮 1~12 s，
  环境突变时它要几轮才跟上。固件侧自动标定**没做**（需人类裁决）。
- 自动闭环的目标是「底噪参考码」，**不是**「信号最强」：弱信号环境里它会让底噪贴目标，
  强信号可能因此变糊——要测弱信号请用 `HARDWARE` 或手动钉高档。
- BLE 页的「硬冲突」判据是 `overlap.fraction >= 0.9` 的**几何重叠**，不是实测同频打架。
- 扫频进度条的**段名**（2.4G / 5G）是前端按频率猜的纯装饰标签，判据全部来自
  `info.plans`；游标位置只反映「正在读哪个窗」，不表示覆盖质量。
- 弹出预览的**「全屏」按钮**走浏览器 Fullscreen API，QWebEngine 默认不 accept，
  失败时退回「铺满窗口」（同一层浮层），行为一致但不会真的占满显示器。
- 缩放上限 300%：再大只是滚动，不会提升信息量（画布是按容器尺寸重渲染的，不是放大位图）。
- 真机实测扫速 ≈ **4 窗/秒**（瓶颈是串口解析 1024 点，不是 100ms 驻留），一轮 49 窗约 12s；
  假固件补节奏后 ≈ 8–10 窗/秒。

遗留（可选优化）：

- `wifi` 页每次表格刷新都重建 38 行 DOM（1 Hz，实测无卡顿）；若以后行数上千要改 diff 渲染。
- 3D 柱状图每次刷新重建 mesh（≤38 个），切页时未复用；暂时够用。

## [REVIEW] 6. 五合一合并（integration）

**开工依据**：人类 2026-11-20 裁决「没有的就做模拟仿真，没问题就合并」→ 授权做阶段 6。

### 缺正样本的地方怎么补

任务 4/5 的验收标准都含「真来了要抓到」，但手上没有真的 5.8G 图传、也没有真偷拍摄像头。
按人类裁决**用仿真补**，新增 `testing/anti_peep_sim.py`（三阶段）：

| 阶段 | 数据 | 模块 | 实测结果 |
| --- | --- | --- | --- |
| 1 | `video_58g` 场景（5.8G 模拟图传：宽平顶 + 载波） | 只跑 `guard_58g`（独占射频 260 片） | `analyses 131` → **告警 1 条**（5741 MHz / 2.7 MHz / 107 s） |
| 2 | `unknown_cw_clean` 场景（2.4G 干净处一根窄带未知源） | 只跑 `capture`（独占射频 240 片） | `triggers 275` → **抓档 2 条**（2454 MHz） |
| 3 | 两阶段产出合流 | `app.audit.verdict()` | 「发现可疑信号并已抓档」+「这不是确认是摄像头」 |

断言 6 项全 `true`：`guard_alerts` / `guard_analyzed` / `capture_taken` / `capture_triggered` /
`verdict_not_empty` / `verdict_not_overclaim`。新增 mock 场景 `hotel_scan`（整场：5.8G 图传 + 2.4G 隐藏源 + 普通 WiFi/BLE 背景），
矩阵里只验「在算 + 不误炸」，「能发现」交给上面的独占跑法 —— 共享单射频时两个模块都攒不够帧，
这是设计代价不是 bug（同样处理过 `video_58g`）。

**边界（写死在脚本和报告里）**：仿真是合成信号，不是实物；证明的是「判据在这类形状上会响」，
不能证明「真摄像头波形一定长这样」。有实物后应补一条真信号实测，脚本继续当回归。

### 合并记录（按任务书 1→2→3→4→5 顺序，每合一次跑一次全量）

| 步 | 合入 | 冲突 | 合完单测 |
| --- | --- | --- | --- |
| 1 | `feat/1-wifi-check`（连带阶段 0 地基，因为阶段 0 未单独进 main） | 无 | **58 OK** |
| 2 | `feat/2-waterfall` | `todo.md` 阶段表、`tests/test_registry.py` 注释 | **97 OK** |
| 3 | `feat/3-ble-risk` | 同上两处 | **130 OK** |
| 4 | `feat/4-guard-58g` | `todo.md` 阶段表 + 阻塞条目 | **186 OK** |
| 5 | `feat/5-capture` | 无 | **271 OK** |
| 6 | `feat/host-gui`（跨阶段工具 T） | 无（内容已等价） | **271 OK** |

冲突处理**只动冲突行、不改逻辑**：阶段表按两侧实际进度合并、注释取信息更全的一句、
阻塞条目取验收摘要更全的一侧。合完后 `git diff feat/host-gui -- examples/esp32c5-rf-monitor`
**为空** —— integration 的代码内容 == 已验收过的 `feat/host-gui`，没有引入任何新逻辑。

### 合流验证（全部在 integration 分支上实跑）

| 项 | 命令 | 实测结果 |
| --- | --- | --- |
| 全量单测 | `python -m unittest discover -s . -p "test_*.py"` | **271 tests OK**（53.6 s） |
| 场景矩阵 | `python -m testing.scenario_matrix --slices 240` | **10/10 PASS** |
| 判据回放 | `python -m testing.capture_replay` | **7/7 PASS** |
| 整场仿真 | `python -m testing.anti_peep_sim` | **三阶段 OK**（告警 1 + 抓档 2） |
| 上位机自检 | `cd host && python app.py --selftest --seconds 10` | **`ok:true` / 44 checks 全绿 / `fail:[]`** |
| 时间片配比 | 同上 `share` | waterfall 0.201 / wifi 0.198 / ble 0.201 / guard 0.201 / capture 0.201（公平 5×20%） |
| 窗口缩放 | 同上 `layout` | 4 档（1280×800 / 1024×680 / 900×620 / 760×560）全 `ok:true`、`bad:[]` |

产物：`docs/06-integration.md`（总验收报告）、tag **`v1.0.0`**（annotated）。

### 合流后的边界（不变，必须随结论一起说）

1. 周期快照 ≠ 连续实时：同一时刻只有一个频段在被扫，抓档期间射频停扫；
2. 功率码是**相对 dB 码**，不是 dBm，跨增益不可比；
3. 单射频分时共享：5 个模块各 20% 时间片，单模块灵敏度低于独占（5.8G 告警在共享跑法下会漏，见双列表）；
4. 分类器是启发式的（只看带宽/持续/占空比），不做调制识别、不做设备级归因；
5. 「未抓到」≠「房间里没有摄像头」：看不到离线存储型设备、看不到非 ISM 频段。

---

## [REVIEW] 7. 设备端扫频状态机（feat/7-fsm）

**开工依据**：人类 2026-11-21 授权「方案 A」——把「谁掐表」搬进固件，判据一行不搬。

### 做了什么

| 侧 | 内容 |
| --- | --- |
| 固件 | `main/common/scan_fsm.{c,h}`（计划表 + 状态机 + 空闲钩子推进）、`spectrum_sweep_frame()`、`burst_serial_serve_idle()`、`CAPS` 加 `FSM`、`FSMPLAN*` / `FSM START\|STOP\|?` 六条命令 |
| 主机 | `core/sweep_source.py`（后台排空线程 + 窗缓存）、`core/serial_link.py`（FSM 订阅 / 读帧）、`host/session.py`（`engine` 配置）、`host/app.py --engine`、`testing/sweep_real.py`、`testing/fsm_probe.py` |
| 文档 | `docs/07-fsm.md`（含主机侧引擎口径、`plan_dwell_ms` 标定、pace/stale 语义、真板数字） |

**线上帧格式一个字节都没改**：仍是每窗 `SPEC` 头行 + 一帧 `SPC1`，主机的帧解析器原样复用。
所以回放、场景矩阵、真板标定全部继续有效。

### 验收材料 → 实测

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 固件协议探针（真板 COM56） | `python testing/fsm_probe.py` | **8/8**（caps/plan/start/frames/rotate/stop/idle/no-frames-after-stop） |
| 设备自扫 30 s（真板） | `python testing/sweep_real.py --seconds 30` | `frames 301 / misses 0 / distinct_mhz 90 / snapshots 235 / 9.75 帧每秒` |
| 驻留封顶 20 ms（真板） | 同上 `--plan-dwell 20` | `frames_per_s 30.72` |
| 主机逐窗对照 | `python testing/sweep_real.py --engine host --mock --seconds 6` | `frames 2174 / frames_src host-snapshots / 343.97 每秒 / [OK]` |
| 双引擎自检 | `cd host && python app.py --selftest --seconds 10 --mock [--engine device]` | 两条都是 `ok:true` / **52 checks** / `fail:[]` |
| 全量单测 | `python -m unittest discover -s tests -t .` | **280 tests OK**（71 s） |
| 会话级（两引擎） | `cd host && python tests/test_sweep_session.py -v` | **9 tests OK** |

### 边界

1. 不省射频时间：采样率 / 点数 / 驻留都没变，省的只是串口往返；
2. `engine=device` 的窗/秒是**串口带宽的函数**（10.2 @bins=1024 / 16.9 @bins=512），不是固件上限；
3. 自扫期间**禁止**发 `GAIN` / `CAP20`：它们和扫频共用射频，必须先 `FSM STOP`；
4. `FSM STOP` 后串口里还压着已发出的帧，停止 = 「边排空边找 `FSMEND`」；
5. 设备不支持时自动退回 `host` 并如实回报，不静默降级。

---

## [REVIEW] 7.1 上位机英文界面零中文

**人类硬要求**：英文环境下，除「中文(CN)」语言切换按钮本身，任何界面文案 / 动态内容 /
表格 / 提示 / 弹窗 / ASCII 输出都不得出现中文。

### 为什么不是「前端整块翻译」

界面上的中文有两个来源：网页静态词条（前端词典能管）和**模块现算的判词/表格/体检结论**
（`modes/`、`app/audit.py` 里全是中文，还带数字）。第二类没法在新前端翻——逐行翻会打碎
定宽对齐，而且数字一改模板就对不上。所以定了「**Python 侧下行本地化**」：

| 层 | 手段 |
| --- | --- |
| 静态词条 | `web/js/i18n.js` 双语词典；`T(key)` 在 `en` 缺 key **不回退中文**，返回 key 本身并记进 `missing()`（宁可亮 key 让自检抓，也不静默漏中文） |
| 模块产出 | `host/l10n.py`：中文串 → 数字段折叠成 `{}` 的「骨架」→ 查表换英文模板；**翻不到就原样返回，绝不抛异常**；只翻值不翻 key |
| 下行出口 | `tables()` / `describe()` / `stats()` / `sweep_state()` / `gain_state()` / `audit()` 全部过 `_out(payload)` |
| ASCII 页 | `render_text()` 英文模式**不再逐行翻模块 ASCII**，改从已本地化的 `tables()` payload 拼英文纯文本 |
| 兜底 | OS 给的串口描述（`USB 串行设备`）在 en 模式直接丢弃，只留 `COM56` |

### 顺手修掉的「英文下逻辑会错」

`level`/`risk_type` 这些字段在英文模式下**值已经变成英文**，而前端多处拿中文字面量比较
（决定标签颜色、3D 柱高、推荐信道色调）。新增 `U.levelKey()` / `U.riskClass()`：中英两套
写法映射到同一个语言无关的键，颜色/标签判断一律走它。

### 验收材料 → 实测

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 下行 payload（10 场景 × 6 出口 + `render_text`） | `cd host && python tests/test_l10n.py -v` | **22 tests OK**（85 s）；en 残留 0 / zh 仍有大量中文（防误翻） |
| 反向验证（清空词典） | 同上临时把 `l10n.TABLE` 清空 | **失败并报出 3586 条**残留 —— 证明这条断言真会抓东西 |
| 网页 DOM（6 页全扫，假固件） | `cd host && python app.py --selftest --seconds 10` | `en_cjk.hitCount = 0`、`missingCount = 0`（`en_no_cjk` / `en_keys_complete` 全绿），**52/52 项** |
| 网页 DOM（6 页全扫，**真板 COM56**） | `cd host && python app.py --selftest --real --port COM56 --gain-index 60 --seconds 12` | **`EXIT=0`，`ok:true`，`hits 0`，`missing 0`，`checks 52` 全绿**（`raw.panels 5`、`device.frames 314` / `misses 0` / `distinct 90`） |
| 骨架缺口自查 | `python testing/chk_l10n.py` | `英文模式下仍未翻掉的中文串 0 条 / 缺条目 0 / 占位不符 0` |
| 网页源码静态残留 | `python testing/_web_cjk.py` | 非兜底中文只剩「双语言映射表」与注释（兜底值都会被词典覆盖，DOM 扫描已证实） |

自检的门槛写死在 `host/app.py`：`CJK_SCAN_JS` 走 `TreeWalker` 扫 body 全部文本 + `[title]` /
`[placeholder]` / `option`，豁免 `[data-lang-toggle]`（只有这一个按钮允许中文）；逐页 1.7 s
（`raw` 页 2.6 s）等渲染再扫，6 页一轮。

### 只在真板才暴露的三类漏网（假场景全绿也抓不到）

| 类 | 现场 | 为什么 mock 抓不到 | 修法 |
| --- | --- | --- | --- |
| 负数字段 | 真板安静频段 `开关幅度 -2.0 码` | 假场景的开关幅度恒为正 → 骨架里多出个减号，整段模板不命中 | `NUM_RE` 带符号 `-?\d+(?:\.\d+)?`；模板里硬编码的 `≤-{}` 改 `≤{}`（负号随数字走） |
| 只在真板触发的风险档 | `宽带常开/形状疑似`（`RISK_FLAT`）+ 整段 hints + BLE 标题 | 假场景永远走不到这档（真板安静频段 `contrast_db < CONTRAST_DB` 就落到这里） | TABLE 补齐单名/两处 `风险：**…**（评分…）` / BLE 标题 / hints 整段；`test_l10n.py::RareRiskTypesTest` 造高斯鼓包固化 |
| **列表长度可变** | `- 有明显抬升（轻度以上）的信道：ch1, ch2, ch7, ch8, ch9, ch10。`（6 个） | 词典按 mock 见过的 1/4/5/9/10/11/12 个备条目，真板这次是 6 个 → 占位数量对不上 | 这三句改走 `LIST_PATTERNS` 正则（命名组、只换外壳，列表原样），长度任意；`ListSentenceTest` 把 1~12 全铺一遍 |
| **异常文案** | `SerialLinkError: 连接已标记失效，请重新打开`（经 `[RF error]` / 底部日志上屏） | 异常文案不走表格 payload，也不在 `modes/` 判词里；只有真板出错才出现 | `l10n.translate_error()`：先查表、再按 `ERROR_PHRASES` 片段换、最后剔掉非 ASCII（保证零中文，数字/设备回显保留）；`session.error_text()` 在 `rf_error` / `auto_throttle` / `thread_error` / `pack_failed` / `read_link_failed` 五处出口统一过一遍 |

### 边界

1. **命令行/终端输出仍是中文**：本轮只要求图形界面英文零中文，`app.live` 等 CLI 不动；
2. **模块判据一个字没改**：`modes/` 里的门槛、判词顺序、边界声明全部原样，英文只是文案层；
3. 英文模板是「骨架匹配 + 数字回填」，**新增判词必须同时补 `host/l10n.py` 的 TABLE**，
   否则该条会原样漏中文 —— `tests/test_l10n.py` 就是拦这个的；**列表长度会变的句子**
   别走 TABLE（占位数量随长度变），补 `LIST_PATTERNS`；**异常文案**一律过
   `session.error_text()`，别把 `str(exc)` 直接拼进界面串；
5. 异常文案为保零中文会**剔掉非 ASCII 部分**——固件回显与数字留着，中文细节只在终端日志里；
4. `_web_cjk.py` 里剩余的英文模式中文属于**双语言映射表**（逻辑用，不上屏），不是漏翻。

---

## [REVIEW] 7.2 串口自保：命令前排空 + 失效自愈

**人类反馈**：手动启动上位机后，**周期瀑布图页跑一会儿提示「断开串口」**，
并猜测是「状态机 / 换页调度」把链路搞坏的，建议改成实时频谱页那套调度。

### 先查证，再动手

| 查证 | 结果 |
| --- | --- |
| 是否与换页状态机有关 | **不是**。默认引擎 `host` 下压根不跑固件 FSM（方案 A 的 `scan_fsm.c` 只在 `--engine device` 生效）；60 s 真板自检里换页 6 次、400+ 窗，`device.frames 144 / misses 0`，**没掉链** |
| 掉链只有哪些入口 | `core/serial_link.py` 全场只有 **2 处**置 `failed = True`：帧 magic 对不上、帧解码失败 |
| 为什么「只有瀑布页」 | 瀑布页是唯一让人愿意长时间盯着看的页面（一轮 44 窗 ≈ 4.4 s，累积 240 行）；掉链概率约 **1/400~1/600 个窗**，看得越久越容易撞上，与页面调度无关 |
| 撞上之后为什么只能重启 | `EspSdrLink.open()` **不复位** `failed`，`scan()` 见 `failed` 直接抛「连接已标记失效」→ RF 循环连续 5 片失败 → 自动停机 → 界面「未连接」 |

根因链：一窗 `SPEC` 的 `SPECEND` 之后设备仍可能吐收尾帧（`drops` 非 0 时更明显）→
主机没读干净 → 下一条文本命令（`FREQ`/`GAIN`/`SPEC`）的 `readline()` 把二进制帧当行读掉
→ 随后的 `_read_exact(4)` 落在帧中间 → magic 不匹配 → `failed = True` → **永久死链**。

### 修法（不动 `core/` 的判据与解析）

| 位置 | 改了什么 |
| --- | --- |
| `host/link_guard.py`（新） | `ResilientLink(EspSdrLink)`：① 每条文本命令 `_write()` 前把读缓冲残留排空；② `scan()` 发现 `failed` 就先 `reopen()`（重开串口 + 重握手 + 复位失效标记），带 3 s 冷却防原地空转；③ `reopen_count` / `last_reopen_at` 可被界面/自检读到 |
| `host/session.py` | 链路换成 `ResilientLink`；新增 `relink()`：停自扫 → 重开链路 → **重设增益** → 重建自扫，返回 `{ok, message, count}`；补 `relink_ok/failed/closed` 中英文案 |
| `host/app.py` | RF 循环「连续失败 → 自动停机」之前先 `session.relink()`：成功就清零续跑并打一行日志，冷却中就歇 3 s 再试，真失败才停机（单次采集最多 20 次，防刷日志） |
| `core/sweep_source.py` | ① 修「计划外频点」的**双读者** bug：自扫期间 `scan()` 直接读串口会和 drain 线程抢，改成 `pause_around`；② drain 线程读到 `failed` 时自行 `reopen()` 并重发计划；③ `stats()` 增加 `reopens` |
| `host/l10n.py` | 顺带补 `无覆盖/有覆盖/未采样`（真板安静频段会出现「无覆盖」档，假场景不触发） |

**为什么重开后必须重设增益**：真板重开串口会 DTR 复位设备，`GAIN?` 回到 `GAIN HARDWARE -1`。
`relink()` 里 `self.gain.apply(link)` 就是为了不留半死状态（实测见下第 3 行）。

### 验收材料 → 实测

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 真板自保探针 | `python testing/probe_resilient.py --windows 24` | **`[OK]`**：24 窗 **1751 帧 / 2.9 s / 失败 0**（证明排空不打坏正常扫描）；`failed=True` 后 `scan()` **自动重开并出 66 帧**、`reopen_count 0→1`、`failed=False`；重开后 `5180 MHz / bins 512 / status 0` 正常 |
| 重设增益的必要性 | 同上第 3 行 | 重开后 `GAIN?` → `GAIN HARDWARE -1 0 81 0`（已回落 AGC）→ 证明 `relink()` 里必须补 `gain.apply()` |
| 单测（新） | `cd host && python tests/test_link_guard.py -v` | **6 tests OK**：残留必被排空、空缓冲不动、无端口不炸、失效自愈、冷却防热循环、重开失败仍留 `failed` |
| 单测（session 级） | `cd host && python tests/test_session.py -v` | **29 tests OK**（原 25 + 3 条 relink + 1 条双语文案）：`failed` 后**下一次 `step()` 自愈**、`source_errors` 不上升；自动自愈被冷却挡住时 `relink()` 能救回并使 `source_errors` 不再增长 |
| 全套回归 | `python -m unittest discover -s tests -t .`（分析层） | **280 tests OK（65.2 s）** |
| 真板 GUI 自检 | `cd host && python app.py --selftest --real --port COM56 --gain-index 60 --seconds 12` | **`EXIT=0`，`ok:true`，`checks 52/52` 全绿**；`en_cjk.hitCount 0`、`missing 0`；`device.frames 146 / misses 0 / distinct 90` |
| 假固件自检（设备自扫） | `python app.py --selftest --mock --engine device --seconds 10` | **`EXIT=0`，52/52 全绿**；`device.frames 101 / misses 0 / distinct 90` |
| **故障注入端到端**（真板 GUI） | `ESPSDR_FAULT_FAILED_AFTER_S=4 python app.py --selftest --real --port COM56 --gain-index 60 --seconds 12` | **`EXIT=0`、`ok:true`、`fault_injected:true`、52/52 全绿**、`hits 0`、`device.frames 260 / misses 0 / distinct 90` —— 跑到第 4 秒人为把链路标失效，采集没停、页面探针不空，证明「自动重同步」这条路径真的接上了（不接的话 RF 线程 5 片内自动停机 → 所有页面探针全空） |
| 类型词英文化 | `cd host && python tests/test_l10n.py KindWordsTest -v` | **2 tests OK**；`modes/capture.py` / `modes/guard_58g.py` 全部 `KIND*` 常量都有英文（故障注入那轮真板暴露出漏翻的 `持续单频点`） |

### 边界

1. **真拔线只能如实报错**：`reopen()` 失败会保留 `failed` 并返回失败，RF 循环照常「连续失败自动停机」，
   不会假装在采集；冷却期内不会原地狂重试；
2. **自愈会丢一窗数据**：重开串口要 1~3 s，那个窗的帧拿不回来（瀑布图会缺一格，不补）；
3. **不改 `core/` 判据**：`serial_link.py` 的解析与错误分类一个字没动，改动只在 host 侧包装 +
   方案 A 的 `sweep_source.py`（其双读者属实现 bug，非判据）；
4. `--engine device` 下自扫掉链的自愈靠 `reopen()` + 重发计划，真断线仍由上层「自动停机」兜底。


---

## [REVIEW] 7.3 真板三处「跑起来不对」的修复（feat/7-fsm）

人类真板实测报了三条：「实时频谱页卡在等待扫频进度」「周期瀑布页丢 5G HIGH 频段」
「BLE 风险评估完全没讲明白」。三条都是**真板 + 单射频分时**才会暴露的问题，
假固件全绿也照样出。逐条定位与修法如下。

### 7.3.1 实时频谱卡在「等待扫频进度」

- **现象**：进频谱页后顶部扫频条一直不动，频谱曲线也不刷新。
- **根因**：首屏 `Router.navigate()` 早于 `Bridge.init()`，`navigate()` 里的
  `setView()` 被 `if (window.Bridge && Bridge.ready())` 挡掉 ⇒ 主机侧
  `wanted_pages()` 收不到 `spectrum` ⇒ `wants_spectrum()` 为假 ⇒ 频谱帧从不下发。
  页面什么错都不报，只是「等」。
- **修法**：`router.js` 把上报抽成 `reportView()` 并导出；`app.js` 在
  `Bridge.init()` 就绪回调里补报一次（补发，不是取消门控）。
- **验证**：`--soak 40`；`frames` 从 43 稳定涨到 599、`passes` 只在真回卷时 +1。

### 7.3.2 周期瀑布丢 5G HIGH

一页里五个模块各自扫自己的计划，原本四处假设都只对「单模块独占射频」成立：

| # | 根因 | 修法 |
|---|------|------|
| A | 瀑布收下了**别的模块**的窗（频点回卷≠换轮） | `modes/waterfall.py` 建 `_plan_index`（按固件整 MHz 口径），`on_snapshot()` 只收自己的 44 个窗 |
| B | 帧里没有「这帧属于谁」 | host 层 `tag_module()` 包 `plugin.on_snapshot`，`snap.meta["module"] = plugin.id`（`core/` 不动） |
| C | 前端扫频条被别的模块的帧推着走 | `scanbar.js` `update()` 先比 `payload.module`，不同模块直接 return |
| D | 扫到一半的当前轮也被画成一行 ⇒ 排在最后的 5G HIGH 永远是空 | `web_payload()` 只画扫满轮次；全不满才退回画当前轮，并上报 `pending_windows`，页面写「当前轮已扫 n/44 窗」 |
| E | 一窗 ~60 帧全部落库，采集时间片 134 ms → 526 ms | 新增 `host/store_throttle.py` `WindowStore`：同窗最大保持合并、换窗或 `max_hold_s=1.0` 才落盘 |
| F | 用户看不出「为什么这行是空的」 | 页面显示「当前轮已扫 n/44 窗（扫满才画成一行）」 |

- **验证**（真板 `probe_wf.py 62`）：三个频段 `nonzero` 全部 **180/180 = 100%**，
  `last_row_nonzero` 也 180（修前 5G HIGH 是 240/66.7%、最后一行只有 60）。
  `--soak 95 --soak-page waterfall`：`rows 1 → 2`、`painted 100`、`frames` 连续涨。
- **边界**：五模块分时下一轮约 35 s，所以「行数」长得慢是设计使然，不是丢数据；
  只画满轮 = 宁可行少也不画错。

### 7.3.3 BLE 页「看不出到底有没有风险」

- **现象**：页面给判词标签 + 十几列数字，得自己拼判据才能回答「所以有没有风险」。
- **修法**：`modes/ble_risk.py` 新增 `verdict()`，把结论结构化下发：
  `code`（`clear/light/complex/flat/ble/wifi/mixed/nodata`）、
  `level`（`risk/watch/clear/nodata`，页面据此上色）、
  `why`（模板要填的数字全在这里，页面不自己算判据、不写死门槛）。
  `code` 与 `risk_type()` 由同一张 `VERDICT_CODE` 映射表绑定 ⇒ **结论与判词不可能互相矛盾**。
  页面新增结论条三行：**结论 / 凭什么 / 接下来做什么**；文案在 `i18n.js`，中英各一套，英文零中文。
- **验证**：自检新增 `ble_verdict` 检查（结论句非空、档位合法、三行都填），真板 **53/53 全绿**；
  mock 十个场景逐个过一遍，结论档位与判词一致（`quiet→clear`、`ble_crowded→ble`、
  `wifi_busy/wifi_suppress_ble/microwave→wifi`、`mixed→mixed`、`unknown_cw*→complex`）。
- **边界**：结论只到「有没有风险 / 是哪一类」，不做设备级归因（要归因得抓包）。

### 回归

- 全量 `python -m unittest discover -s tests -t .` **280 OK**；
- `host/tests/test_session.py` 29 OK、`test_l10n.py` 24 OK、`test_link_guard.py` 6 OK、
  `test_sweep_session.py` 9 OK；
- 真板 `--selftest --real --port COM56 --seconds 12` **53/53 全绿**（含 `en_no_cjk` 6 页 0 条、缺 key 0）；
- `python testing/chk_l10n.py` 残留 0 / 骨架 0 / 缺条目 0 / 占位不符 0。

### 7.4 上位机「5G HIGH 看不到」+ 全功能并行（feat/7-fsm）

用户报障 + 诉求：①扫频进度图上 5G HIGH 有热力、周期瀑布图上 5G HIGH 是空的 ——
「肯定有一个以上的热力图在说假话」；②既然按 nginx「一份数据多个消费者」改，就别再按切页裁剪功能。

| # | 根因 | 修法 |
|---|------|------|
| A | **染色口径让有信号的那段变黑**：`fitBandRanges()` 用 p5..p95 两端拉伸。一段里只要有强信号，p95 就被抬到信号上，底噪全挤到最暗（真板 5G HIGH 底噪 29 码、峰 186 码 ⇒ 底噪只剩 2% 亮度）→ 整段看着像"没扫到"；而没信号的 2.4G 反而满屏花色（跨度只有 8 码） | `waterfall.js` 改成**以本段 p10 底噪为 0 点、+30 码满亮**（`SPAN_CODES`），与后端 `ref_db` 取 p10 的口径一致 |
| B | **降级时只画最后一行**：一轮没扫满时 `web_payload()` 退回 `rows[-1:]`，而窗按频率顺序扫、高段排最后 ⇒ 那一行的高段永远空 | 改成画最近**两**行 + payload 加 `degraded`，高段上一轮的数据能显示出来 |
| C | 用户分不清「没数据」和「数据太暗」 | 段标题旁标 `底噪 N · 峰 M`；整段无有效格时画「本段暂无可视数据」；`probe()` 新增 `bright/brightRatio/ref/peak` |
| D | **按页裁剪模块**：`PAGE_MODULES` + `needed_modules()` 只算当前页的模块，切页会改射频调度节拍（真板实测掉到 0.37 窗/秒），切回来数据还是断的 | `needed_modules()` 恒全量、`wants_spectrum()` 恒 True；`PAGE_MODULES` 删除；自检两条改判据（`view_gating` = 每条留档 `modules` 数 = 模块总数且跨两页；`spectrum_gating` = 停在非频谱页时频谱帧计数仍涨） |

- **验证（本轮，假固件 `--selftest --seconds 15`）**：**57/57 全绿**、`ok: true`；
  瀑布页 `probe.bandStats`：`24g ref 28 / peak 128 / bright 76%`、`5gl ref 28 / peak 62 / 13%`、
  `5gh ref 30 / peak 123 / 6%`（真板 5.8G 图传在 mock 里本来就只占少数窗，峰比底噪高 93 码 ⇒ 满亮红）；
  `table_views` 60 条留档全部 `modules` = 5 个、跨 spectrum/waterfall/wifi/ble/capture/raw 六页；
  `spectrum_on_ble 617 → spectrum_after 624`（非频谱页仍在推）。
- **回归**：全量 **285 OK**；`test_session.py` 29 OK、`test_l10n.py` 24 OK、
  `test_waterfall.py` 20 OK、`test_sweep_session.py` 9 OK、`test_port_lock.py` 7 OK；
  `chk_l10n.py` 0/0/0/0；`node --check` 全过。
- **未验证（诚实标注）**：**真板未复测** —— COM56 被用户正在跑的 GUI 实例占着
  （`PermissionError(13)`），本轮画面证据来自假固件自检 + 用户自己出的真板 PNG
  （`.data/waterfall/waterfall-p1.png` 三段都有内容：亮像素 24g 14.5% / 5gl 9.6% / 5gh 7.4%，
  最亮 177 ⇒ 后端 ring 里 5gh 确实有数据，问题在染色与降级，不在采集）。

## [DOING] 8 圆屏本地面板（feat/8-round-display）

**需求**（人类原话）：「给开发板接入了 xiao 生态的一个圆形触摸屏，想要做一个整合的设置页面，
然后点击进去就可以打开我们现在上位机的这些图，频谱热图、3D 热图等。关于刷新率问题，
基于双缓冲 + DMA 的 pingpong 缓冲模式，注意好状态机和消息队列，别触发竞争了。」

**硬件**（已核实，详见 `docs/10-round-display.md`）：GC9A01 240×240 RGB565 + CHSC6X 触摸（I2C 0x2e）。
引脚经 XIAO ESP32-C5 映射落定：SCK=GPIO8、MISO=GPIO9、MOSI=GPIO10、CS=GPIO0、
DC=GPIO7、BL=GPIO11、TOUCH_INT=GPIO12、SDA=GPIO23、SCL=GPIO24。
来源：`Seeed_Arduino_RoundDisplay-main/src/lv_xiao_round_screen.h` + TFT_eSPI `Setup66_Seeed_XIAO_Round.h`
+ micropython `SEEED_XIAO_ESP32C5/pins.csv`（三处互相印证）。

### 三处必须先解决的冲突

| # | 冲突 | 结论 |
|---|---|---|
| C1 | `CONFIG_ESP_SDR_UART_TX_PIN=11` / `RX_PIN=12` 撞 **BL** 与 **触摸 INT** | 关掉 `CONFIG_ESP_SDR_UART_ENABLED`。已 grep 确认它被 6 处 `#if` 包着（`burst_serial.c` 3 处、`burst_gpio.c` 1 处、4 个 target 的 `receiver.c` 各 1 处），关掉干净；`burst_gpio.c` 也因此不再 reserve 这两个脚 |
| C2 | **GPIO7 是 strapping pin**，被 DC 占用 | 上电电平由屏模块决定 → 可能影响启动模式。**必须实测**（连续上电 20 次）；失败则需飞线把 DC 挪走，属硬件改动，要人类拍板 |
| C3 | GPIO25（D2）也是 strapping | 本方案不用 D2，保持空置 |

### 并发设计（人类点名要求的三件事）

1. **双缓冲 + DMA ping-pong** → 不做全屏双缓冲（240×240×2×2 = **230,400 B**，C5 只有 400 KB SRAM，
   还要跑 WiFi 混杂 + FFT + 串口）。改为**条带 ping-pong**：24 行 × 240 × 2 = 11,520 B × 2 = **23,040 B**，
   由 `esp_lcd_panel_draw_bitmap` 的 `on_color_trans_done` 驱动换块。
2. **不设共享 framebuffer** —— 每个视图实现 `render_band(view, dst, y0, rows)`，纯函数按行渲染，
   ping-pong 语义退化成「两条独立绘制任务」，从根上消除「两份画布不一致」。
3. **状态机** —— `ST_BOOT/ST_MENU/ST_VIEW/ST_SETTINGS` 单一枚举 + 一张 `TR[state][event]` 转移表，
   不写散落的 if。
4. **消息队列** —— `q_spectrum`（射频→UI，元素 72 B 只放 64 bin 摘要，深度 4，满则丢最旧）
   与 `q_touch`（触摸→UI，深度 8，满则丢新）。
5. **所有权表**（防竞争的核心）：SPI 总线只有 `ui_task` 能碰，I2C 只有 `touch_task` 能碰，
   射频侧两边都不碰；三条硬禁令 —— ① 禁止在 `on_color_trans_done`（ISR 上下文）里渲染，
   ② 禁止两个任务同时 `draw_bitmap`，③ 禁止 UI 任务直接调 `scan_fsm_*`（设置项必须走与 PC 命令
   同一个入口，受「只在两次扫描之间改参数」约束）。

### 分阶段验收

| 阶段 | 判据 |
|---|---|
| 8.1 驱动点亮 | 屏上真出三色条；关 UART 后 PC 侧 `SPEC` 扫频全功能回归通过；连续上电 20 次正常 |
| 8.2 触摸 + 菜单 | 点 6 格都能进/能回；圆外点击无反应；`tap_ok/tap_drop` 计数正确 |
| 8.3 频谱 + 瀑布 | 20 fps 稳定；`dma_wait_us < band_render_us`（证明重叠成功）；PC 侧窗率降幅 < 10% |
| 8.4 3D 热图 + WiFi/BLE 页 | 逐行投影不掉帧 |
| 8.5 并发压测 | 30 min 无死机、无堆泄漏、无丢帧尖峰 |

### 待人类确认（阻塞 8.1 动代码）

1. **屏是不是插在这块 ESP32-C5 上**（XIAO 座直插）？若接在另一块板而靠串口拿数据，方案要重写。
2. **UART 桥可以关吗**？关掉后 `CONFIG_ESP_SDR_UART_*` 那套能力失效（PC 侧本来只走 USB/COM56）。
3. **菜单项对不对**？现排 6 格：`频谱 / 热图 / 3D热图 / WiFi / BLE / 设置`；抓档没排进去（屏上无意义）。

### 边界（诚实标注）

- **未写一行固件代码**：本轮只做硬件核实 + 设计定稿 + 冲突排查。所有刷新率数字都是**纸面估算**，
  尚未实测（`ui_stats` 打点后才有真数字）。
- **不引 LVGL / Arduino 运行时**：本工程是 IDF v6.1，arduino-esp32 v3.x 绑 IDF 5.x，版本风险高；
  且要画的是逐像素波形与热图，不是控件树。GC9A01 初始化序列自己写，零第三方依赖。
- 屏上不做 IQ 抓档、不做中文（内置点阵只有 ASCII，4 MB flash 留给固件）。

## 阻塞与需求

- **应用侧健壮性缺口**（真机实测暴露，属 `core/scheduler.py`）：一次扫描失败会把链路
  置为 `failed`，调度器随后无退避地空转重试（实测一次卡死刷出 1349405 次同类错误）。
  修它要动 `core/`，按规则需人类裁决：是否允许在功能分支修 `core/scheduler.py`
  的失败重试/退避逻辑（或允许加"连续 N 次失败即停止本轮"的保护）。
- ~~5G 宽窗归属口径~~：已由人类裁决「可以窄、多次移动扫描、允许重叠」→ 本轮落实为
  20MHz 窗 / 15MHz 步进重叠滑扫 + 覆盖率掩码，**已解除**。
> 补充（任务 2 视角）：瀑布图不依赖上述两项修复，mock 与真机（`app.live`）都能出图。

- **上位机瀑布图页嵌不嵌**：已按人类「烧录了就做上位机给我」需求在 `feat/host-gui` 落地（`web/pages/waterfall/`），
  本源实现 `modes/waterfall.py:web_payload()`；不需要再等裁决。

- **固定增益档位的产品化口径**：当前靠人工给 `--gain-index 60`，上位机已给**主机侧**
  自动闭环（`--gain-mode AUTO`）。固件侧自动标定（逐档试扫、挑不饱和的最高档）仍然
  **需要人类裁决**，属于固件/应用层（不碰 `core/`）。
- **阶段 4 正样本缺口**（需要实物才能闭合）：没有真实 5.8G 图传/模拟摄像头，
  抓得到只能靠 `testing/guard_replay.py` 往真板数据里注入信号自证；
  有实物（哪怕一个 5.8G 无线摄像头）时可补一条真信号实测，把「抓到」从注入证据升级为实物证据。
- ~~阶段 4/5 是否放行~~：人类已授权「没有的就做模拟仿真，没问题就合并」→ 阶段 4/5 已交材料、
  阶段 6 合并已完成，**已解除**。
- **阶段 1~5 + T + 6 全部 `[REVIEW]`**：等人类整体验收（一次性给「确认完成」即可，可分批）。
  未确认前**不打远端 tag、不动 `main`**（`main` 仍等于上游 `3ab3b65`）。

## 约定

- 每条功能分支开工前：`git checkout -b feat/<n>-<name>`，动手前把本条改成 `[DOING]`。
- 收工：提交信息写清「做了什么 / 怎么验证 / 边界」，并在本文件补结果。