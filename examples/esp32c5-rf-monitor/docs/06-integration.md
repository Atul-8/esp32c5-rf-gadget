# 06 · 五合一合并与总验收报告（`integration` / `v1.0.0`）

> 本文是任务书阶段 6 的交付物：五个功能模块 + 图形上位机合并成一条可跑通的链路，
> 附**每一项都能自己复跑**的验证命令与实测输出，以及**必须随结论一起说**的边界。

---

## 1. 交付物一览

| 阶段 | 分支 | 代码 | 文档 | 验收状态 |
| --- | --- | --- | --- | --- |
| 0 地基 | `feat/0-foundation` | `core/`（registry/scheduler/serial_link/snapshot/store）、`output/format.py`、`tests/`（mock 源 10 场景 + 假固件 + 43 条自测） | `docs/00-foundation.md` | `[REVIEW]` |
| 1 WiFi 信道体检 | `feat/1-wifi-check` | `modes/wifi_check.py`（20 MHz 窗 / 15 MHz 步进重叠滑扫 + 覆盖率并集） | `docs/01-wifi-check.md` | `[REVIEW]` |
| 2 频谱瀑布图 | `feat/2-waterfall` | `modes/waterfall.py`、`output/png.py`、`output/heatmap.py`、`app/waterfall.py` | `docs/02-waterfall.md` | `[REVIEW]` |
| 3 BLE 风险评估 | `feat/3-ble-risk` | `modes/ble_risk.py`（p80 电平 + 窄带占比 + 肩部 max 三判据、六档判词） | `docs/03-ble-risk.md` | `[REVIEW]` |
| 4 5.8G 频段守卫 | `feat/4-guard-58g` | `modes/guard_58g.py`（13 窗 / 5710–5885 MHz / 14.58 MHz 步进、五类判词、跨窗聚类） | `docs/04-guard-58g.md` | `[REVIEW]` |
| 5 可疑信号抓拍 | `feat/5-capture` | `core/classifier.py`、`modes/capture.py`、`app/capture_link.py`、`app/audit.py` | `docs/05-capture.md` | `[REVIEW]` |
| T 图形上位机 | `feat/host-gui` | `host/`（PySide6 + QWebEngine + QWebChannel，6 页，Python 对页面零知识） | `host/README.md` | `[REVIEW]` |
| 6 五合一 | `integration` | 本文 + tag `v1.0.0` | 本文 | `[REVIEW]` |

**目录边界**：应用业务代码全在 `examples/esp32c5-rf-monitor/`；固件 `main/` 保持「哑射频前端」
（不收频率计划、不做判决、不存结论）。

---

## 2. 合并流程（按任务书 1→2→3→4→5，每合一次跑一次全量）

```bash
git checkout -b integration refs/heads/main
git merge --no-ff feat/1-wifi-check -m "merge(integration): 阶段 0 地基 + 阶段 1 WiFi 信道体检"
python -m unittest discover -s . -p "test_*.py"        # → 58 OK
git merge --no-ff feat/2-waterfall                     # 冲突：todo.md 阶段表 / test_registry 注释
python -m unittest discover -s . -p "test_*.py"        # → 97 OK
git merge --no-ff feat/3-ble-risk                      # 冲突：同上两处
python -m unittest discover -s . -p "test_*.py"        # → 130 OK
git merge --no-ff feat/4-guard-58g                     # 冲突：todo.md 阶段表 + 阻塞条目
python -m unittest discover -s . -p "test_*.py"        # → 186 OK
git merge --no-ff feat/5-capture                       # 无冲突
python -m unittest discover -s . -p "test_*.py"        # → 271 OK
git merge --no-ff feat/host-gui                        # 无冲突（内容已等价）
python -m unittest discover -s . -p "test_*.py"        # → 271 OK
```

| 步 | 合入 | 冲突 | 冲突处理（只动冲突行，不改逻辑） | 合完单测 |
| --- | --- | --- | --- | --- |
| 1 | `feat/1-wifi-check` | 无 | —— | 58 OK |
| 2 | `feat/2-waterfall` | `todo.md` 阶段表、`tests/test_registry.py` 注释 | 阶段表两侧进度合并、注释取信息更全的一句 | 97 OK |
| 3 | `feat/3-ble-risk` | 同上 | 同上 | 130 OK |
| 4 | `feat/4-guard-58g` | `todo.md` 阶段表 + 「阻塞与需求」条目 | 取验收摘要更全的一侧 | 186 OK |
| 5 | `feat/5-capture` | 无 | —— | 271 OK |
| 6 | `feat/host-gui` | 无 | —— | 271 OK |

> **阶段 0 说明**：阶段 0 的提交（`1f36a4e`）从未单独进 `main`，它一直是 `feat/1` 的父提交，
> 所以第 1 步合 `feat/1` 时连带把地基一起带进来，顺序与任务书一致。

**等价性证明（关键）**：

