# 任务 5：可疑信号抓拍 + 酒店反偷拍一键体检（`feat/5-capture`）

> 本文件是阶段 5 的验收材料。三件事都做了，判据都在真板（COM56）上跑过：
>
> * **分得清才动手**：先判「分不分得清」再判「是哪一类」，已知类型（WiFi/BLE/微波炉/图传）
>   **一律不抓**；`wifi_busy` 满负荷 WiFi 场景误抓从 2 条降到 **0 条**（§4）。
> * **抓得住**：`CAP20` 抓 16380 样本 I/Q（20 bit / 20 MS/s / 819 µs），落盘 + sha256 +
>   sidecar 元数据（含前后各 30 s 频谱上下文）。
> * **能收口**：`app.audit` 把五个模块的产出汇成一张「活跃发射源清单 + 可疑证据」，一句话结论。

## 1. 做了什么

| 文件 | 作用 |
| --- | --- |
| `core/classifier.py` | 全项目**唯一的分类器**：启发式判 `wifi/ble/microwave/analog_video/unknown`，门槛集中在文件顶部 |
| `modes/capture.py` | 抓拍插件：2.4G 6 窗滑扫 → 找候选 → `classify()` → 只抓 `unknown` → 存 I/Q + sidecar + 事件 |
| `app/capture_link.py` | `CaptureAwareSource`：**只在两次扫描之间**清队列（同根串口边扫边抓会打乱读缓冲）；`FakeCapture` / `synthetic_capture` 供无硬件自测 |
| `app/audit.py` | 「酒店反偷拍」一键体检：跑一轮全频段，出清单 + 结论 + JSON 报告 |
| `tests/test_classifier.py` (16) / `tests/test_capture.py` (41) / `tests/test_audit.py` (12) | 判据、抓拍流程、体检汇总 |

未动 `core/` 其余文件、未动其它模块的判据。

## 2. 扫描计划

| 常量 | 值 | 依据 |
| --- | --- | --- |
| `BAND_LO_HZ` / `BAND_HI_HZ` | 2400 / 2483.5 MHz | 2.4G ISM 全段（802.11 b/g/n 与 BLE 都在里面） |
| `SCAN_BW_HZ` / `SCAN_STEP_HZ` | 20 / 15 MHz | 与功能 1/2/3/4 同一口径，相邻窗重叠 ≥5 MHz |
| `DWELL_MS` / `BINS` | 100 / 512 | 39.06 kHz/格 |
| 窗数 | **6**（2410.0 / 2422.7 / 2435.4 / 2448.1 / 2460.8 / 2473.5 MHz） | 首窗左沿压 2400，末窗右沿压 2483.5 |
| `RING_FRAMES` | 40 | 每窗 40 帧环形缓冲（~4 s 上下文） |
| `ANALYZE_MIN_INTERVAL_S` | 0.9 | 一次窗扫 ~0.1 s，1 s 分析一次够了 |
| `slice_weight` | 1.0 | 单射频共享时间片：5 个模块各 20%（不许谁多占，META-0087） |

```bash
python -c "from modes.capture import window_centers as w; c=w(); print(len(c), [round(h/1e6,1) for h in c])"
# 6 [2410.0, 2422.7, 2435.4, 2448.1, 2460.8, 2473.5]
```

## 3. 判据表（每条都对应一个具体失败现象）

### 3.1 候选检测（`CaptureMode.candidates()`）

