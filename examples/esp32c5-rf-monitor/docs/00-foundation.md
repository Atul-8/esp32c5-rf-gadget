# 阶段 0 地基说明（验收材料）

分支：`feat/0-foundation`（若已合入则为 `main`）
适用固件：本仓库 `main/`（ESP32-C5，Serial/JTAG 或 UART0，2 Mbaud）

## 1. 这一阶段做了什么

| 文件 | 职责 | 关键点 |
| --- | --- | --- |
| `core/serial_link.py` | esp-sdr 串口协议封装 | `SYNC` 取租约、`INFO`/`CAPS`/`SPECINFO?`/`LIMITS?`/`TRANSPORT?` 握手；`SPEC` 扫描逐帧产出；`CAP16`/`CAP20` 原始 I/Q 抓取（自动把固件的 `(I,-Q)` 还原成 `(I,Q)`）；CRC32 校验；帧同步丢失即标记连接失效 |
| `core/snapshot.py` | 统一数据 schema | `Snapshot(ts, center_hz, bw_hz, bins_db, meta)`；`SPC1` 帧解析 + CRC；`SPS1` 统计记录解析；DC 居中 → fftshift 转成频率升序；占用率/噪声底/峰值/分带切片 |
| `core/scheduler.py` | 时间片轮转 | 平滑加权轮询（smooth WRR），按 `slice_weight` 分射频；单模块异常只影响自己那一片 |
| `core/store.py` | 存档 | SQLite `snapshots`(BLOB 功率码 + JSON meta) + `events` + CSV 旁路双写；按时间/频点查询、整库导出 |
| `core/registry.py` | 契约 + 自动注册 | `ModePlugin`（`id`/`name`/`slice_weight`/`scan_requests`/`on_snapshot`/`produce`）、`ScanRequest`、`Output`；`discover("modes")` 自动导入、按 id 稳定排序、隔离坏模块 |
| `core/classifier.py` | 分类器接口 | `SignalFeatures`/`Classification` 已定型，`classify()` 留给任务 5（当前 `NotImplementedError`） |
| `tests/mock_source.py` | 假快照源 | 8 个场景：`quiet`/`wifi_busy`/`wifi_suppress_ble`/`ble_crowded`/`microwave`/`video_58g`/`unknown_cw`/`mixed` |
| `tests/fake_firmware.py` | 假固件 | 假 pyserial 端口，按 `docs/protocol.md` 回 `INFO`/`CAPS`/`SPECINFO?`/`SPEC`+`SPC1`+`SPECEND`/`CAP16`/`CAP20` |
| `tests/mock_modes.py` | 演示插件 | `ProbePlugin`（不是真功能），用来验证 注册→调度→收快照→入库 |
| `app/demo.py` | mock 全链路演示 | `python -m app.demo` |
| `app/live.py` | 真机/假固件链路 | `python -m app.live --port COM5` / `--mock` |
| `output/format.py` | 输出基础件 | 中文对齐的等宽表格、单位格式化、方块条 |

## 2. 协议对齐（照 `docs/protocol.md` / `docs/spectrum.md` 抄的，不是猜的）

- `SYNC <nonce>` → 回显；打开端口可能复位板子，所以先同步再发命令。
- `INFO` → `C5SDR 6 burst 16380`；`CAPS` → 含 `SPECCAPS` 才有频谱；
  `SPECINFO?` → `{"profiles":[[fs, rate_code, bins, stride, units, continuous], ...]}`。
- `SPEC <ms> <stride> <units> <detector> <rate_code> <bins>` → 回复
  `SPEC <bins> <fs> <units> <MHz>`，随后是 `SPC1` 二进制帧（28 字节头 +
  bins + CRC32）与 `SPECEND`（12 个字段，status≠0 视为失败）。
- 头部关键字段：`[20:22]` 本帧合并的 FFT 数、`[22]` 标志位（bit0 最大保持、
  bit3 快照空档）、`[23]` 增益码、`[26]` log2(bins)、`[27]` dB 码倍率。
- 功率码是 `20*log10(Q15 域功率)` 的四舍五入值，0..255 饱和 —— **相对量，不是 dBm**。
- `CAP16` = 每对 16 位（I 8 位 + -Q 8 位）；`CAP20` = 每对 20 位（各 10 位，LSB 先紧密打包）；
  两者都传 `(I, -Q)`，解析时取反还原。
- 线上 bins 顺序以 DC 为首（负频率在后半段）；`Snapshot` 统一转成频率升序，
  下游模块不再关心 fftshift。

## 3. 调度语义

- 每个模块在自己的文件里声明 `slice_weight`，调度器按权重轮转（参考配比
  WiFi 40 / BLE 30 / 5.8G 20 / 抓拍 10，各自改自己的数字）。
- 每个时间片 = 一次 `SPEC` 驻留（`dwell_ms`、`bins`、`detector` 由模块的
  `ScanRequest` 决定），一次驻留可产出多帧快照，全部喂给 `on_snapshot` 并入库。
- 单模块 `scan_requests`/`on_snapshot` 抛异常只记一次错误，不阻塞其它模块；
  射频层异常（串口/CRC）只废掉当前时间片。
- 合并 = 五个文件同时躺在 `modes/` 里，`discover()` 自动接线，无共享文件冲突。

## 4. 怎么验证（一条命令 + 预期现象）

```sh
cd examples/esp32c5-rf-monitor

# 1) 全量自测：43 条，全绿
python -m unittest discover -s tests -t .

# 2) mock 全链路（无硬件）
python -m app.demo --duration 2 --weights "wifi_check:40,ble_risk:30,guard_58g:20,capture:10"
#    预期：调度统计的「占比」列 ≈ 40% / 30% / 20% / 10%；
#          「入库」行数 = 帧快照总数；打印最近帧的峰值频点与噪声底

# 3) 假固件走真机同一条代码路径
python -m app.live --mock --duration 2 --no-store
#    预期：固件 C5SDR、传输 USB、8 个剖面、状态 0、错误 0
```

实测（本机 Windows，Python 3.12，2026-11-18）：

```
Ran 43 tests in 2.8s
OK

调度统计（共 30 个时间片 / 60 帧快照）
ble_risk 30.0% / capture 10.0% / guard_58g 20.0% / wifi_check 40.0%
入库：60 行

python -m app.live --mock --duration 1
调度统计（225 片 / 450 帧 / 1.0s），错误 0，最后一次扫描 2442MHz / 80MS/s / 512bin / 状态 0
```

真机尚未测试（本机没有接 ESP32-C5）：`app.live` 的握手与解析路径已被假固件
覆盖，但**真机首次联调仍需人工确认**，见 `todo.md` 的验收项。

## 5. 边界声明（本阶段做不到什么）

- 真机联调未做：所有数字来自 mock 与假固件。
- `classify()` 未实现（任务 5 负责），其余四个功能模块不存在（阶段 0 的 `modes/` 必须为空）。
- 瀑布图没有画图依赖（numpy/matplotlib 留给任务 2，按需引入）。
- 没有做成连续流：固件快照模式本身有接收空档，本层不掩盖这个物理边界。
- 频点分辨率是 1 MHz（`FREQ <MHz>` 整兆），且接受频点不等于 PLL 一定锁定。

## 6. 下一阶段

按任务书写：`git checkout -b feat/1-wifi-check`，只加 `modes/wifi_check.py`
和它自己的输出实现；**不许改 `core/`**，接口不够用就在 `todo.md` 写需求等人裁决。