```bash
git diff --stat refs/heads/feat/host-gui -- examples/esp32c5-rf-monitor ':!*/docs/06-integration.md' ':!*/todo.md' ':!*/README.md'
# （空输出）
```

即 `integration` 的**代码内容逐字节等于**已验收过的 `feat/host-gui`；相对 `feat/host-gui` 只多了
三处**文档**变更：本文 `docs/06-integration.md`、`todo.md` 的阶段 6 登记、`README.md` 的目录说明。
合并过程没有引入任何新逻辑，冲突解决也没改判据、没改门槛、没改渲染。

---

## 3. 五项验收标准 → 实测结果

| # | 任务书要求 | 实测结果 | 复跑命令 |
| --- | --- | --- | --- |
| 1 | 调度器把五个模块的时间片按配比轮转、互不阻塞 | `share` = waterfall 0.201 / wifi 0.198 / ble 0.201 / guard 0.201 / capture 0.201（各 20%，公平） | `cd host && python app.py --selftest --seconds 10`（看 `share`） |
| 2 | 五功能产出物齐备 | wifi 渠道表 38 行 + 推荐信道；瀑布图 PNG + HTML 索引；BLE 六档判词 + 分数；5.8G 五类告警 + 事件落库；抓档 IQ + sidecar + 一键体检 | 同上（`wifi` / `waterfall` / `ble` / `capture` 段） |
| 3 | 合并版全链路演示 | 上位机 6 页全绿：频谱 / 瀑布 / WiFi / BLE / 抓档 / 原始输出 | 同上 + `python -m app.demo` |
| 4 | 每合一次跑全量 | 58 → 97 → 130 → 186 → 271 → 271，**六次全绿** | 见 §2 |
| 5 | tag `v1.0.0` + 最终 STOP | annotated tag 已打（**未推远端**，等人类验收），`main` 未动 | `git tag -n1 v1.0.0` |

---

## 4. 缺正样本的地方：用仿真补，并且说清补到哪里为止

任务 4/5 的验收句里都有「真来了要抓到」，但**手上没有真的 5.8G 图传、也没有真的偷拍摄像头**。
人类裁决「没有的就做模拟仿真」，落地为 `testing/anti_peep_sim.py`：

```bash
python -m testing.anti_peep_sim --json .data/anti-peep-sim.json
```

| 阶段 | 数据 | 模块 | 实测 |
| --- | --- | --- | --- |
| 1 | `video_58g`（5.8G 模拟图传：宽平顶 5728–5752 MHz + 载波 5740 MHz） | 只跑 `guard_58g`，独占射频 260 片 | `analyses 131` → **告警 1 条**：5741 MHz / 2.7 MHz 宽 / 107.1 s |
| 2 | `unknown_cw_clean`（2.4G 干净处一根 2454 MHz 窄带未知源 + 普通 WiFi/BLE 背景） | 只跑 `capture`，独占射频 240 片 | `triggers 275` → **抓档 2 条**：2454 MHz |
| 3 | 两阶段产出合流 | `app.audit.verdict()` | **「发现可疑信号并已抓档」** + 理由里明确「这不是确认是摄像头」 |

断言 6 项全 `true`：`guard_alerts` / `guard_analyzed` / `capture_taken` / `capture_triggered` /
`verdict_not_empty`（结论不是「什么都没发现」）/ `verdict_not_overclaim`（不说「确认是摄像头」）。

同时新增 mock 场景 `hotel_scan`（整场：5.8G 模拟图传 + 2.4G 隐藏窄带源 + 普通 WiFi/BLE 背景），
在场景矩阵里登记为第 10 个场景。矩阵对它的断言只写 `capture_max 4` / `guard_analyses_min 5`
（= 「在算 + 不误炸」），**「能发现」由上面的独占跑法自证** —— 因为四个模块共享一根射频时，
守卫要「每窗连续 8 帧」、抓档要「同窗 ≥3 s 跨度」，帧被摊薄就攒不够，这是分时共享的设计代价，
不是判据失效（`video_58g` 早已用「共享 / 单跑」双列表登记同一个事实）。

**补到什么程度为止（诚实边界）**：

- 仿真是**合成信号**，噪声底、时间戳都是假的。它证明的是「判据在这类形状上会响」，
  **不能**证明「真摄像头的波形一定长这样」；
- 有实物（哪怕一台 5.8G 无线摄像头、一个无线拾音器）时应补一条真信号实测，
  把注入证据升级为实物证据；本仿真脚本届时降级为回归用例，不退场。

---

## 5. 全链路验证命令与实测输出

在 `examples/esp32c5-rf-monitor/` 下：

