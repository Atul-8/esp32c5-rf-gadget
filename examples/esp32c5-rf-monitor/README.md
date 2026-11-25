# ESP32-C5 射频监测（host 侧应用层）

本目录是这个 fork 上的**应用业务代码**：五个射频监测功能（WiFi 体检 /
瀑布图 / BLE 风险评估 / 5.8G 守卫 / 可疑信号抓拍）的地基与模块家。

上层固件仍在本仓库 `main/`（ESP-IDF，C 语言）；这里只放**跑在电脑上的
Python 分析层**，通过串口（USB Serial/JTAG 或 UART0，2 Mbaud）与固件
通信，协议见 `docs/protocol.md`、`docs/spectrum.md`。

## 目录

```
examples/esp32c5-rf-monitor/
├── core/                 # 地基（功能分支只读，禁止修改）
│   ├── serial_link.py    #   esp-sdr 串口协议：SYNC/INFO/CAPS/SPEC/CAP16/CAP20
│   ├── snapshot.py       #   Snapshot 契约 + SPC1 帧解析（CRC32 校验）
│   ├── scheduler.py      #   平滑加权轮转：按 slice_weight 分时间片
│   ├── store.py          #   SQLite 主存档 + CSV 旁路双写
│   ├── registry.py       #   ModePlugin 契约 + modes/ 自动发现
│   └── classifier.py     #   启发式分类器（任务 5：先判分不分得清，再判像哪一类）
├── modes/                # 五个功能各占一个文件（功能分支各自添加）
│   ├── wifi_check.py     #   任务 1：WiFi 信道体检（20 MHz 窗 / 15 MHz 步进重叠滑扫）
│   ├── waterfall.py      #   任务 2：三频段轮扫 + 环形缓冲 + produce()
│   ├── ble_risk.py       #   任务 3：BLE 广播信道风险评估（六档判词 + 证据链）
│   ├── guard_58g.py      #   任务 4：5.8G 频段守卫（持续发射源 + 告警日志 + 落事件）
│   └── capture.py        #   任务 5：可疑信号抓拍（6 窗常驻缓冲 + 四道过滤 + CAP20 存证）
├── output/               # 输出基础件（表格/单位格式化），各功能再自带实现
│   ├── format.py         #   表格/单位格式化
│   ├── png.py            #   零依赖 PNG 编码器 + 5×7 点阵（任务 2）
│   └── heatmap.py        #   时频热力图 PNG + HTML 翻页（任务 2）
├── tests/                # mock 快照源（10 场景）+ 假固件 + 无硬件自测
├── testing/              # 手工实测/自证脚本（不进单测）：
│                         #   guard_replay（往真板数据注入图传）/ capture_real（真板抓档）
│                         #   scenario_matrix（10 场景 × 4 模块）/ capture_replay（判据回放 7 例）
│                         #   anti_peep_sim（反偷拍整场仿真：缺实物时自证「能发现」）
├── app/                  # demo（mock 全链路）/ live（真机·假固件）/ waterfall（瀑布图 CLI）
│                         # capture_link（抓档只在两次扫描之间落地）/ audit（一分钟体检报告）
├── host/                 # 图形上位机（PySide6 + WebView）：换实验只换网页
├── docs/                 # 阶段说明（00~05）+ 06-integration（五合一总验收报告）
└── todo.md               # 唯一任务事实源
```

## 快速开始（不需要硬件）

