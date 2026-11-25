#pragma once
/* 圆屏本地面板（ESP32-C5 + Seeed Round Display GC9A01 240x240 + CHSC6X 触摸）。

 * 接入真实调度：屏**不占** `burst_serial_serve_idle` 的 idle 槽位（那里已被
 * `scan_fsm_idle` 独占，且 scan_fsm_idle 里采一窗是阻塞的 ~60 ms），而是起一个
 * 与 app_main 同优先级的独立任务，靠 FreeRTOS 时间片 + 主循环 `vTaskDelay(1)`
 * 的空隙推进。这样做的好处：
 *   - 屏刷新再慢也不会挡住射频采集（射频优先级不降）
 *   - 不需要在射频路径里加任何锁
 * 代价：串口吞吐会被时间片切分。实测判据见 `RD?` 的 `frames` / `fps`。

 * 并发模型（三条硬规则）：
 *   1) SPI 总线 + GC9A01 只由 `ui_task` 触碰；射频侧永不碰
 *   2) I2C 总线 + CHSC6X 也只由 `ui_task` 触碰（同任务串行读，省一条队列）
 *   3) 跨任务只走一条队列 `q_cmd`（app_main → ui_task）和一份临界区保护的统计快照；
 *      绝不跨任务读对方正在改的结构体
 */
#include <stdbool.h>
#include <stdint.h>

/* UI 状态机（单枚举 + 转移表，见 round_ui.c 的 TR[]） */
typedef enum {
    ROUND_UI_BOOT = 0,   /* 驱动初始化中 / 重配置中 */
    ROUND_UI_LIVE,       /* 正常刷新 */
    ROUND_UI_ERROR,      /* 初始化失败或运行中致命错误 */
} round_ui_state_t;

/* 统计快照：ui_task 单写，读侧走临界区拷贝（见 round_ui_snapshot） */
typedef struct {
    uint32_t frames;        /* 累计整屏帧数 */
    uint32_t fps_x10;       /* 最近窗口 fps × 10（整数，避免跨任务浮点） */
    uint32_t pclk_hz;       /* 当前 SPI 时钟 */
    uint32_t render_us;     /* 最近窗口单帧渲染 CPU 时间 */
    uint32_t init_errors;   /* 初始化失败次数 */
    uint32_t submit_errors; /* 提交失败次数 */
    uint16_t band_rows;     /* 条带行数（24 = 11,520 B/块） */
    uint16_t touch_x;
    uint16_t touch_y;
    uint8_t state;          /* round_ui_state_t */
    uint8_t touch_down;
    uint8_t ready;          /* 1 = 驱动已就绪 */
    uint16_t heat_cells;    /* 热力图非零格数（0..352） */
    uint8_t heat_peak;      /* 当前最大热力值（0..255） */
    uint8_t view;           /* 当前视图 0=ring 1=chart 2=spec 3=wfall */
    int8_t touch_int_lvl;   /* 触摸 INT 引脚当前电平；-1 = 没读 */
    uint8_t touch_i2c_cnt;     /* 启动扫描：I2C 总线应答设备数 */
    uint8_t touch_chsc6x;      /* 启动扫描：CHSC6X(0x2E) 是否应答 */
    uint8_t touch_first_addr;  /* 启动扫描：首个应答地址（0=无） */
} round_ui_stats_t;

/* 可选钩子：由 family 注册「静默固定增益」（不应答串口）。
 * 圆屏自扫必须固定增益：硬件 AGC 会把每个信道都拉到相近电平，占用率读不出差别。
 * 没注册就跳过 —— 屏照常工作，只是占用读数会被 AGC 摊平。 */
typedef void (*round_ui_gain_fn)(unsigned index);
void round_ui_set_gain_hook(round_ui_gain_fn fn);

/* 创建 ui_task。必须在 `burst_serial_serve_idle()`（永不返回）之前调用一次。 */
void round_ui_start(void);

/* 挂进 handle_command。返回 true 表示这行是本模块的命令（已应答）。支持：
 *   RD?                              状态一行
 *   RD RATE <mhz>                    改 SPI 时钟（20/40/60/80），异步重建
 *   RD BAND <rows>                   改条带行数（8/16/24/48/120/240）
 *   RD OFF / RD ON                   停 / 恢复刷新（用于对照吞吐）
 *   RD CLEAR                         清空热力图记忆（全表归零）
 *   RD VIEW <0..3>                   切换视图：0 环带热力 / 1 信道榜 / 2 实时频谱 / 3 2D 瀑布
 *   RD VIEW                          回显当前视图
 */
bool round_ui_command(char *line);

/* 临界区拷贝一份统计快照（调用方给结构体） */
void round_ui_snapshot(round_ui_stats_t *out);

/* 清空热力图记忆（只由 ui_task 在命令分支里调；测试也可直接调） */
void round_ui_clear_heat(void);