| 环节 | 参数 | 为什么是这个值 |
| --- | --- | --- |
| 参考底噪 | 均值谱 `FLOOR_PCT=10` 分位 | 用 p25 会把「占窗 90% 的宽带」当整体底噪抬升，平顶直接消失（实测 `p25 60.0 / max 0.0`）；换 p10 后 17 MHz 平顶正常判出 |
| 均值谱平滑 | `SMOOTH_BINS=5` 滑动中位 | 单格噪声能骗出「10 码抬升」；平滑后边界再用**未平滑**的抬升收紧，免虚宽 |
| 抬升门槛 | `ALERT_DB=20` 码 | 真板底噪自身抖动实测 ±10~18 码，10 码压不住噪声 |
| 窄段门槛 | `<NARROW_MAX_HZ=1 MHz` 的段要求 `≥NARROW_ALERT_DB=30` 码 | 噪声纹波就能造出 300 kHz 的「+20 码凸起」；真值得抓的窄带源（本振/杂散/时钟）实测都 +30 码以上 |
| 宽度 | `MIN_WIDTH_HZ=100k` ~ `MAX_WIDTH_HZ=19.5M` | 低于 3 格不可信；高于可判宽度是窗切出来的假象 |
| 占比 | `OCC_EXCESS_DB=10` 算「这一帧该格有信号」 | 与抬升门槛分开：均值看持续、单帧看占空 |
| 占空 | `HOP_FILL=0.20` 起算、`STEADY_FILL=0.75` 算持续 | 间歇源 20% 就要能看见，否则「跳频摄像头」一律漏 |
| 持续性 | `MIN_FRAMES=8` 连续帧 | 单帧毛刺不判；25% 占空源在 40 帧里约 10 帧，仍过得去 |
| 不判区 | 窗中心 ±`CENTER_NOTCH_HZ=250k`、窗沿 ±`EDGE_GUARD_HZ=500k` | 接收机直流/LO 泄漏 + 滤波器滚降 |
| 窗边界切开 | 贴着可判边界且 `<15 MHz` 的段直接丢（`skipped_edge`） | **本轮关键修复**：强 WiFi 信道在邻窗只露出 5 MHz 尾巴，宽度不可信，之前被判成「分不清」反复抓 |
| 直流格不断段 | `runs_of_state()` 三态 `True/None/False` | 19 MHz 平顶横跨窗中心时，挖掉的直流格若断段会劈成左右两个 ~9 MHz「WiFi」 |
| 重抓抑制 | 同频点 ±`MATCH_HZ=3 MHz`、`RETRIGGER_S=120 s` | 同一个源不该反复抓 |
| 预算 | `MAX_CAPTURES=20`、每轮 `max_per_slice=1` | 抓档时射频停扫，抓太勤会把时间片吃光 |

### 3.2 分类器（`core/classifier.py`，判定顺序即优先级）

| 顺序 | 标签 | 条件 |
| --- | --- | --- |
| 1 | `microwave` | 带宽 ≥15 MHz **且** 占空 ≥70%（`AMBIGUOUS_*`）——「和满负荷 WiFi 分不清」 |
| 2 | `analog_video` | 带宽 4–8 MHz **且** 占空 ≥75% **且** 持续 ≥5 s |
| 3 | `wifi` | 带宽 ≥8 MHz **且** 占空 ≥50% |
| 4 | `ble` | 带宽 1–2.5 MHz **且** 占空 ≤60% |
| 5 | `unknown` | 其余（含 ≤0.5 MHz 的持续窄带、跳频样、中间地带） |

**顺序的理由**：先具体后笼统。原来 `wifi` 在 `analog_video` 之前，会把 6 MHz 图传吃掉；
而 15 MHz 以上高占空如果落 `unknown`，满负荷 WiFi 会被反复抓拍 —— 所以给它一个**已知大类**
`microwave`，置信度压到 0.45，判词明说「只敢给大类不敢给设备」。

**已知信号不抓** 是产品的硬要求：抓拍只在 `label == "unknown"` 时触发。

## 4. 触发与预算（为什么「已知信号不触发」）

mock 五个场景各跑 15 s（`frames_per_scan=2`）：

