# 10. 圆屏本地面板（Round Display for XIAO / GC9A01 240×240）—— 设计与边界

> 分支：`feat/8-round-display`（从 `feat/7-fsm` 0763b0b 拉出）
> 目标：把 PC 上位机里那几个视图搬到板载圆屏上，做「一个设置页 + 点进去看频谱 / 瀑布热图 / 3D 热图」。
> 状态：**设计定稿，未写代码**（等硬件接线确认，见 §9）。
>
> ⚠️ **本文是阶段 8 的历史快照**（当时的设计判据与实测）。实现后的演进（页名
> `HEATMAP`/`OCCUPY`/`WATERFALL`、页 2 读占空比、页 3 单窗瀑布 + 页 2 同款翻页、
> UNII 四段色环、圆心 MHz/频率/频段名三行布局、轮播每页独立秒数 `RD VIEWSEC`）
> 以根目录 [`AGENTS.md`](../../../AGENTS.md)「圆屏显示层」各段为准，本文不再同步更新。

---

## 1. 硬件事实（已核实，非推测）

### 1.1 屏与触摸

| 项 | 值 | 来源 |
|---|---|---|
| 显示控制器 | **GC9A01**，240×240 RGB565，SPI | 官方库 `driver.h`：`BOARD_SCREEN_COMBO 501`；`lv_xiao_round_screen.h` |
| 触摸控制器 | **CHSC6X / CHSC6540**，I2C 地址 **0x2e** | 同上；协议 = 读 5 字节，`buf[0]==0x01` 表示按下，`buf[2]=x`、`buf[4]=y` |
| 触摸中断 | INT 低有效，默认上拉 | `chsc6x_is_pressed()` 读两次防抖 |
| 依赖 | Arduino + LVGL + TFT_eSPI/Arduino_GFX | `library.properties` |

### 1.2 引脚（XIAO 丝印 → ESP32-C5 GPIO）

XIAO ESP32-C5 的映射（micropython `pins.csv` 与 arduino-esp32 `variants/XIAO_ESP32C5/pins_arduino.h` 两处一致）：

| 丝印 | GPIO | Round Display 用途 |
|---|---|---|
| D0 | 1 | — |
| D1 | **0** | **TFT_CS** |
| D2 | 25 | —（strapping pin，空着） |
| D3 | **7** | **TFT_DC** ⚠️ strapping pin |
| D4 | **23** | **触摸 I2C SDA** |
| D5 | **24** | **触摸 I2C SCL** |
| D6 | **11** | **背光 BL** ⚠️ 与 UART TX 冲突 |
| D7 | **12** | **触摸 INT** ⚠️ 与 UART RX 冲突 |
| D8 | **8** | **SPI SCK** |
| D9 | **9** | **SPI MISO**（GC9A01 只写，可不接） |
| D10 | **10** | **SPI MOSI** |

### 1.3 已确认的三处冲突

| # | 冲突 | 处理 |
|---|---|---|
| C1 | `CONFIG_ESP_SDR_UART_ENABLED=y`，`TX=11 / RX=12` 撞背光与触摸 INT | **关掉 UART 桥**（`CONFIG_ESP_SDR_UART_ENABLED=n`）。PC 侧走 USB Serial JTAG（COM56），UART 桥本就多余。改前先 grep `CONFIG_ESP_SDR_UART` 的全部使用点 |
| C2 | **GPIO7 = strapping pin**，被 `DC` 占用 | 上电瞬间 DC 电平由屏模块决定，可能影响启动模式。**必须实测**：反复上电 20 次确认能进 app；若失败，考虑把 DC 挪到 D0/D2（需飞线，属硬件改动，要人类拍板） |
| C3 | GPIO25（D2）也是 strapping | 本方案不用 D2，保持空置 |

**射频侧不冲突**：C5 的采样走 WiFi 混杂模式 + `adctrig()`（`families/c5_c6_c61/receiver.c`），不占外部 GPIO，屏 SPI 用 GDMA 与它不共享外设。

---

## 2. 软件栈选型：IDF 原生，不用 Arduino/LVGL

| 方案 | 结论 | 理由 |
|---|---|---|
| A. 把 arduino-esp32 作为 IDF component | ❌ | arduino-esp32 v3.x 绑 IDF 5.x，本工程是 **IDF v6.1**，版本风险高；且会拉进整个 Arduino 运行时 |
| B. 移植 LVGL | ❌ | 我们要画的是**逐像素波形与热图**，不是控件树。LVGL 的 draw_buf + flush 回调只增加一层间接与 RAM 开销 |
| C. **IDF 原生 `esp_lcd` + 自写渲染器** | ✅ | `esp_lcd_panel_io_spi` 自带 `on_color_trans_done` 回调（DMA 完成通知），ping-pong 直接可做；RAM 与刷新时序完全可控；零第三方依赖 |

