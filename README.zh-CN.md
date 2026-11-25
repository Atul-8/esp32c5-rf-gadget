# ESP32-C5 RF Gadget（射频小圆屏）

[English](README.md) | **简体中文**

一台装在桌上的射频探测小 gadget，基于 **Seeed XIAO ESP32-C5** +
**Seeed Round Display for XIAO**（1.28" GC9A01 圆屏）。板子自主扫
2.4 GHz / 5 GHz WiFi 频段并把结果画上圆屏——全程离线自转。Python
上位机（可选）从同一条串口流取数据，产出表格、热力图与 CSV 导出；
关掉它，小圆屏照转不误。

本项目衍生自上游 **[ESPARGOS/esp-sdr](https://github.com/ESPARGOS/esp-sdr)**——
没有这个上游仓库就没有本项目。致谢与 GPL-3.0 要求的
[修改声明](#修改声明gpl-30)见下文。

---

## 修改声明（GPL-3.0）

本程序按 GPL-3.0 发布，不附带任何担保（见[许可证](#许可证)）。按 GNU
GPL v3 §5(a) 要求，本仓库作出如下修改声明：

- **基线**：上游 [esp-sdr](https://github.com/ESPARGOS/esp-sdr) 固件，
  修改如下所列（2026-11 起）。
- **版权**：上游固件与文档版权归 ESPARGOS 项目贡献者；下文标注
  「新增 / 修改」的内容版权归本仓库所有者（2026 年起）。
- **修改的上游文件**（固件，C）：
  - `main/common/scan_fsm.c` —— 新增设备侧扫频状态机（`FSM` / `FSMPLAN`
    命令）；帧格式不变。
  - `main/common/spectrum.c`、`spectrum.h` —— 新增逐剖面能力上报
    （`SPECN` / `SPECCAPS` 等）。
  - `main/common/burst_serial.c`、`burst_serial.h`、`burst_gain.h` ——
    帧序修正与增益上限调整。
  - `main/families/c5_c6_c61/receiver.c` —— ESP32-C5/C6/C61 接收机时钟修正。
  - `main/CMakeLists.txt` —— 注册下述新增的显示/触摸组件。
- **新增固件文件**：`main/common/round_ui.c/.h`（圆屏 UI：三页、触摸手势、
  自动轮播、诊断包）、`main/common/rd_font*.c/.h` 与 `rd_pal.c/.h`
  （点阵字库与调色板，由 `tools/gen_font2.py` 生成），以及三个新组件：
  `components/dsh_display`（GC9A01 圆屏驱动）、`components/dsh_touch`
  （CHSC6X 触摸驱动）、`components/dsh_hal`（共用 IDF HAL）。
- **新增应用**（Python，跑在电脑上）：`examples/esp32c5-rf-monitor/`——
  串口链路、扫频调度、分析模式、PySide6 + WebView 图形上位机（`host/`）、
  单元测试与阶段验收材料（`docs/00`–`10`）。
- **新增工具**：`testing/round_sim.py`（离屏圆屏模拟器）、
  `testing/round_desk.py`（实数据调试台）、`testing/occ_view.py`、
  `testing/record_occ_trace.py`、`testing/demo_tour.py`、
  `tools/gen_font2.py`（字库生成器）。

其余部分（`main/` 的 burst 协议、`docs/`、`tests/`、`platform/`、`tools/`
辅助脚本）来自上游项目；`docs/` 下的上游文档描述共用的串口协议。

---

## 功能

**板子上**（ESP32-C5 + GC9A01 圆屏）：

- **离线自主**：上电即自扫 + 轮播；上位机只是可选的观察/配置端。
  拔掉电脑圆屏照转——上位机退出后，板子在主机静默约 3 秒后自动
  恢复巡扫，逻辑全部在单片机里。
- 设备侧自动扫频 2.4 GHz / 5 GHz（每轮 30 窗、约 3 s 一圈），经 USB
  Serial/JTAG 以 2 Mbaud burst 帧（约 16 KB/帧）流出。
- 圆屏三页 UI，触摸切换或自动轮播（逐页驻留秒数，默认 8/25/8，可被上位机控制）：
  - **RING** —— 频段色环 + 各频段占用（时间占空比）。
  - **CHART** —— 信道功率表，手势翻页。
  - **WATERFALL** —— 钉住单一信道的频谱 + 时间瀑布。
- 触摸手势：纵向滑动翻页/钉信道；左侧条与底部圆点切视图。
- 串口命令集（`RD` 前缀）供电脑侧控制：视图/翻页、增益、轮播驻留
  （`RD VIEWSEC`）、诊断包。

**电脑上**（`examples/esp32c5-rf-monitor/`）：

- 桌面上位机（PySide6 + WebEngine，中英文界面）五页：
  1. 实时频谱（当前窗）。
  2. 热力图记忆（全频段时频图）。
  3. 异常热点（信道负载 + 未知设备）。
  4. 信道占用 —— 38 信道逐条占空比 + CSV 导出。
  5. 圆屏轮播控制（启动/停止/读回板子状态）。
- 增益控制：手动 index、主机侧自动闭环、硬件 AGC。
- 假固件模式（`--mock`）——没有硬件也能玩全部功能；另有自检探针
  （`--selftest`）与 130+ 单元测试。
- CLI 模式：`python -m app.live`（WiFi 信道体检）、
  `python -m app.waterfall`；其余分析模式（BLE 广播风险、5.8G 守卫、
  可疑信号抓拍）位于 `modes/_legacy/`。

---

## 文件架构树

```
esp32c5-rf-gadget/
├── main/                        ESP-IDF 固件（C）
│   ├── common/                  串口 burst 协议 + 增益表 + 扫频 FSM
│   │   ├── burst_*.c/.h         2 Mbaud 帧协议（上游，含修正）
│   │   ├── scan_fsm.c/.h        [修改] 设备侧扫频状态机
│   │   ├── spectrum.c/.h        [修改] FFT 剖面 + 能力上报
│   │   ├── round_ui.c/.h        [新增] 圆屏 UI + 轮播 + 手势
│   │   └── rd_font* / rd_pal    [新增] 点阵字库与调色板（生成物）
│   ├── families/c5_c6_c61/      [修改] C5/C6/C61 接收机时钟
│   ├── targets/                 板级引脚定义（上游）
│   └── diagnostics/             固件自诊断（上游）
├── components/
│   ├── esp-dsp/                 Espressif DSP 库（git 子模块）
│   ├── dsh_display/             [新增] GC9A01 圆屏驱动
│   ├── dsh_touch/               [新增] CHSC6X 触摸驱动
│   └── dsh_hal/                 [新增] 两者共用的 ESP-IDF HAL
├── examples/esp32c5-rf-monitor/ [新增] 电脑侧应用（本项目主体）
│   ├── core/                    串口链路、扫频源、调度器、分类器
│   ├── app/                     CLI 入口（live / waterfall / audit / demo）
│   ├── modes/                   分析模式（waterfall 生效；其余在 _legacy/）
│   ├── host/                    PySide6 桌面上位机
│   │   ├── app.py / session.py  主窗口 / 会话与 Qt 桥接
│   │   ├── round_ctl.py         圆屏轮播控制（意图队列）
│   │   ├── gain.py              增益控制（手动 / 自动 / 硬件）
│   │   ├── web/                 前端（五页 HTML/JS，中英 i18n）
│   │   └── tests/               上位机单元测试（130+）
│   ├── tests/                   app 层单元测试 + 假固件
│   ├── docs/00–10.md            阶段验收材料（判据 + 实测数字）
│   └── testing/                 开发期验证脚本
├── testing/                     [新增] 圆屏模拟器与调试台
├── tools/                       固件辅助脚本（上游）+ gen_font2.py [新增]
├── docs/                        上游文档：串口协议、原理说明
├── tests/                       上游固件 C 测试
├── platform/                    上游构建辅助
└── LICENSE                      GNU GPL v3
```

---

## 环境依赖

**固件** —— 在任何能跑 **ESP-IDF v6.1** 的机器上编译（推荐 Linux；
Windows 本机可用但明显慢）：

- 硬件：Seeed **XIAO ESP32-C5** 开发板 + Seeed **Round Display for
  XIAO**（GC9A01 圆屏，CHSC6X 触摸）。
- ESP-IDF v6.1，含 ESP32-C5 目标支持。
- `components/esp-dsp` 子模块（见下方克隆步骤）。
- 一根 USB 线（USB Serial/JTAG 会以 COM/tty 串口出现）。

**电脑应用** —— Python **3.10+**，Windows/Linux/macOS 均可：

- `PySide6>=6.5`（Qt WebEngine 外壳）、`numpy`、`pyserial>=3.5`
  （仅串口需要；`--mock` 与单元测试只用标准库即可跑）。

---

## 快速上手

```sh
# 1. 克隆（含 DSP 子模块）
git clone https://github.com/Atul-8/esp32c5-rf-gadget.git
cd esp32c5-rf-gadget
git submodule update --init --recursive

# 2. 编译固件（在 ESP-IDF v6.1 环境里）
idf.py set-target esp32c5
idf.py build
idf.py -p <端口> flash monitor      # Windows 如 COM56，Linux 如 /dev/ttyUSB0
```

> 在哪编译：任何装了 ESP-IDF 的机器都行。Windows 本机编译比 Linux 明显
> 偏慢；全量重编必须重跑 `set-target`（干净构建下 ESP-IDF 会落到默认目标芯片）。

```sh
# 3. 运行桌面上位机
cd examples/esp32c5-rf-monitor
pip install -r host/requirements.txt

python host/app.py --port COM56 --gain-mode MANUAL --gain-index 60
python host/app.py --mock --scenario wifi_busy     # 无需硬件
# Windows 也可双击 host/start.bat

# CLI（无界面）：固定增益跑 60 秒 WiFi 信道体检
python -m app.live --port COM56 --duration 60 --gain-index 60
python -m app.live --mock --duration 5             # 假固件

# 4. 测试与自检
cd examples/esp32c5-rf-monitor/host
python -m unittest discover -s tests -t tests      # 上位机单元测试
python app.py --selftest --mock --seconds 5        # 端到端自检
```

打开上位机，选好串口，点**开始**；顶部导航在五个页面之间切换，
语言切换器支持中/英文。

---

## 致谢

本项目是 **[ESPARGOS/esp-sdr](https://github.com/ESPARGOS/esp-sdr)** 的衍生之作——
SDR 机制、burst 串口协议、固件骨架以及 `docs/` 协议文档的全部功劳属于上游作者。
感谢他们以 GPL-3.0 开源发布。

其他依赖，按各自许可证使用：

- [Espressif esp-dsp](https://github.com/espressif/esp-dsp)（git 子模块）。
- [Qt / PySide6](https://www.qt.io/) 与 Python（桌面上位机）。

---

## 许可证

> 本程序为自由软件：你可以在自由软件基金会发布的 **GNU 通用公共许可证**
> （**第 3 版**，或你选择的任何更高版本）条款下重新分发和/或修改它。
>
> 本程序按「有用即发布」的愿望分发，但**不附带任何担保**；亦不附带
> 适销性或特定用途的默示担保。详见
> [GNU 通用公共许可证 v3.0](LICENSE)。

完整许可证文本见 [`LICENSE`](LICENSE)。上游
[esp-sdr](https://github.com/ESPARGOS/esp-sdr) 采用 GPL-3.0；本仓库沿用
该许可证，并将上游版权归属 ESPARGOS 项目贡献者。