| 场景 | 抓拍数 | 说明 |
| --- | --- | --- |
| `wifi_busy`（满负荷 WiFi） | **0** | 修之前是 2~4 条：17.4 MHz/95% 被判 `unknown`；邻窗 5.6 MHz 尾巴被判「分不清」；300 kHz 噪声纹波被判窄带 |
| `unknown_cw`（WiFi+BLE+两个 CW） | 3 | CW 与中间地带照抓 |
| `quiet` | 0 | 安静环境零误报 |
| `mixed` | 3 | 微波炉/图传环境只抓分不清的那部分 |
| `video_58g` | 0 | 5.8G 不在本模块扫描范围 |

`skipped_*` 计数器在 `web_payload()` 里全公开（`skipped_budget/repeat/edge/weak`），
挡掉了多少、为什么挡，界面上能直接看。

## 5. 抓档与存档

```bash
python -m app.live --port COM56 --duration 60 --gain-index 60      # 真机
python -m app.demo --scenario unknown_cw --duration 14             # 假固件同一条代码路径
```

- 抓档命令：固件 `CAP <samples>`，`samples ≤ 16380`（`IQ_WORDS`）。参数取 `CAPTURE_SAMPLES=16380`、
  `CAPTURE_BITS=20`、`CAPTURE_RATE_CODE=2`（20 MS/s，与 20 MHz 窗同宽）。
- 文件名：`cap{序号:03d}-{频点 MHz}MHz-{YYYYmmdd-HHMMSS}.iq16`（int16 LE 交错 I/Q，`samples*4` = 65520 B），
  同名 `.json` sidecar。
- 频点必须是整 MHz（固件 `FREQ` 只收整 MHz）：`freq_hz = round(center_hz/1e6)*1e6`。
- 落库：`store.add_event("capture", "captured", {...})`（由 `app.bind_capture` 注入 store）。

### 5.1 sidecar 字段

| 字段 | 含义 |
| --- | --- |
| `kind` / `id` / `freq_hz` / `samples` / `bits` / `rate_code` / `sample_rate_hz` | 抓档身份 |
| `duration_us` | 819 µs（16380 / 20 MS/s）——一次突发，不是录制 |
| `format` | `int16 LE interleaved I,Q` |
| `sha256` | 文件哈希（防篡改 / 可复核） |
| `classification{label,confidence,reason}` | 分类器判词原文（中文，不翻） |
| `candidate{Candidate.as_dict()}` | 触发它的候选：中心/宽度/占空/帧数/抬升/持续 |
| **`context`** | 前后各 `CONTEXT_S=30 s` 本窗均值谱（下采样 `CONTEXT_BINS=64` 格）+ `noise_p10` / `peak` / `frames` |
| `context_note` | 没有可用快照时**如实写**「没有可用快照上下文」，绝不假装有 |
| `boundary` | 3 条边界声明（一次突发非连续 / 相对码非 dBm / 启发式不归因） |

## 6. 「酒店反偷拍」一键体检（`app/audit.py`）

```bash
python -m app.audit --port COM56 --duration 90 --gain-index 60
python -m app.audit --mock --scenario mixed --duration 20 --json .data/audit/mixed.json
```

输出四件事：**活跃发射源清单**（5.8G 守卫告警 / BLE 风险 / 重度 WiFi 信道 / 可疑抓拍）、
**一句话结论 + 理由**、**证据文件路径**、**JSON 报告落盘**（`kind: "anti_peep_audit"`）。

结论分支写死四档，措辞不给过头话：

| 情况 | 结论 |
| --- | --- |
| 有 `unknown` 抓拍 | 「发现可疑信号并已抓档」+ **「这不是『确认是摄像头』」** |
| 只有窄带持续源 | 「有需要复核的持续信号」 |
| 有抓拍但都分类为已知 | 「有抓拍但分类为已知类型」 |
| 什么都没有 | 「本轮未抓到可疑信号」+ **「不等于房间里没有摄像头」** |

报告 `boundaries` 里固定带四条：分时快照非连续实时 / 相对码非 dBm / 启发式不归因 /
「未发现」≠「安全」（看不到离线存储型设备与非 ISM 频段）。

## 7. 实测记录