```sh
cd examples/esp32c5-rf-monitor

python -m app.demo                      # mock 全链路：注册→调度→收快照→入库→产出
python -m app.demo --scenario ble_crowded  # 场景清单见 tests/mock_source.py:SCENARIOS
python -m app.demo --weights "wifi_check:40,ble_risk:30,guard_58g:20,capture:10"
python -m unittest discover -s tests -t . -v    # 全量自测

# 任务 4：5.8G 频段守卫（持续发射源 + 告警日志）
python -m app.demo --scenario video_58g --duration 14   # mock：5728–5752 宽带块 + 5740 载波 + 弱 WiFi
#   产出：[alert] 表格（编号/类型/频点/带宽/抬升/占空比/持续时长）+ 判据原文
#   真板：python -m app.live --port COM56 --duration 45 --gain-index 60 --db .data/guard.db
python testing/guard_replay.py --db .data/guard.db      # 回放注入自证：往真板数据里塞一个图传

# 任务 5：可疑信号抓拍（分类 → 抓档 → 落 I/Q + sidecar）
python -m app.demo --scenario unknown_cw_clean --duration 12   # 干净环境里一个窄带持续源
#   产出：抓档清单（频点/样本/位宽/分类/置信度/存档路径）+ 四道过滤计数
python -m testing.capture_replay                               # 判据自证：7 个用例过四道门
python -m testing.scenario_matrix                              # 10 场景矩阵（含 5.8G 共享/单跑双列）
python -m app.audit --mock --scenario mixed --duration 30      # 体检报告（真板去掉 --mock）
#   真板：python -m app.audit --port COM56 --duration 60 --gain-index 60 --db .data/audit/real.db
#   边界：「已存证」≠「确认是摄像头」；杂散只报清单不抓；抓档只收 unknown

# 任务 2：周期性频谱瀑布图（10 分钟等效 mock 数据 → PNG + HTML 翻页）
python -m app.waterfall --minutes 10 --scenario mixed --ascii
#   产物：.data/waterfall/index.html + waterfall-pN.png/html
#   边界：**周期快照，非连续实时**；功率码是相对 dB 码（0..255），不是 dBm
```

`python -m app.demo --help` 有全部参数；产物落在 `.data/`（已被 gitignore）。

## 图形上位机（WebView 版）

除了命令行，还有一套**网页界面**的上位机（Python 只做串口/调度/打包 JSON，
界面全在 `host/web/`，换实验只换网页不用改 Python）：

```sh
python host/app.py --mock --scenario wifi_busy          # 没板也能看
python host/app.py --port COM56 --gain-index 60          # 真板
python host/app.py --selftest --seconds 8 --shot out.png # 自动自检（退出码 0/2）
```

六个页面：实时频谱（2D 曲线 + 3D 瀑布 + 2D 瀑布）、周期瀑布图（全频段时频
热力图 + 一键出 PNG/HTML）、WiFi 信道体检（排序表 + 3D 柱状图 + 推荐）、
BLE 风险评估（判定卡 + 证据链 + 3D 信道柱）、可疑信号抓拍
（存证清单 + 四道过滤计数 + 本机杂散清单 + 门槛表 + **一键体检**）、
原始输出（任意模块表格兜底 + CLI 同款 ASCII）。
细节与「怎么加新实验页」见 `host/README.md`。

## 接真机

```sh
pip install pyserial
python -m app.live --port COM5 --duration 60 --gain-index 60  # Windows（固定增益才有可比性）python -m app.live --port /dev/ttyACM0 --duration 60 --gain-index 60
python -m app.live --mock --duration 5                # 同一条代码路径的假固件版
```

固件烧写用仓库自带流程（根目录 README「Build and flash」）或浏览器安装器。
C5 的 Serial/JTAG 口和 UART0 都能跑频谱，`SPECINFO?` 会告诉你当前连接支持
哪些剖面（4/8/10/20/40/80 MS/s、256..2048 bin）。

## 硬性边界（写进产出物，不许美化）

- **分时扫描，不是连续实时**：`SPEC` 快照模式每次抓一段就停一次，帧与帧
  之间有接收空档。所有对外产物都要标「周期快照，非连续实时」。
- **功率码不是 dBm**：`bins_db` 存的是固件上算出来的相对 dB 码（0..255
  饱和），没有绝对增益校准，跨增益/跨设备只能比相对强弱。
- **单射频**：同一时刻只有一个模块在扫，五个功能靠 `slice_weight` 分时间片。
- 分类器是**启发式**（带宽/持续时长/占空比），不做调制识别、不做设备归因。

## 阶段状态

见 `todo.md`。阶段 0（地基）与功能 1~5、T（图形上位机）**都已交验收材料**，
统一在 `[REVIEW]` 等人类回「确认完成」；当前全量自测 **271 条 OK**，
上位机自检（假固件）40+ 项 checks 全绿。