```bash
# ① 全量单测（10 个 mock 场景 × 所有模块 + 协议/存储/调度/分类器）
python -m unittest discover -s . -p "test_*.py"
# Ran 271 tests in 53.577s
# OK

# ② 场景矩阵（10 场景 × 4 模块，共享 + 5.8G 单跑双列）
python -m testing.scenario_matrix --slices 240
# 全部场景断言通过（10/10 PASS，约 60 s）

# ③ 抓档判据回放（7 个用例：该抓的抓、该拦的拦）
python -m testing.capture_replay
# 全部用例符合预期（7/7 PASS）

# ④ 整场反偷拍仿真（缺实物时的灵敏度自证）
python -m testing.anti_peep_sim --json .data/anti-peep-sim.json
# OK 三阶段全部符合预期（告警 1 + 抓档 2 + 结论不越界）

# ⑤ 上位机自检（真窗口渲染 + 探针，别把 --seconds 压到 8）
cd host && python app.py --selftest --seconds 10
# [selftest] {"ok": true, ... "checks": {44 项全 true}, "fail": []}
```

上位机自检的关键读数（本轮实测）：

| 项 | 值 |
| --- | --- |
| `ok` | `true`，`fail: []`，44 项 checks 全 `true` |
| `share` | `{waterfall: 0.201, wifi_check: 0.198, ble_risk: 0.201, guard_58g: 0.201, capture: 0.201}` |
| `canvases` | `scanbar` 0.546 / `spec` 0.297 / `3d` 0.106 / `wf` 0.115（非黑像素占比，画布真画了东西） |
| `layout` | 1280×800 / 1024×680 / 900×620 / 760×560 四档全 `ok:true`、`bad:[]`、`overflowY: ok` |
| `capture` 探针 | `thresholds 16` / `notes 13` / `audit: 有需要复核的持续信号` / `auditRows 12` |
| `waterfall_render` | `1 page(s): waterfall-p1.png (PNG) / index.html (HTML index)` |

---

## 6. 合并后的边界（必须随任何结论一起说）

1. **周期快照 ≠ 连续实时**：同一时刻只有一个频段在被扫；抓档期间射频停扫。
2. **功率码是相对 dB 码，不是 dBm**：跨增益不可比，体检期间必须固定增益。
3. **单射频分时共享**：5 个模块各 20% 时间片，单模块灵敏度**低于**独占跑法
   （5.8G 守卫在共享跑法下会漏告警，已在矩阵用「共享 / 单跑」双列表量化）。
4. **分类是启发式的**：只看带宽 / 持续 / 占空比，不做调制识别、不做设备级归因。
   `unknown` 只代表「形状不像已知类型」，不代表「这是摄像头」。
5. **「本轮未抓到」≠「房间里没有摄像头」**：看不到离线存储型设备、看不到非 ISM 频段、
   看不到被墙体衰减到噪声以下的设备。
6. **真板本机杂散**：`real2.db` 里 4 根固定杂散（2427/2455/2465/2468 MHz，宽 0.16–0.27 MHz，
   fill ≥ 0.89）与窄带窃听器（CW）**物理上不可区分**，只能靠「靠近对比」人工复核；
   抓档模块已按设计把它们记进 `spurs[]` 清单而**不**抓档。
7. **频段口径三方不一致**：任务书 5700–5900 / 早期 `todo.md` 5150–5850 / 代码 5710–5885，
   代码以「13 窗 × 20 MHz、步进 14.58 MHz、覆盖 5710–5885」为准，文档已注明。
8. **增益**：真板闭环实测 `60 → 58 → … → 48`（AGC 会自动降档）；固件侧自动标定**未做**，
   仍是人工 `--gain-index 60`。

---

## 7. 已知未完成 / 等裁决（不藏）

| 项 | 状态 | 谁决定 |
| --- | --- | --- |
| `core/scheduler.py` 失败退避 | 一次扫描失败置 `failed` 后无退避空转（实测刷出 1,349,405 次同类错误）。改它要动 `core/` | 需人类授权 |
| 固件侧增益自动标定 | 未做（主机侧 AUTO 闭环已可用） | 需人类裁决 |
| 5.8G 摄像头 / 偷拍设备实物正样本 | 无 → 已用仿真补（§4） | 需硬件 |
| 安静房间底噪复核 | 环境找不到，改用仿真 + mock `quiet` 守 | 需场地 |
| `real3.db` 3 条抓拍（2468/2410/2462 MHz） | 均宽于 `SPUR_MAX_HZ` 故按设计不拦，**待人工复核具体是什么** | 需人眼/实物 |
| 阶段 1~5 + T + 6 验收 | 全部 `[REVIEW]` | 等人类「确认完成」 |
| `v1.0.0` tag 推远端 / `main` 快进 | tag 已打本地；`main` 仍 = 上游 `3ab3b65` | 等人类验收后执行 |

---

## 8. 一句话结论

**五个功能 + 上位机已合并成一条可跑通的链路，六步合并每步全量单测全绿（58→271），
合并后代码逐字节等于已验收过的 `feat/host-gui`（只多三处文档）；缺实物的两处灵敏度证明确认为仿真自证
（5.8G 告警 1 条 / 2.4G 抓档 2 条 / 结论不越界）；边界（分时、相对码、启发式、无设备级归因）
全部写在报告里。最终验收权在人类。**