### 7.1 假固件（本机，同一条代码路径）

```
$ python -m unittest tests.test_classifier tests.test_capture tests.test_audit
Ran 69 tests ... OK

$ python -m app.demo --scenario unknown_cw --duration 14
调度统计：59 片 / 118 帧 / 错误 0；5 模块各 20%
事件：capture 2 条

$ python -m app.audit --mock --scenario mixed --duration 20
| 来源         | 类型      | 频点    | 带宽    | 持续   | 等级   |
| BLE 风险评估 | 混合型    | -       | -       | -      | 关注   |
| 可疑抓拍     | CAP-001   | 2402MHz | -       | -      | 可疑   |
| 5.8G 守卫    | 持续窄带  | 5731MHz | 6.5 MHz | 17.4 s | 需复核 |
| WiFi 信道    | 2.4G ch1  | -       | -       | -      | 重度   |
结论：发现可疑信号并已抓档（1 个分不清类型的源已存证）
报告：.data/audit/mixed.json
```

### 7.2 判据自证：合成快照回放（`testing/capture_replay.py`）

真板没有真偷拍设备，所以「判据按设计工作」用合成快照单独压一遍，输出人眼可读对照表：

```
$ python -m testing.capture_replay
| 用例           | 期望       | 实际       | triggers | 证据                                          | 判决 |
| clean_cw       | capture    | capture    | 3        | 2437.00 MHz / 16380 样本 / rate_code=2        | PASS |
| pure_spur      | spur       | spur       | 0        | 2450.09 MHz / 312 kHz / fill=1.00             | PASS |
| short_span     | skip_short | skip_short | 0        | span=1.00s < 3.0s                             | PASS |
| crowded        | skip_busy  | skip_busy  | 0        | skipped_busy=3 span=4.75s                     | PASS |
| weak_narrow    | no_capture | no_capture | 0        | span=4.75s                                    | PASS |
| edge_clipped   | no_capture | no_capture | 0        | span=4.75s                                    | PASS |
| wideband_known | no_capture | no_capture | 0        | span=4.75s / 分类 microwave → 非 unknown 不抓 | PASS |
全部用例符合预期
```

两条踩过的坑写进脚本注释了，后来人别再踩：

1. **喂帧时间基准必须与 `time.time()` 同线**。写成 `t0=1000.0` 这种过去值，模块内部
   新鲜度判断（拿真实 `time.time()` 当 now）会认为所有帧都过期 → 所有用例一律
   `skipped_short`（第一次跑 7 个用例 6 个假红就是这个）。
2. **`skipped_short` 是全局计数器**，空窗每轮都会 +1；判断「这一例是不是被跨度门挡了」
   必须看**目标窗自己的 `span_s()`**，看计数器会把每个用例都误标成 `skip_short`。

### 7.3 场景矩阵（`testing/scenario_matrix.py`）

9 个 mock 场景 × 4 个真模块（WiFi / BLE / 5.8G / 抓档），一条命令出一张总表：

```
$ python -m testing.scenario_matrix --slices 240        # ≈ 59 s
| 场景              | 抓档长相 | WiFi 忙/网数 | BLE 分/判词        | 5.8G 共享/单跑 | 抓档条数     | busy/short/spur | 断言 |
| quiet             | cw       | 0/0          |   5 无明显风险     | 0/-            | 0（trig 0）   | 0/0/0           | PASS |
| wifi_busy         | wideband | 12/12        |  16 WiFi 压制型    | 0/-            | 0（trig 0）   | 81/0/0          | PASS |
| wifi_suppress_ble | wideband | 11/11        |  22 WiFi 压制型    | 0/-            | 0（trig 0）   | 80/0/0          | PASS |
| ble_crowded       | cw       | 0/0          |  82 蓝牙设备密度型 | 0/-            | 3（trig 86）  | 0/0/2           | PASS |
| microwave         | wideband | 10/10        |  30 WiFi 压制型    | 0/-            | 1（trig 43）  | 79/0/0          | PASS |
| video_58g         | wideband | 1/1          |   5 无明显风险     | 0/1            | 0（trig 0）   | 0/0/0           | PASS |
| unknown_cw        | cw       | 4/4          |  18 复杂噪音       | 0/-            | 0（trig 0）   | 83/0/0          | PASS |
| unknown_cw_clean  | cw       | 4/4          |  18 复杂噪音       | 0/-            | 1（trig 77）  | 83/0/0          | PASS |
| mixed             | cw       | 9/9          |  44 混合型         | 0/-            | 3（trig 62）  | 104/0/41        | PASS |
全部场景断言通过
```