GC9A01 的初始化序列自己写（约 40 条命令，参考公开 datasheet 与 TFT_eSPI 的 `GC9A01_Init.h`），不引 component manager，避免构建机联网依赖。

---

## 3. 缓冲策略：条带 ping-pong（不用全屏双缓冲）

**算术**：240×240×2 B = **115,200 B/帧**。

| 方案 | RAM | 能否上 C5 |
|---|---|---|
| 全屏双缓冲 | 230,400 B | ❌ C5 只有 400 KB SRAM，还要跑 WiFi 混杂 + FFT + 串口 |
| 全屏单缓冲 | 115,200 B | ⚠️ 勉强，但 DMA 期间 CPU 不能碰同一块 → 等于没双缓冲 |
| **条带 ping-pong（本方案）** | **2 × 11,520 = 23,040 B** | ✅ 条带 24 行 × 240 列 × 2 B |

```
         ┌── band A (11,520 B) ──┐      ┌── band B (11,520 B) ──┐
DMA 发 A │███████████████████████│      │                       │  CPU 正在渲染 B
         └───────────────────────┘      └───────────────────────┘
   on_color_trans_done 回调 → 交换角色
```

**关键设计：不设共享 framebuffer。**

每个视图实现同一个签名：

```c
/* 把第 band 条（从 y0 起的 rows 行）渲染进 dst；纯函数，无全局状态 */
typedef void (*ui_render_fn)(const ui_view_t *v, uint16_t *dst, int y0, int rows);
```

理由：
* 零 framebuffer ⇒ **没有"两份画布内容不一致"这类竞争**，ping-pong 的语义退化成"两条独立的绘制任务"，最简单。
* 每个视图按行独立计算（频谱曲线、热图取色、3D 柱状投影都是逐行可算的），天然可条带化。
* 重绘一屏 = 10 条 ×（渲染 24 行 + DMA 发送），渲染与 DMA 完全重叠。

实测指标（要打点，不靠猜）：`ui_stats.band_render_us`（每条渲染耗时）、`ui_stats.dma_wait_us`（等 DMA 的耗时）、`ui_stats.fps`。

---

## 4. 并发模型：一所有权、两队列、零共享指针

### 4.1 任务与所有权

| 任务 | 优先级 | 栈 | 独占资源 | 绝不做 |
|---|---|---|---|---|
| **射频/采集**（现有 `burst_serial_serve_idle` 主循环） | 原样 | 原样 | WiFi PHY、ADC、串口 TX | 绝不碰 SPI / I2C / 屏驱动 / UI 状态 |
| **ui_task** | 5 | 4096 | **SPI2 总线全权**、band A/B 缓冲、UI FSM | 绝不调 `spectrum_*`、绝不发串口命令、绝不碰 ADC |
| **touch_task** | 4 | 3072 | **I2C0 总线**（与 RTC 共享时用 `i2c_master` 的 `xSemaphoreTake` 参数互斥） | 绝不碰 SPI、绝不直接改 UI 状态（只发事件） |

**规则一句话**：SPI 只有 `ui_task` 能碰，I2C 只有 `touch_task` 能碰，射频侧两边都不碰。跨任务只走队列。

### 4.2 两个队列

```c
/* ① 数据队列：射频 → UI。元素是"摘要"，不是整帧 */
typedef struct {
    uint32_t seq;          /* 单调递增，UI 掉帧时据此判断新旧 */
    uint16_t center_mhz;
    int16_t  floor_code;   /* 本窗 p10 底噪 */
    int16_t  peak_code;
    uint8_t  bins;         /* 固定 64（屏宽 240 只画 64 根柱够） */
    uint8_t  data[64];     /* 已归一化的 0..255 灰度，射频侧算好再入队 */
} ui_spectrum_msg_t;
QueueHandle_t q_spectrum;  /* 深度 4，满则丢最旧（永远给 UI 最新的一窗） */

/* ② 事件队列：触摸 → UI */
typedef enum { UI_EV_TAP, UI_EV_LONG, UI_EV_SWIPE_LEFT, UI_EV_SWIPE_RIGHT } ui_ev_kind_t;
typedef struct { ui_ev_kind_t kind; uint16_t x, y; } ui_ev_t;
QueueHandle_t q_touch;     /* 深度 8，满则丢新（手滑不该积压） */
```