矩阵本身暴露了两个**真问题**（是设计取舍，不是 bug，已改进断言口径）：

- **`video_58g` 在四模块共享射频时 5.8G 告警为 0**。5.8G 守卫要「每个窗连续 8 帧」才敢报警，
  而 13 个窗 × 四模块轮转把单窗帧摊得太稀，共享跑法本来就攒不够。所以表里改成双列：
  **共享列只验「在算」（`analyses ≥ 5`）**，「能报警」由**单模块独占 260 片**那一列验（`0/1`）。
  换句话说：**四模块同跑时 5.8G 灵敏度会下降**，这是单射频分时的固有代价，别指望它和独占跑一样灵。
- **`ble_crowded` 会抓 3 条**（不是 2 条）。BLE 拥挤场景里除广播信道那几根，还有一批
  「窄带但不在广播信道」的源，分类器只看形状不看信道 → 判 `unknown` → 进抓档队列。
  这是预期行为（抓的是「分不清」，包含「像是 BLE 但不在广播信道」），断言上限放到 3。

## 8. 真板实测（COM56，20 位 / 20 MS/s，`--gain-index 60`）

### 8.1 抓档链路（单次 `CAP20`）

```
$ python -m testing.capture_real --port COM56
CAP20 → 16380 样本 = 979 µs @ 20 MS/s   # 请求 16384，固件上限 16380
线上字节 40950（bits/8 × 2 × samples = 2.5 B/对）
存档字节 65520（int16 LE 交错 = samples × 4）
sha256 线上 == sha256 存档             # 一致，无截断/错位
分类结论 unknown 0.65                  # 「分不清」而不是「危险」
```

### 8.2 本机杂散：只报清单，不抓（`real2.db`，60 s / 15074 快照 / 错误 0）

| 频点 | 宽度 | 占空 | 峰值抬升 | 持续 | 处置 |
| --- | --- | --- | --- | --- | --- |
| 2455.0 MHz | 0.27 MHz | 0.93 | 33.9 码 | 37.5 s | 进 `spurs[]`，不抓 |
| 2465.0 MHz | 0.16 MHz | 1.00 | 35.4 码 | 37.5 s | 进 `spurs[]`，不抓 |
| 2427.0 MHz | 0.27 MHz | 0.95 | 30.1 码 | 44.0 s | 进 `spurs[]`，不抓 |
| 2468.0 MHz | 0.16 MHz | 0.89 | 32.7 码 | 52.5 s | 进 `spurs[]`，不抓 |

四根都是板载固定杂散（本振/时钟谐波），宽度 0.156–0.27 MHz 全在 `SPUR_MAX_HZ`(500 kHz) 内。
**这就是四道门里最难看的一条**：真板环境里每块板子都有几根这样的窄带，抓它只会把
「可疑存证」刷满，所以按「报清单不抓」处理；代价是**窄带窃听器（CW）与板载杂散物理上
不可区分**，只能靠「搬机靠近看会不会跟着变强」人工复核。

### 8.3 spur 过滤后仍存的 3 条（`real3.db`）

| 频点 | 宽度 | 占空 | 峰值 | 持续 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 2468.0 MHz | 1.17 MHz | 0.91 | 39.1 码 | 32.3 s | 宽 > 500 kHz → 不该被杂散门拦 |
| 2410.0 MHz | 2.81 MHz | 0.92 | 36.7 码 | 38.6 s | 同上 |
| 2462.0 MHz | 1.88 MHz | 0.88 | 29.9 码 | 39.7 s | 同上 |

三条都 **比板载杂散宽一个量级**（1.17 / 2.81 / 1.88 MHz vs 0.16–0.27 MHz），
所以杂散门不拦它们是**按设计**，不是漏过。IQ 已存证（`.data/capture/cap00{1,2,3}-*.iq16`），
结论按规矩只写「待人工复核」，不写「是摄像头」。

### 8.4 一分钟体检（`app/audit.py`）

```
$ python -m app.audit --port COM56 --duration 60 --gain-index 60 --db .data/audit/real3.db
headline  发现可疑信号并已抓档
why       3 个分不清类型的源已存证（2468.0 MHz, 2410.0 MHz, 2462.0 MHz），
          请人工复核 IQ 文件；这不是「确认是摄像头」。
sources   17 条（1 条 BLE 关注 + 3 条抓拍 + 13 条 5.8G 窗）
captures  3 条（unknown 0.5）
boundaries 4 条
报告      .data/audit/audit-20261119-003957.json
```

### 8.5 上位机：抓档页 + 一键体检

假固件自检（`python host/app.py --selftest --seconds 10`）：

```
capture_page  true   {"rows":1,"filters":7,"spurs":1,"thresholds":16,"notes":13,
                      "warn":175,"records":"1","audit":"发现可疑信号并已抓档",
                      "auditRows":6,"auditBoundary":4,"probe":{"kind":"capture"}}
capture_wired true   session.capture_plugins 非空且 capture_dir 非空
```

顺带修掉的两个前端 bug（都在 `host/web/js/bridge.js` / `modes/capture.py`）：

- `Bridge.call` 内部已经把 JSON 字符串 `JSON.parse` 成对象了，JS 侧再 parse 一次会
  报「解析失败」→ 改成 `typeof data === "string"` 才 parse。
- `take_pending()` 原来「全取走再切片」，被切掉的那几条请求**静默丢**（一轮冒出多个
  可疑源时第二条永远抓不到）→ 改成只取前 `limit` 条、剩下的留队，`skipped` 才看得出来。

## 9. 边界（必读，产出物与界面上都写）

1. **一次突发，不是录制**：`CAP20` 上限 16380 样本 = 819 µs @ 20 MS/s。抓档期间射频停扫（单射频分时）。
2. **相对量不是 dBm**：功率码与 I/Q 都未经绝对校准；跨增益不可比，体检请固定 `--gain-index`。
3. **分类是启发式的**：只看带宽/持续/占空比，不做调制识别，不做设备级归因（「像不像图传」≠「是谁的摄像头」）。
4. **看不到的东西**：低于噪底的源、只看 2.4G 的窗口、离线存储型设备、非 ISM 频段（如 1.2G/3.5G 图传）。
5. **`unknown` 是「分不清」而不是「危险」**：抓拍是**线索**，结论必须人拿 IQ 文件复核。
6. **抓档只收 `unknown`**：16 MHz 平顶会被分类器先判成 `microwave`（已知大类）→ **不抓**。
   「没抓」不等于「没信号」，只是「不用抓」，别把两种情况混着读（`capture_replay` 有专门用例钉住）。
7. **窄带窃听器与板载杂散物理上不可区分**：宽度 ≤500 kHz 一律当杂散报清单，不抓不存证。
   要区分只能人工搬机靠近看强度是否跟着变（本机固定杂散不随位置变）。
8. **四模块同跑时 5.8G 灵敏度会下降**：单射频分时 + 13 窗轮转，共享跑法里单窗帧被摊薄，
   「每窗连续 8 帧」这条可能攒不够（`scenario_matrix` 的共享/单跑双列就是在量化这件事）。