**为什么数据元素只放 64 个 bin**：屏宽 240，画成柱状最多 240 根；用 64 根柱 + 每柱 3~4 px 已经比 PC 端 256 bin 的曲线更容易读。把归一化放在**射频侧**做，UI 侧只做颜色映射 ⇒ 队列元素 72 B，深度 4 只要 288 B。

### 4.3 明确禁止的三件事（都踩过或极易踩）

1. **禁止在 `on_color_trans_done` 回调里渲染下一条**：那是 ISR 上下文（`esp_lcd` 的完成回调默认在 ISR）。回调里只 `xSemaphoreGiveFromISR` 或 `xQueueSendFromISR`，渲染在任务里做。
2. **禁止让两个任务同时 `esp_lcd_panel_draw_bitmap`**：`ui_task` 是唯一调用者，靠单所有者保证，不靠锁。
3. **禁止 UI 任务调 `scan_fsm_*` 或直接改 `fsm` 结构**：UI 的"设置"要改增益/驻留时，把命令**塞进跟 PC 命令同一个入口**（见 §6），因为它受"只在两次扫描之间才能发"的约束。

---

## 5. UI 状态机

```
        ┌──────┐  任何视图长按(BACK) / 3s 无操作
        │ MENU │◄──────────────────────────────┐
        └──┬───┘                               │
   点圆心  │  点环上某一格                       │
        ┌──▼──────────────┐                    │
        │ VIEW(kind)      │────────────────────┘
        └──┬──────────────┘
           │ 设置手势（长按 > 1.2s）
        ┌──▼──────────┐  确认
        │ SETTINGS    │──┐
        └─────────────┘  │ 取消/超时
                         └→ 回到 VIEW
```

状态用**单一枚举 + 一张转移表**，不用散落的 if：

```c
typedef enum { ST_BOOT, ST_MENU, ST_VIEW, ST_SETTINGS } ui_state_t;
typedef enum { VK_SPECTRUM, VK_WATERFALL, VK_HEAT3D, VK_WIFI, VK_BLE, VK_CAPTURE, VK_SETTINGS, VK_COUNT } ui_view_kind_t;

/* 转移表：state × event → (next_state, action) */
static const ui_tr_t TR[ST_COUNT][UI_EV_COUNT] = { ... };
```

**菜单布局**（圆形 240×240 只有中心 170 直径可用，四角是物理圆外）：

```
        ┌─────────────────┐
        │    ◜ 频谱  ◝     │   ← 6 个入口环状排布，半径 78
        │  热图   3D热图   │      每格 56×44，圆心 90° 分区
        │   WiFi   BLE    │  ← 第二屏：抓档 / 设置
        │      ● 设置     │
        └─────────────────┘
```

点不到就是点不到 —— **每格最小热区 44×44 px**，且只在半径 ≤ 112 的圆内判有效（圆外点击丢弃，不做"最近格"猜测，避免误触）。

---

## 6. 设置项与命令通道（这条最容易搞出竞争）

UI 能改的东西，本质上都是**改射频参数**，而射频参数只能在两次扫描之间改（既有裁决：`GAIN` / 抓档命令不能插进正在进行的扫描）。

所以设计成**单一命令入口**，与 PC 命令平级，不另开通道：

```
PC 串口 ─┐
         ├→ serve 循环（现有 burst_serial_serve_idle）
UI 队列 ─┘        │
                  ├ 若 scan_fsm_running() → 先 FSM STOP，发 FSMEND，改参数，再重启
                  └ 否则直接改
```

UI 侧只做一件事：把 `{cmd, arg}` 塞进 `q_cmd`，然后**等一个结果事件**（`q_cmd_done`，超时 2 s）。它不等回执就不刷新设置页，避免出现"界面显示 61、实际还是 60"。

设置页允许的项（**只读或安全可改**）：

| 项 | 可改 | 生效方式 |
|---|---|---|
| 增益档位 | ✅ 0..90 | `GAIN`，需在扫描间隙 |
| 驻留 ms | ✅ 60..300 | `FSMPLAN` 重建 + `FSM START` |
| 模块开关（5 个） | ✅ | 重建 plan 的 module 标注位 |
| 屏幕亮度 | ✅ | 背光 GPIO11 的 LEDC 占空（本地，立即生效，不碰射频） |
| 屏幕超时熄屏 | ✅ | 本地 |
| 颜色主题 | ✅ | 本地 |

---

## 7. 刷新率预算（先算，再实测）

| 环节 | 估算 | 依据 |
|---|---|---|
| SPI @ 40 MHz 传一帧 | 115,200 × 8 / 40e6 = **23.0 ms** → 43 fps 上限 | GC9A01 支持 40–80 MHz，XIAO 走线取保守 40 |
| 渲染一屏（10 条） | 未知，先打点 | 热图 57,600 像素取色，定点大约 1–2 ms |
| **目标** | **20 fps 稳定**（50 ms/帧） | 频谱曲线在 PC 端也是 20 fps；屏上不需要更快 |

**与 PC 并存**：屏是设备侧本地显示，不占串口。唯一共享的是射频时间片 —— 屏的 UI 任务只在**扫描间隙**（`scan_fsm_idle` 让出 CPU 时）跑，因为 FreeRTOS 优先级：UI(5) > 采集循环(默认)意味着**屏刷新会抢射频**。所以：

* 采集循环所在任务的优先级必须 **≥ UI 任务**（实测后定；必要时 UI 降到 2 并接受 10 fps）；
* 或者 UI 任务每渲染一条 `vTaskDelay(1)` 主动让路，代价是最多掉到 ~30 fps。

这条**必须实测**，不能拍脑袋 —— 加一个对照实验：屏开/屏关各跑一次 `sweep_real.py`，比窗率。

---

## 8. 分阶段（每阶段独立可验收）

| 阶段 | 内容 | 验收判据 |
|---|---|---|
| **8.1** | 关 UART 桥；GC9A01 初始化 + 背光；画三色条 + 十字线 | 屏上真的出现色条；串口 PC 侧仍能正常 `SPEC` 扫频（证明关 UART 无副作用）；上电 20 次都正常 |
| **8.2** | 触摸驱动 + 菜单状态机 + 6 格环状菜单 | 点每格都能进对应视图并返回；圆外点击无反应；`ui_stats.tap_ok/tap_drop` 计数正确 |
| **8.3** | 频谱曲线视图 + 瀑布热图视图（条带 ping-pong） | 20 fps 稳定；`dma_wait_us` < 渲染耗时（说明重叠成功）；PC 侧窗率下降 < 10% |
| **8.4** | 3D 热图视图 + WiFi/BLE 结论页 | 3D 投影逐行算，不掉帧 |
| **8.5** | 并发压测 + 与 PC 并存 + 长时间浸泡 | 30 min 无死机、无丢帧尖峰、堆稳定不泄漏 |

---

## 9. 待人类确认（阻塞 8.1）

1. **屏是不是插在这块 ESP32-C5 上**（XIAO 座直插）？如果接在另一块板（如 XIAO ESP32S3）上而是靠串口拿数据，整个方案要重写。
2. **UART 桥可以关吗**？关掉后 `CONFIG_ESP_SDR_UART_*` 那套（可能被别人用）失效。PC 侧只走 USB，我认为可以，但这动的是既有对外能力，要你拍板。
3. **菜单要哪几项**？我按 6 格排了 `频谱 / 热图 / 3D热图 / WiFi / BLE / 设置`，抓档没排进去（要等 IQ 传输，屏上没意义）。你觉得要改就现在说。

---

## 10. 风险清单

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | GPIO7（DC）是 strapping，上电被屏模块拉偏 | 可能进不了 bootloader / 起不来 | 实测 20 次；失败则飞线换 DC |
| R2 | 关掉 UART 桥后有人依赖它 | 既有能力回退 | 8.1 验收里加「PC 侧全功能回归」 |
| R3 | UI 任务抢射频时间片 | 窗率下降 = 数据变慢 | 优先级对比实验 + `vTaskDelay(1)` 让路 |
| R4 | SPI DMA 与 PHY ADC 争带宽 | 采集偶发失败 | 屏刷新限速 20 fps；采集错误计数进 `ui_stats` 对照 |
| R5 | 屏占 23 KB + 触摸任务栈，堆吃紧 | malloc 失败 | 8.1 起每阶段打印 `heap_caps_get_free_size(MALLOC_CAP_DMA)` |
| R6 | CHSC6X 中断是电平低有效，抖动 | 误触/连点 | 读两次 + 20 ms 去抖 + 44×44 最小热区 |

---

## 11. 不做的事（划清边界）

* 不在屏上做 IQ 抓档（要传大块数据，屏没有意义）。
* 不做中文显示（内置点阵字体只有 ASCII；中文要几千字字库，C5 的 flash 只有 4 MB 且要留给固件）。
* 不替代 PC 上位机：屏是**现场速览**，详细分析仍在 PC。
* 不引入 LVGL / Arduino 运行时。