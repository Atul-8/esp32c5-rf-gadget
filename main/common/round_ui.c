/* 圆屏本地面板：ui_task 实现（ESP32-C5 + GC9A01 + CHSC6X）。
 *
 * 为什么不塞进 burst_serial_serve_idle 的 idle 槽位：
 *   1) 那个槽位已被 scan_fsm_idle 独占（签名只收一个回调）
 *   2) scan_fsm_idle 里 ops->frame() 采一窗是阻塞的（60 ms 驻留），期间任何
 *      idle 内渲染都会断流
 *   3) idle 返回 true 会跳过 vTaskDelay(1)，主循环忙转会抢串口吞吐
 * 所以起独立任务，与 app_main 同优先级（1），靠 FreeRTOS 时间片推进：
 * 射频采帧阻塞期间 tick 中断照样把 CPU 切给 ui_task，射频帧率不受影响。
 *
 * 并发三条硬规则（写代码时不许破）：
 *   1) SPI 总线 + GC9A01 只由 ui_task 触碰；射频侧永不碰
 *   2) I2C 总线 + CHSC6X 只由 ui_task 触碰（同任务串行，省一条队列）
 *   3) 跨任务只有两处：q_cmd（app_main → ui_task）和 s_stats + 临界区
 */
#include "round_ui.h"

#include <stdio.h>
#include <string.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "driver/gpio.h"

#include "dsh_display.h"
#include "dsh_touch.h"
#include "scan_fsm.h"
#include "burst_serial.h"
#include "spectrum.h"
#include "rd_font.h"
#include "rd_font2.h"
#include "rd_font3.h"
#include "rd_pal.h"

/* ---- 引脚：XIAO ESP32-C5（D 编号 != GPIO 编号）----
 * 权威：micropython SEEED_XIAO_ESP32C5/pins.csv + arduino-esp32 variants/XIAO_ESP32C5/pins_arduino.h
 *   D1=GPIO0 CS | D3=GPIO7 DC(strapping) | D6=GPIO11 BL | D7=GPIO12 TOUCH_INT
 *   D8=GPIO8 SCK | D9=GPIO9 MISO | D10=GPIO10 MOSI | D4=GPIO23 SDA | D5=GPIO24 SCL
 * 注意 GPIO11/12 正是 CONFIG_ESP_SDR_UART 的默认 TX/RX → 必须关掉 UART 桥
 * （sdkconfig.defaults.esp32c5 已置 CONFIG_ESP_SDR_UART_ENABLED=n） */
#define RD_SPI_HOST   1     /* SPI2_HOST */
#define RD_SCK        8
#define RD_MOSI      10
#define RD_MISO       9
#define RD_CS         0
#define RD_DC         7
#define RD_BL        11
#define RD_TOUCH_INT 12
#define RD_TOUCH_SDA 23
#define RD_TOUCH_SCL 24
#define RD_LED_GPIO  27

#define RD_W 240
#define RD_H 240

/* ---- 半圆频谱热力图的几何常量 ----
 * 分段收拢 DFS 空档、段内线性：83.5 MHz → 44 格（2.4G）、100 MHz → 53 格（UNII-1）、
 * 100+255 MHz → 53+136 格（UNII-2A/2C，5350~5470 的 DFS 空档零弧宽收拢）、
 * 125 MHz → 66 格（UNII-3），每格 1.88~1.90 MHz，弧上每格的物理宽度是均匀的。 */
#define RD_GRID          352
#define RD_SEG_24G        44
#define RD_SEG_U1         53   /* UNII-1   5150~5250 */
#define RD_SEG_U2A        53   /* UNII-2A  5250~5350 */
#define RD_SEG_U2C       136   /* UNII-2C  5470~5725 */
#define RD_SEG_U3         66   /* UNII-3   5725~5850 */
#define RD_R_OUT         116     /* 外框圈 */
#define RD_HEAT_HI       108     /* 热力环外径 */
#define RD_HEAT_LO        88     /* 热力环内径：22 px 厚 */
#define RD_TICK_LO       110     /* 主/次刻度内端 */
#define RD_TICK_HI       115     /* 刻度外端（顶到外框内侧） */
#define RD_R_CORE         74     /* 中心功能区可用半径（下一步放三按键） */
#define RD_TICK_STEP      22     /* 每 22 格一根次刻度（22.5°） */
#define RD_TICK_MAJOR     88     /* 每 88 格一根主刻度（90°） */
#define RD_BLOCKS        176     /* 热力环块数 = RD_GRID / 2 */
/* 热力值到多少就画满整个环带。
 * v 是「连续扫到多少次」。实测设备一轮约 2.6 s、每格每轮约 +1，
 * 即约 3 格值/秒。
 *
 * 旧值 32 太小：开机十几秒就整圈饱和成同一个最亮色，强度梯度全丢；
 * 实测 20 秒 peak 已 84。改成 96 后约 30 秒到满，梯度能持续可读。
 * heat 值本身仍是 0..255（+1 封顶 255、-1 触底 0，用户指定的算法），
 * 这里只决定「多少个 heat 单位算满格」。 */
#define RD_HEAT_FULL     96
/* 瀑布历史行的归一值下限：底噪色，避免纯黑断带（弱信号仍是暗的）。 */

/* 视图 1 用的信道表：2.4G CH1..CH13（5 MHz 步进）+ 5G 常用 25 个。 */
static const uint16_t s_ch_mhz[] = {
    2412, 2417, 2422, 2427, 2432, 2437, 2442, 2447, 2452, 2457, 2462, 2467, 2472,
    5180, 5200, 5220, 5240, 5260, 5280, 5300, 5320,
    5500, 5520, 5540, 5560, 5580, 5600, 5620, 5640, 5660, 5680, 5700, 5720,
    5745, 5765, 5785, 5805, 5825,
};
#define RD_NCH     ((int)(sizeof(s_ch_mhz) / sizeof(s_ch_mhz[0])))

/* 自扫信道表（原先写死在 rd_auto_scan() 里）：**扫描层与显示层共用的一张表**。
 * 提到模块作用域的唯一理由是：显示层必须知道"这一行到底会不会被轮询"，
 * 否则 8 个从未纳入自扫的 2.4G 信道（2417/2422/2432/...）会永远显示 0%，
 * 看起来像"真的没占用"，而不是"没测"。
 * 扫描行为一个字节没改：rd_auto_scan() 仍按这张表逐条 FSMPLAN ADD。 */
static const uint16_t s_scan_ch[] = {
    2412, 2427, 2442, 2457, 2472,
    5180, 5200, 5220, 5240, 5260, 5280, 5300, 5320,
    5500, 5520, 5540, 5560, 5580, 5600, 5620, 5640, 5660, 5680, 5700, 5720,
    5745, 5765, 5785, 5805, 5825
};
#define RD_NSCAN ((int)(sizeof(s_scan_ch) / sizeof(s_scan_ch[0])))

/* 页 3（WATERFALL）的前提：纵轴＝时间、横轴＝**同一个频窗**内的频率。
 * 自扫是 60 ms 换一个窗，混进来就不是瀑布（实测每 3 帧才留一个窗，
 * 一轮 30 个信道在屏上只有约 10 行，横轴印的 lo/hi 还只是"当前窗"）。
 * 所以：进页 3 → 把扫描钉死在 s_scan_ch[s_wf_ch] 这一个信道上；
 *       左右滑动 → 换信道（换窗必须清历史）；
 *       离开页 3 → 恢复整表自扫。 */
static int s_wf_ch;            /* s_scan_ch[] 下标，页 3 钉住的信道 */
static bool s_wf_pinned;       /* 当前是否处于"钉住"状态（供渲染判读） */

/* 页 3 的翻页指示条（宏 RD_WF_BAR_*）已删除：用户 2026-11-25 拍板
 * 「第三页不要显示右栏页码条」——钉住的信道由下方 PIN 文字行完整表达，
 * 竖条冗余且视觉干扰。换信道手势仍是上下滑动（上推 = 后一个 = 频率更高）。
 */

/* 频段色环 / 圆心频段名用的五段名字与颜色（字库只有 ASCII → 2.4G + FCC 四块）。
 * 5G 部分按 FCC 法规术语（47 CFR 15.407）拆成**四个色块**：UNII-1 / UNII-2A /
 * UNII-2C / UNII-3，2A/2C 拆开异色（用户 2026-11-25）；5350~5470 DFS 空档仍零弧宽
 * 收拢。名字存纯法规名；**显示**时按用户 2026-11-25 要求加 5G- 前缀
 * （现场一眼看不出 UNII 是 5G 的段），且频段名已从圆心行拆出单独居中：
 * 2.4G 段写 "2.4G"、UNII 段写 "5G-UNII-x"（最长 "5G-UNII-2A" 10 字符 110 px，
 * y=112 弦宽 146 px 放得下；组合式 "MHZ 5G-UNII-1" 13 字符 = 142 px 会擦圆边，
 * 弃用）。2C 色 0xB060 暗橙红：与 2A 青绿 0xA17E、U3 黄绿 0xAB40 异色，
 * dim 后高字节 0x50/0x58/0x55 仍可精确区分。
 * MHz 单位另画在频率数字上方 y=62（2026-11-25 第二轮要求，见圆心行②）。
 * 纯显示常量，不参与任何测量或判决。 */
static const char *const s_band_name[5] = { "2.4G", "UNII-1", "UNII-2A", "UNII-2C", "UNII-3" };
static const uint16_t s_band_col[5] = { 0x2F10, 0x453D, 0xA17E, 0xB060, 0xAB40 };

/* 显示层：当前窗中心落在哪个大区。分界与环上五段弧一致
 * （2.4G 2400~2483.5 / UNII-1 5150~5250 / UNII-2A 5250~5350 /
 *  UNII-2C 5470~5725，5350~5470 DFS 空档收拢归 2C 侧 / UNII-3 5725~5850）。
 * 只读 s_win_mhz，不做任何测量。 */
static int band_of_mhz(uint16_t mhz)
{
    if (mhz < 3000) return 0;
    if (mhz < 5250) return 1;
    if (mhz < 5350) return 2;
    if (mhz < 5725) return 3;
    return 4;
}

/* RGB565 整体折半：非当前频段用暗色，一眼看出"现在扫的是哪一段"。 */
static inline uint16_t rd_dim(uint16_t c) { return (uint16_t)((c >> 1) & 0x7BEF); }

/* 显示层用：该信道是否在自扫计划里。纯查表，不参与任何测量或判决。 */
static bool ch_polled(uint16_t mhz)
{
    for (int i = 0; i < RD_NSCAN; i++) {
        if (s_scan_ch[i] == mhz) return true;
    }
    return false;
}
/* 屏幕上"点一下就换视图"的左侧条宽度。圆屏 x<92 在 y∈[80,160] 内都还在
 * 可视圆内，手指按得稳，又不会挡住中部的数据区。 */
#define RD_SWITCH_ZONE_W 92
/* CHART 每页显示的信道数。37 个信道 / 6 = 7 页，用上下滑动翻阅。 */
#define RD_CH_PER_PAGE 5          /* 6 -> 5：腾出表头(y=50)与页脚(y=172) */
/* RD ROW 回读粒度：10x18 正文的笔画只有 1~2 px 宽，旧的每 8 px 采一点
 * 会整段漏掉（'C' 的竖笔在 x=19,20，采样点落在 16/24 上就是全 0）。
 * 收到 4 px 才能在固件侧无歧义地重建字形，不依赖摄像头对焦。 */
#define RD_ROW_STEP 4
#define RD_ROW_N    (RD_W / RD_ROW_STEP)
#define RD_NCH_MAX 40
#define RD_SPEC_N  64
/* ---- 圆心「占用」口径 ----------------------------------------------------
 * 用户的需求是「比较好描述当前信道占用情况」，明确不要强弱判断。
 * 定稿口径 = **时间占空比**：本信道最近若干个采集帧里，有多大比例被判为
 * 「有传输」。0~100%，不看信号多粗，也不跟着增益跑。
 *
 * 单帧判据：帧内 (中位 − 25%分位) 超过 15 码。
 *  ① 不能用绝对电平：真板实测接收机会整带跳变（阻带 bin 从 1~5 码抬到
 *     38~47 码），同一信道几十秒内 p50 从 20 跳到 80，绝对门限全稳不住。
 *  ② 不能用全窗直方图：设备自扫是 512 bins @ 40 MS/s，实测 17% 的 bin 是
 *     模拟滤波器外的零码，25% 分位被拉到 0，帧内差值恒超门限（真板跑出
 *     88~100% 的荒谬值）。所以只看窗内**中间一半**（±10 MHz，所有采样率
 *     下都是有效通带）。
 *  ③ 跳变帧也会抬高中间一半，但抬高量比阻带小，d 受影响有限；真正的
 *     稳定性靠「多次访问的 EMA」而不是单次判决。
 *
 * 实测对照（主机侧同配置 40 MS/s/512bin，门限 15、只看中间一半）：
 *   5745 (空) 0.9%  5500 2.6%  2437 (有 AP) 16.2%  2442 区段 29~32%
 */
#define RD_OCC_LIGHT 5      /* ≥ 5%：有人在用 */
#define RD_OCC_CROWD 15     /* ≥ 15%：相当挤 */
#define RD_OCC_HEAVY 35     /* ≥ 35%：重度占用 */

#define RD_WF_N    64

#define RD_DEFAULT_PCLK_HZ   20000000   /* 20 MHz 实测稳定（fps≈21，合乎上位机 ≤20 fps 约定）。
                                          * 40 MHz 曾用过但未验证是否丢像素，在没有明确收益前
                                          * 先用已验证的值。 */
#define RD_DEFAULT_BAND_ROWS 24     /* 2 x 11,520 B ping-pong */

/* ---- 任务/队列配置 ---- */
#define RD_TASK_STACK   4096
#define RD_TASK_PRIO    1           /* = app_main 优先级 → 时间片轮转 */
#define RD_CMD_DEPTH    4
#define RD_TOUCH_PERIOD_US 30000    /* 触摸轮询间隔 */
#define RD_TOUCH_RETRY_US 2000000   /* 触摸掉线后的重探间隔 */

typedef enum {
    RD_CMD_RATE = 1,
    RD_CMD_BAND,
    RD_CMD_ON,
    RD_CMD_OFF,
    RD_CMD_CLEAR,
    RD_CMD_VIEW,
    RD_CMD_FILL,
    RD_CMD_PAGE,
    RD_CMD_KNOWN,
    RD_CMD_WFCH,
    RD_CMD_VIEWSEC,     /* arg = view<<8 | sec，设每页轮播秒数 */
} rd_cmd_type_t;

typedef struct {
    uint8_t type;
    uint32_t arg;
} rd_cmd_t;

/* ---- 状态机：单枚举 + 转移表 ---- */
typedef enum {
    RD_EV_INIT_OK = 0,
    RD_EV_INIT_FAIL,
    RD_EV_REBUILD,
    RD_EV_FAULT,
    RD_EV_COUNT
} rd_event_t;

/*        INIT_OK       INIT_FAIL     REBUILD        FAULT       */
static const uint8_t RD_TR[3][RD_EV_COUNT] = {
    /* BOOT  */ { ROUND_UI_LIVE, ROUND_UI_ERROR, ROUND_UI_BOOT,  ROUND_UI_ERROR },
    /* LIVE  */ { ROUND_UI_LIVE, ROUND_UI_ERROR, ROUND_UI_BOOT,  ROUND_UI_ERROR },
    /* ERROR */ { ROUND_UI_LIVE, ROUND_UI_ERROR, ROUND_UI_BOOT,  ROUND_UI_ERROR },
};

/* ---- 单写者状态（只由 ui_task 触碰） ---- */
static dsh_display_t *s_disp;
static QueueHandle_t s_q_cmd;
static uint32_t s_pclk_hz = RD_DEFAULT_PCLK_HZ;
static uint16_t s_band_rows = RD_DEFAULT_BAND_ROWS;
static uint32_t s_frame_no;
static uint16_t s_pen_x, s_pen_y;
static bool s_pen_down;
static bool s_enabled = true;
static TaskHandle_t s_task;      /* ui_task 句柄，只为读栈高水位 */
static volatile int8_t s_pen_int_lvl = -1;   /* 触摸 INT 引脚电平快照 */
static volatile uint8_t s_touch_i2c_cnt = 0;     /* 启动扫描：总线上应答设备数 */
static volatile uint8_t s_touch_chsc6x = 0;      /* 启动扫描：0x2E 是否应答 */
static volatile uint8_t s_touch_first_addr = 0;  /* 启动扫描：首个应答地址 */
static uint16_t s_tap_x, s_tap_y;   /* 本次按下的起点（抬手时用来判断滑动/点击） */
static uint8_t s_tap_active;
static int64_t s_touch_retry_t = 0;              /* 上次重探时刻 */
static uint16_t s_fill_color;
/* 诊断：render_band 最近一次实际写进 buffer 的前 8 字节 */
static uint8_t s_dbg_buf[8];
static uint16_t s_dbg_fill;
/* 热力环渲染诊断：记录的 pmax / 首个非零块及其长度、颜色 */
static uint8_t s_dbg_pmax;
static uint8_t s_dbg_b;
static uint8_t s_dbg_len;
static uint16_t s_dbg_col;
static uint32_t s_dbg_n;
static bool s_ready;   /* 驱动已就绪（只由 ui_task 写） */
static uint8_t s_heat[RD_GRID];            /* 时间累积：该频点多久没断过 */
/* 全频段幅度峰值表：记录每个格子上"曾经见过的最大功率码"。
 * s_heat 是时间累积（每秒 +1，封顶 255），在全频段滚动扫描下几乎每个格子
 * 都会涨到饱和，用它算"信道占用率"会让所有信道一起顶格、毫无区分度。
 * CHART 要的是信号强弱，所以单独记一张幅度峰值表。 */
static uint8_t s_lvl[RD_GRID];
/* RING 环带上色专用电平：与即时功率码取 max、按 1/64 慢衰减（半衰约 9 秒 ≈ 4 轮扫描）。
 * 为什么不能直接用 s_lvl：它每帧衰减 3%（半衰 4.5 秒），扫一轮 38 个频点要 2.3 秒，
 * 轮到某格之前它的值已掉到峰值 ~25%，环带因此整圈落在调色板冷端 = 一片蓝。
 * 为什么也不能用 s_heat：那是 +1/-1 累计量，跑久后每格都停在 1~30，本身没有层次。
 * 只用于上色，不影响任何判决口径。 */
static uint8_t s_ring_lvl[RD_GRID];
/* Peak Hold：每个 bin 见过的最大值。专业频谱仪的标准读数方式——
 * "当前值"在 21 fps 下抖动看不清，"保持值"才能看出到底有没有稳定信号。
 * SPEC 视图用它和实时柱叠加：彩色柱 = 当前，白线 = 保持。 */
static uint8_t s_peak[RD_SPEC_N];
static uint8_t s_dbg_noise;     /* 25% 分位底噪，供圆心读数显示 */
/* 诊断：最近一次结算的原始量（不做任何平滑），用来核对圆心口径与主机侧实测
 * 是不是同一份数据。rrate 是窗带宽（Hz），rbins 是帧内 bin 数。
 * 这几个字段本身不上屏，只走 RD STATS?，定位完可以删。 */
static uint8_t s_dbg_p50_raw;
static uint16_t s_dbg_bins;
static uint32_t s_dbg_rate;
static unsigned s_dbg_nz;
static uint8_t s_dbg_row[512];
static uint16_t s_dbg_hist[256];
/* 每格「占用率」百分比（0..100）：最近若干轮采集里被判为「有传输」的帧占多少。
 * 与 s_lvl/s_heat 并列的第三种量：
 *   s_lvl  = 峰值电平（信号多强，增益一变就全变）
 *   s_heat = 连续扫到几次（多久没断过，慢）
 *   s_occ  = 时间占空比（多长时间是有传输的）→ 圆心读数
 * 更新方式是每次访问该格时 α=1/8 的 EMA（一轮 ≈ 3 s → τ ≈ 24 s）：
 * 每窗只采 8 帧（spectrum.c 的 OCC_FRAMES），单次比例直接上屏会跳。 */
static uint8_t s_occ[RD_GRID];
static uint16_t s_win_mhz;                 /* 当前窗口中心频率 */
static uint32_t s_win_rate;                /* 当前窗口带宽 */          /* 一格一字节：0 = 冷，255 = 饱和 */

/* ---- 跨任务统计：ui_task 写，临界区读 ---- */
static round_ui_stats_t s_stats;
static portMUX_TYPE s_stats_lock = portMUX_INITIALIZER_UNLOCKED;

static void stats_publish(uint8_t state, uint32_t fps_x10, uint32_t render_us,
                          uint32_t init_err, uint32_t sub_err)
{
    uint16_t cells = 0;
    uint8_t peak = 0;
    for (int g = 0; g < RD_GRID; g++) {
        if (s_heat[g]) {
            cells++;
            if (s_heat[g] > peak) peak = s_heat[g];
        }
    }
    taskENTER_CRITICAL(&s_stats_lock);
    s_stats.frames = s_frame_no;
    s_stats.fps_x10 = fps_x10;
    s_stats.pclk_hz = s_pclk_hz;
    s_stats.render_us = render_us;
    s_stats.band_rows = s_band_rows;
    s_stats.state = state;
    s_stats.ready = s_ready ? 1 : 0;
    s_stats.init_errors = init_err;
    s_stats.submit_errors = sub_err;
    s_stats.touch_x = s_pen_x;
    s_stats.touch_y = s_pen_y;
    s_stats.touch_down = s_pen_down ? 1 : 0;
    s_stats.touch_int_lvl = s_pen_int_lvl;
    s_stats.touch_i2c_cnt = s_touch_i2c_cnt;
    s_stats.touch_chsc6x = s_touch_chsc6x;
    s_stats.touch_first_addr = s_touch_first_addr;
    s_stats.heat_cells = cells;
    s_stats.heat_peak = peak;
    taskEXIT_CRITICAL(&s_stats_lock);
}

void round_ui_snapshot(round_ui_stats_t *out)
{
    if (!out) {
        return;
    }
    taskENTER_CRITICAL(&s_stats_lock);
    *out = s_stats;
    taskEXIT_CRITICAL(&s_stats_lock);
}

static void rd_reply(const char *s)
{
    if (!s || !*s) {
        return;
    }
    /* 必须保证每条回复都以换行结尾。上位机用 readline() 读应答，
     * 没有行尾它会一直等串口超时（5 s）；更要命的是下一条回复会直接
     * 拼到这条尾巴上，命令与响应从此永久错位。
     * 分两段发而不是拼进临时缓冲：RD ROW 一帧就有 200 多个字符，
     * 拼缓冲反而要赌长度，分段发零拷贝零截断风险。 */
    size_t n = strlen(s);
    (void)burst_serial_send(s, n);
    if (s[n - 1] != '\n') {
        (void)burst_serial_send("\n", 1);
    }
}

/* ================= 半圆频谱热力图（贴边沿的弧形条带） =================
 *
 * 形态：上半圆一个环带，角度 = 频率，径向长度 = 热力值，颜色 = 热力色标。
 * 数据源：spectrum_last_row()（只能拿**最近一帧**，即当前扫到的那一个 20 MHz 窗）。
 *         窗外的格保持旧值 —— 这一带就是「记忆」：扫到的格往上长，整轮回到起点时
 *         全表减一，长时间没人用的频段自己褪色，不占任何历史存储。
 *
 * 为什么按频段分段弧长而不是按频率线性铺：2.4G 只有 83.5 MHz，5G 全段有 700 MHz
 * （UNII-1/2A/2C/3），线性铺的话 2.4G 会挤成不到 5 度的细线，什么都看不见。分段、
 * 段内线性，和上位机瀑布图（三频段等宽并排）用同一套口径。
 *
 * 每格频率宽：83.5/44 = 1.90（2.4G）、100/53 = 1.89（UNII-1）、100/53 = 1.89（2A）、
 * 255/136 = 1.88（2C）、125/66 = 1.89（UNII-3）MHz —— 各段基本一致，所以弧上
 * 每一格的物理宽度是均匀的，不是视觉上随意拉伸。 */

/*: Iceburn 色标 32 档（v=1..32 直接做下标，0 号位占位不用）。
 * 为什么弃掉原 jet 彩虹 8 档：实测最强档 L*=53 比中档 L*=91 暗 60%，
 * 信号越强反而越看不见。Iceburn 的 L* 从 19 单调升到 93，无双峰无反转，
 * 且亮度主要由 G 通道承担（G 是 6 bit，台阶最细）。
 * 全 const → 落 flash，BSS 增量 0。 */

static unsigned s_heat_last_mhz;         /* 上一帧频点，用来检测「整轮回到起点」 */
static bool s_heat_valid;
/*: heat_absorb 的工作区。摆成 static 而不是栈上：任务栈只有 4096 B，
 * 一个 512 B 行缓冲 + 1 KB 直方图会把余量吃掉一半。反正只有 ui_task 碰这两个。 */
static uint8_t s_row[512];
static uint16_t s_hist[256];

/* 直方图求分位数：num/den 分位（如 1/4 = 25% 分位）。256 桶刚好覆盖功率码。 */
static uint8_t hist_quantile(const uint16_t *hist, unsigned bins, unsigned num, unsigned den)
{
    uint32_t target = (uint32_t)((uint64_t)bins * num / den);
    if (!target) target = 1;
    uint32_t acc = 0;
    for (unsigned v = 0; v < 256; v++) {
        acc += hist[v];
        if (acc >= target) return (uint8_t)v;
    }
    return 255;
}

/* 频率（Hz）→ 格号；不在任何一段里返回 -1。UNII-2A/2C 两块之间 5350~5470 的
 * DFS 空档维持零弧宽收拢（5350 与 5470 在环上相邻）。 */
static int freq_to_grid(uint64_t f_hz)
{
    if (f_hz >= 2400000000ull && f_hz <= 2483500000ull) {
        return (int)((f_hz - 2400000000ull) * RD_SEG_24G / 83500000ull);
    }
    if (f_hz >= 5150000000ull && f_hz <= 5250000000ull) {
        return RD_SEG_24G + (int)((f_hz - 5150000000ull) * RD_SEG_U1 / 100000000ull);
    }
    if (f_hz >= 5250000000ull && f_hz <= 5350000000ull) {
        return RD_SEG_24G + RD_SEG_U1
             + (int)((f_hz - 5250000000ull) * RD_SEG_U2A / 100000000ull);
    }
    if (f_hz >= 5470000000ull && f_hz <= 5725000000ull) {
        return RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A
             + (int)((f_hz - 5470000000ull) * RD_SEG_U2C / 255000000ull);
    }
    if (f_hz >= 5725000000ull && f_hz <= 5850000000ull) {
        return RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A + RD_SEG_U2C
             + (int)((f_hz - 5725000000ull) * RD_SEG_U3 / 125000000ull);
    }
    return -1;
}

/* 四个视图各占 Turbo 里的一段色相，既能满屏全彩，又让每一页有自己的
 * 主色调便于一眼分辨：
 *   RING  0    深紫->蓝->青->绿
 *   CHART 40   青->绿->黄
 *   SPEC  90   绿->黄->橙
 *   WFALL 150  黄->橙->红->紫红
 * gain 用来把集中在窄区间的实测值展开——环境噪声大多落在 40~70，
 * 不展开的话整屏只有青绿一带，看起来仍然"没色彩"。 */
static uint16_t view_color(uint8_t v, uint8_t hue, uint8_t gain)
{
    uint32_t x = (uint32_t)v * gain;
    if (x > 255u) x = 255u;
    return rd_pal256[(uint8_t)(x + hue)];
}

/* 热力值 → 颜色。0..RD_HEAT_FULL 线性归一化到 32 档 Iceburn 表。
 *
 * 必须归一化，不能拿 v 直接当下标：v 能涨到 96，而表只有 33 项，
 * 直接索引会越界读到表尾之外。v=0 由调用方跳过（不画，保留环底）。 */

/* 吸收最近一帧：高于本窗底噪的 bin 让对应格 +1（每次最多 +1，封顶 255）。
 * 「扫到 +1 / 扫不到 -1」里的 -1 放在**整轮边界**上做（检测到频点回退），
 * 否则 38 帧/秒的喂入速度会让热力图几秒就糊成一片，记忆就失去意义了。
 *
 * 两个曾踩过的坑（都在真板上实测出来）：
 *  ① 512 个 bin 只盖大概 10 格，逐 bin 直接 ``heat[g]++`` 会让一格一次加几十，
 *     30 秒就冲到 255。所以先收集「本帧命中的格」（最多十几格），再去重加一。
 *  ② 底噪用 ``min`` 时，噪声里一个凹谷就把门限拉到很低，几乎每个 bin 都算「有信号」。
 *     改用 25% 分位（直方图求，256 桶，比排序 512 项便宜且无额外分配）。 */
static void heat_absorb(void)
{
    uint8_t *row = s_row;
    unsigned bins = 0, mhz = 0, rate = 0;
    if (!spectrum_last_row(row, sizeof(s_row), &bins, &mhz, &rate) || bins < 2 || !rate) {
        return;
    }
    /* 同一窗会被设备连续上报很多帧（60 ms 驻留 ≈ 十几帧）。只结算一次，
     * 否则一格几秒就冲满 255，「记忆」就退化成实时图了。
     *
     * 例外：单信道计划（调试用的 FSMPLAN 只放一个频点）下 mhz 永远等于上次，
     * 光看频点会永久早退，`s_occ` 就冻在第一帧的值上（实测单信道预热 30 s
     * 板端仍报 0~1%，看着像估计器坏了）。所以先 take 一次占用统计，
     * 「这次 take 到了新批次」也算新窗，不能被频点早退挡掉。 */
    bool fresh_occ = false;
    {
        spectrum_occ_t occ;
        if (spectrum_occ_take(&occ) && occ.frames) {
            fresh_occ = true;
            int og = freq_to_grid((uint64_t)occ.freq_mhz * 1000000ull);
            if (og >= 0 && og < RD_GRID) {
                unsigned pct = (unsigned)((uint64_t)occ.hits * 100u / occ.frames);
                /* α=1/8 的 EMA：一轮扫到该格一次，τ ≈ 8 轮 ≈ 24 s。 */
                int cur = s_occ[og], d = (int)pct - cur;
                s_occ[og] = (uint8_t)(d > 0 ? cur + (d + 7) / 8 : cur + d / 8);
            }
        }
    }
    if (s_heat_valid && mhz == s_heat_last_mhz && !fresh_occ) {
        return;
    }
    /* 底噪：25% 分位（直方图）。功率码是 0..255，256 桶刚好。 */
    uint16_t *hist = s_hist;
    memset(hist, 0, sizeof(s_hist));
    for (unsigned i = 0; i < bins; i++) {
        hist[row[i]]++;
    }
    uint8_t floor_q = hist_quantile(hist, bins, 1, 4);
    uint8_t thr = (uint8_t)(floor_q + 8 > floor_q ? floor_q + 8 : 255);
    s_dbg_p50_raw = hist_quantile(hist, bins, 1, 2);
    s_dbg_bins = (uint16_t)bins;
    s_dbg_rate = rate;
    {
        unsigned nzr = 0;
        for (unsigned i = 0; i < bins; i++)
            if (row[i] <= 2) nzr++;
        s_dbg_nz = nzr;
    }

    /* 窗宽 20 MHz / 每格 1.9 MHz ≈ 10.5 格，留足余量给边界跨格。
     *
     * 双向结算（用户指定的算法）：这一窗**覆盖到**的格，有信号 +1（封顶 255）、
     * 没信号 -1（触底 0）；这一窗**没覆盖到**的格一律不动。
     * 这样「记忆」才成立：停扫后画面凝固在最后一次扫描的结果上，
     * 而真正没人用的频段会在后续扫描里自己褪到 0。
     *
     * 旧版在「频点回退 = 一轮扫完」时对**全表**减一，与每轮 +1 正好相抵，
     * 热力几乎永远涨不起来；停扫后又被残余 -1 清零 → 记忆功能形同虚设。 */
    int cov[24];
    int nc = 0;
    int hit[24];
    int nh = 0;
    uint64_t lo_hz = (uint64_t)mhz * 1000000ull;
    lo_hz = lo_hz > rate / 2u ? lo_hz - rate / 2u : 0;
    for (unsigned i = 0; i < bins; i++) {
        uint64_t f = lo_hz + (uint64_t)rate * (2u * i + 1u) / (2u * bins);
        int g = freq_to_grid(f);
        if (g < 0 || g >= RD_GRID) continue;
        bool seen = false;
        for (int k = 0; k < nc; k++) {
            if (cov[k] == g) {
                seen = true;
                break;
            }
        }
        if (!seen) {
            if (nc >= (int)(sizeof(cov) / sizeof(cov[0]))) continue;
            cov[nc++] = g;
        }
        if (row[i] <= thr) continue;
        bool dup = false;
        for (int k = 0; k < nh; k++) {
            if (hit[k] == g) {
                dup = true;
                break;
            }
        }
        if (!dup && nh < (int)(sizeof(hit) / sizeof(hit[0]))) hit[nh++] = g;
    }
    s_win_mhz = (uint16_t)mhz;
    s_win_rate = rate;

    s_dbg_noise = floor_q;

    /* 幅度峰值：把本窗每个格子对应的最大功率码并进 s_lvl。
     *
     * 并入之前先对整网格衰减一次 —— 这个量代表「**现在**有多强」，不是
     * 「历史上最强过多少」。只 max 不衰减的话，路由器关掉、临时热点关掉
     * 之后 ALERT 页照样满屏报警，「重点关注」就退化成「永久报警」。
     * 每帧 -3%（且至少减 1，避免小值右移成 0 卡住不降），20 fps 下半衰期
     * 约 4.5 秒：信号真消失，几秒内就从榜上掉下去。
     */
    for (int g = 0; g < RD_GRID; g++) {
        if (s_lvl[g]) s_lvl[g] -= (uint8_t)((s_lvl[g] >> 5) | 1u);
        /* RING 专用量衰减慢得多（1/64 ≈ 1.5%/帧，半衰 ~9 秒 = 约 4 轮扫描），
         * 这样整圈都能同时保住各自的电平量级，颜色才有层次（详见数组声明处）。 */
        if (s_ring_lvl[g]) s_ring_lvl[g] -= (uint8_t)((s_ring_lvl[g] >> 6) | 1u);
    }
    for (unsigned i = 0; i < bins; i++) {
        uint64_t f = lo_hz + (uint64_t)rate * (2u * i + 1u) / (2u * bins);
        int g = freq_to_grid(f);
        if (g < 0 || g >= RD_GRID) continue;
        if (row[i] > s_lvl[g]) s_lvl[g] = row[i];
        if (row[i] > s_ring_lvl[g]) s_ring_lvl[g] = row[i];
    }
    for (int k = 0; k < nc; k++) {
        bool is_hit = false;
        for (int j = 0; j < nh; j++) {
            if (hit[j] == cov[k]) {
                is_hit = true;
                break;
            }
        }
        if (is_hit) {
            if (s_heat[cov[k]] < 255) s_heat[cov[k]]++;
        } else if (s_heat[cov[k]]) {
            s_heat[cov[k]]--;
        }
    }

    s_heat_last_mhz = mhz;
    s_heat_valid = true;
}

void round_ui_clear_heat(void)
{
    memset(s_heat, 0, sizeof(s_heat));
    memset(s_lvl, 0, sizeof(s_lvl));
    memset(s_ring_lvl, 0, sizeof(s_ring_lvl));
    memset(s_occ, 0, sizeof(s_occ));
    s_heat_last_mhz = 0;
    s_heat_valid = false;
}

/* ---- 方向表（一次性）：176 块的单位方向，整圆顺时针 ----
 * 角度映射：ψ(b) = 360° × b / 176，b=0 在 12 点（2400 MHz），顺时针递增。
 * 屏幕 y 向下，所以：
 *     x 方向 = sin ψ   →  sn[b] = round(16384 · sin ψ)
 *     y 方向 = cos ψ   →  cs[b] = round(16384 · cos ψ)   （取负即向上）
 *
 * 为什么 176 而不是 352：352 格铺满 360° 时每格只有 1.57~1.96 px，
 * 逐格画必出莫尔纹。合并成 176 块（每块吃相邻 2 格，取 max），
 * 块节距 3.14~3.93 px，再画 3 px 实心条 + 自然缝隙，纹路消失。
 *
 * 为什么不再存 s_y_lo/s_y_hi：整圆之后每块的 y 区间不再单调可用，
 * 而且条带裁剪靠 draw_clip 本身就够快，这两张表纯属浪费 704 B。
 *
 * 表尺寸：176 × 2 × 2 B = 704 B（旧版 352×2×2 + 352×2 = 2,112 B）。 */
static int16_t sn[RD_BLOCKS];
static int16_t cs[RD_BLOCKS];
static bool s_tab_ready;

static void tab_build(void)
{
    for (int b = 0; b < RD_BLOCKS; b++) {
        float th = 2.0f * (float)M_PI * (float)b / (float)RD_BLOCKS;
        sn[b] = (int16_t)(sinf(th) * 16384.0f);
        cs[b] = (int16_t)(cosf(th) * 16384.0f);
    }
    s_tab_ready = true;
}

/* Bresenham + 条带裁剪：只写落在 [y0, y0+rows) 内的点。 */
static void draw_clip(uint8_t *buf, int y0, int rows, int x1, int y1, int x2, int y2, uint16_t c)
{
    int dx = x2 > x1 ? x2 - x1 : x1 - x2;
    int dy = y2 > y1 ? y1 - y2 : y2 - y1;    /* 取负 */
    int sx = x1 < x2 ? 1 : -1;
    int sy = y1 < y2 ? 1 : -1;
    int err = dx + dy;
    uint8_t hi = (uint8_t)(c >> 8), lo = (uint8_t)(c & 0xFF);
    for (;;) {
        if (y1 >= y0 && y1 < y0 + rows && x1 >= 0 && x1 < RD_W) {
            size_t o = (size_t)((y1 - y0) * RD_W + x1) * 2;
            buf[o] = hi;
            buf[o + 1] = lo;
        }
        if (x1 == x2 && y1 == y2) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x1 += sx; }
        if (e2 <= dx) { err += dx; y1 += sy; }
    }
}

/* 整数开方：二进制逐位法，约 20 周期，无 libm 依赖。
 * 逐行填圆要靠它算每行的水平半宽（见 render_band ①）。 */
static int isqrt_i(int v)
{
    if (v <= 0) return 0;
    int r = 0;
    int bit = 1 << 14;
    while (bit > v) bit >>= 2;
    while (bit) {
        if (v >= r + bit) {
            v -= r + bit;
            r = (r >> 1) + bit;
        } else {
            r >>= 1;
        }
        bit >>= 2;
    }
    return r;
}

/* 从半径 r1 到 r2 画一块（块下标 b），k 是切向偏移：
 *   k = -16384 / 0 / +16384  对应 -1 / 0 / +1 像素
 * 三块平行线拼起来就是 3 px 实心条，比逐像素填充省事且天然带缝。
 *
 * +8192 是四舍五入，必须加：不加的话 45° 附近偏移被整体截断成 0，
 * 相邻块之间会漏出 1 px 的缝（真板实测过）。 */
static void draw_radial_k(uint8_t *buf, int y0, int rows, int b,
                          int r1, int r2, int k, uint16_t c)
{
    const int cx = RD_W / 2, cy = RD_H / 2;
    int x1 = cx + ((sn[b] * r1 + cs[b] * k + 8192) >> 14);
    int y1 = cy - ((cs[b] * r1 - sn[b] * k + 8192) >> 14);
    int x2 = cx + ((sn[b] * r2 + cs[b] * k + 8192) >> 14);
    int y2 = cy - ((cs[b] * r2 - sn[b] * k + 8192) >> 14);
    draw_clip(buf, y0, rows, x1, y1, x2, y2, c);
}

/* 渲染：① 环带底色（无数据时也看得见屏在活） ② 每格热力径向条 ③ 刻度与分段线。
 * 纯函数：只读 s_heat（只由 ui_task 写）→ 与射频任务无竞争。 */
static void draw_core_readout(uint8_t *buf, int y0, int rows);
/* 环上频段标签要用文字原语，而它定义在 render_ring 之后 → 先声明。 */
static void rd_text_s(uint8_t *buf, int y0, int rows, int x, int y,
                      const char *s, uint16_t c, int sc);
static int rd_text_w_s(const char *s, int sc);

static void render_ring(uint8_t *buf, int y0, int rows)
{
    if (y0 == 0) {   /* 每帧只抓第一块 */
        s_dbg_fill = s_fill_color;
        s_dbg_n++;
    }
    /* 调试填充：整屏纯色，用于「改屏内容 → 拍照对比」确认画面归属。 */
    if (s_fill_color) {
        for (int r = 0; r < rows; r++) {
            for (int x = 0; x < RD_W; x++) {
                size_t o = (size_t)(r * RD_W + x) * 2;
                buf[o] = (uint8_t)(s_fill_color >> 8);
                buf[o + 1] = (uint8_t)(s_fill_color & 0xFF);
            }
        }
        if (y0 == 0) {
            for (int i = 0; i < 8; i++) s_dbg_buf[i] = buf[i];
        }
        return;
    }
    if (!s_tab_ready) tab_build();
    const int cx = RD_W / 2, cy = RD_H / 2;
    const int span = RD_HEAT_HI - RD_HEAT_LO;

    /* ① 底色：整圆（不再只画上半圆）+ 逐行 isqrt 区间填充
     *
     * 旧版逐像素算 dx*dx+dy*dy 再比半径：57,600 次乘加/帧，实测 2,500 µs。
     * 新版每行只求两次整数开方（480 次/帧），得到该行的左右边界，
     * 边界内用 32-bit 批量写——约 700 µs，整圆的额外面积几乎免费。 */
    for (int r = 0; r < rows; r++) {
        int y = y0 + r;
        int dy = y - cy;
        int ady = dy < 0 ? -dy : dy;
        int xo = 0, xi = 0;
        if (ady < RD_R_OUT) {
            xo = isqrt_i(RD_R_OUT * RD_R_OUT - dy * dy);
        }
        if (ady < RD_HEAT_LO) {
            xi = isqrt_i(RD_HEAT_LO * RD_HEAT_LO - dy * dy);
        }
        /* 五个区间：[0,xL) 黑 | [xL,xI) 环底 | [xI,xI2) 黑 | [xI2,xR) 环底 | [xR,W) 黑 */
        int xL = cx - xo, xR = cx + xo;
        int xI = cx - xi, xI2 = cx + xi;
        for (int x = 0; x < RD_W; x++) {
            uint16_t c = 0x0000;
            if (x >= xL && x < xR && (x < xI || x >= xI2)) {
                c = 0x0841;   /* 环带底：中性 (8,8,8)，旧版 0x0861 偏绿显脏 */
            }
            size_t o = (size_t)(r * RD_W + x) * 2;
            buf[o] = (uint8_t)(c >> 8);
            buf[o + 1] = (uint8_t)(c & 0xFF);
        }
    }

    /* ② 外框圈：1 px，只提示"这是圆的"，不抢数据 */
    for (int b = 0; b < RD_BLOCKS; b++) {
        draw_radial_k(buf, y0, rows, b, RD_R_OUT - 1, RD_R_OUT, 0, 0x18E3);
    }

    /* ③ 刻度：主刻度 4 根（90° 一根，2 px 宽） + 次刻度 16 根（22.5°，1 px） */
    for (int b = 0; b < RD_BLOCKS; b += RD_TICK_STEP / 2) {
        bool major = (b % (RD_TICK_MAJOR / 2)) == 0;
        if (major) {
            draw_radial_k(buf, y0, rows, b, RD_TICK_LO, RD_TICK_HI, 0, 0xC618);
            draw_radial_k(buf, y0, rows, b, RD_TICK_LO, RD_TICK_HI, 1, 0xC618);
        } else {
            draw_radial_k(buf, y0, rows, b, RD_TICK_LO, RD_TICK_HI - 2, 0, 0x5AEB);
        }
    }

    /* ④ 热力环：176 块 × 3 px 实心条
     *
     * **长度**与**颜色**是两套口径，别混（混过一次，环带整圈变蓝）：
     *   长度 = s_heat（时间累积：该频点多久没断过）相对当前峰值 + 开方压缩。
     *          几何长度相对归一合理：最强格恒定满长（20 px），弱格仍可见。
     *   颜色 = s_ring_lvl（RING 专用慢衰减电平）+ gain 2，绝对口径不归一。
     *          绝对值才是「这一刻有多强」的可读表达。
     *
     * 三次踩坑记录（都被实测证伪，别再回头）：
     *   ① `view_color(vv*255/pmax)`：pmax 会随运行时间涨到 255（heat 饱和上限
     *      写死 255，不是设计满档 96），一旦顶住，除法等于没做，弱格索引退化成
     *      自身小值 → 全落在冷端。实测复位 0s pmax=8 / 31s 51 / 61s 95 / 183s 255。
     *   ② 改成「按满档开方」：仍蓝。因为 s_heat 跑久后每格都停在 1~30，没层次。
     *   ③ 改用 s_lvl 上色：仍蓝。它半衰 4.5 秒，而扫一轮要 2.3 秒，轮到某格前
     *      值已掉到峰值 ~25% → lv 普遍 4~20 → pal[8..40] 暗紫/深蓝。
     * 结论：RING 需要一个**既反映即时功率、又跨扫描轮保持**的电平量 → s_ring_lvl。 */
    uint8_t pmax = 0;
    for (int b = 0; b < RD_BLOCKS; b++) {
        uint8_t v = s_heat[2 * b] > s_heat[2 * b + 1] ? s_heat[2 * b] : s_heat[2 * b + 1];
        if (v > pmax) pmax = v;
    }
    if (pmax < 8) pmax = 8;   /* 只有 1~2 格噪声时不让它撑满整环 */
    s_dbg_pmax = pmax;
    s_dbg_b = 0xFF;
    s_dbg_len = 0;
    s_dbg_col = 0;
    for (int b = 0; b < RD_BLOCKS; b++) {
        uint8_t v0 = s_heat[2 * b], v1 = s_heat[2 * b + 1];
        uint8_t v = v0 > v1 ? v0 : v1;
        /* 条的存在看 RING 电平，不看记忆量：s_heat 要几十秒才积累起来，
         * 复位/烧录后立刻看屏时 352 格里只有 ~114 格有条（实测），
         * 其余只剩深蓝环底 → 整环「一片蓝」。 */
        uint8_t r0 = s_ring_lvl[2 * b], r1 = s_ring_lvl[2 * b + 1];
        uint8_t rl = r0 > r1 ? r0 : r1;
        if (!rl) continue;
        int vv = v > pmax ? pmax : v;
        int len = span * (int)isqrt_i((uint32_t)vv * 1024u / (uint32_t)pmax) / 32;
        if (len < 3) len = 3;   /* 极弱信号/刚复位也留 3 px，否则在环底上等于看不见 */
        /* 颜色：RING 专用慢衰减电平 + gain 2，与 waterfall 的即时功率码同口径。
         * 不用 pmax 归一（那会把弱格压进冷端，和 waterfall 之前那次同款错误）。 */
        uint16_t c = view_color(rl, 0, 2);
        if (s_dbg_b == 0xFF) { s_dbg_b = (uint8_t)b; s_dbg_len = (uint8_t)len; s_dbg_col = c; }
        /* k 的单位是**像素**：这里原来传 k * 16384，等于把两条平行线甩到屏幕外，
         * 每格只剩中间 1 px，而 176 格在 r≈98 处的间距是 3.5 px —— 整圈看起来
         * 是"栅栏"，弱而持续的发射在环上读成断续的缝。频段色环同款 bug 修完
         * 覆盖率 31% -> 87.6%，这里照同一口径补实。 */
        for (int k = -1; k <= 1; k++) {
            draw_radial_k(buf, y0, rows, b, RD_HEAT_HI - len, RD_HEAT_HI,
                          k, c);
        }
    }

    /* ④c 扫描光标：在环外侧（108..112，环底区）标出"现在正在扫哪一段"。
     *
     * 用户的观察：「热力图看不到当前在测的频段，也看不到光标一直转动」。
     * 352 格环上是"过去攒下来的记忆"，没有任何东西表示扫描头此刻的位置，
     * 静止画面看不出它活着。做法：
     *   ① 窗口覆盖的整段（rate/2 到 rate/2）画一条暗青弧 = 当前测量范围
     *   ② 窗口中心画 5 px 亮白弧   = 扫描头
     * 指针跟着窗口跳，转一圈就把整段频段扫完。 */
    if (s_win_rate) {
        uint64_t half = s_win_rate / 2u;
        uint64_t c_hz = (uint64_t)s_win_mhz * 1000000ull;
        int g_lo = freq_to_grid(c_hz > half ? c_hz - half : 0);
        int g_hi = freq_to_grid(c_hz + half);
        if (g_lo >= 0 && g_hi >= 0) {
            if (g_hi < g_lo) { int t = g_lo; g_lo = g_hi; g_hi = t; }
            for (int g = g_lo; g <= g_hi && g < RD_GRID; g++)
                draw_radial_k(buf, y0, rows, g / 2, RD_R_OUT - 8, RD_R_OUT - 5,
                              0, 0x02CF);
            int gc = freq_to_grid(c_hz);
            if (gc < 0) gc = 0;
            for (int k = -2; k <= 2; k++)
                draw_radial_k(buf, y0, rows, (gc + k + RD_GRID) % RD_GRID / 2,
                              RD_R_OUT - 10, RD_R_OUT - 3, 0, 0xFFFF);
        }
    }

    /* ④b 圆心：峰值读数（或未扫描时的 NO SCAN 提示）。
     * 原先这里是 "NO SCAN" 4 倍字块，圆心 r<74 平时整片纯黑。 */
    draw_core_readout(buf, y0, rows);

    /* ④c 本页名字（用户 2026-11-24：第一页也要有个名字）。
     * 这一页 = 每个信道一格、颜色/径向长度表示记忆强度的环带，所以叫 HEATMAP。
     * y=164 原来放 QUIET/LIGHT/CROWD/HEAVY 等级词，删掉后正好空出来。 */
    rd_text_s(buf, y0, rows, (RD_W - rd_text_w_s("HEATMAP", 1)) / 2, 164,
              "HEATMAP", rd_pal256[72], 1);

    /* ⑤ 段间缺口：g=44 / 97 / 150 / 286 处擦 2 px 黑，把五段分开（g=150 是
     *   UNII-2A/2C 接缝——2026-11-25 起按 FCC 法规术语拆成两块，画线；
     *   此前合并口径里 g=150 不画线）。
     * 旧版画 0xFFFF 纯白长线——L*=100 比数据顶部 L*=93 还亮，层级反转，
     * 白线比数据本身更抢眼。擦黑才是正确做法。 */
    {
        const int seps[4] = { RD_SEG_24G, RD_SEG_24G + RD_SEG_U1,
                              RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A,
                              RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A + RD_SEG_U2C };
        for (int i = 0; i < 4; i++) {
            int b = seps[i] / 2;
            for (int k = -1; k <= 1; k++) {
                draw_radial_k(buf, y0, rows, b, RD_HEAT_LO, RD_HEAT_HI,
                              k, 0x0000);   /* k 是像素，不能写 k*16384 */
            }
        }
    }

    /* ⑥ 12 点接缝：b=0 处一条暗线，让"起点"看得出来 */
    draw_radial_k(buf, y0, rows, 0, RD_R_CORE, RD_R_OUT, 0, 0x8410);

    /* ⑦ 频段色环（半径 78..84）：**用颜色代替文字**。
     *   五段弧各给一个颜色 = 哪段弧是 2.4G / UNII-1 / 2A / 2C / UNII-3；当前窗
     *   所在频段用全亮色，其余四段折半变暗 —— 扫到哪儿一眼能看出来。
     *   半径 78..84 夹在圆心功能区 74 与热力环内径 88 之间，既不压圆心读数，
     *   也不盖热力条；上下各留 3~4 px 黑缝，与热力环视觉上分开。 */
    {
        int cur = band_of_mhz(s_win_mhz);
        int b24 = RD_SEG_24G / 2;
        int b1  = (RD_SEG_24G + RD_SEG_U1) / 2;
        int b2a = (RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A) / 2;
        int b2c = (RD_SEG_24G + RD_SEG_U1 + RD_SEG_U2A + RD_SEG_U2C) / 2;
        for (int b = 0; b < RD_BLOCKS; b++) {
            int seg = (b < b24) ? 0 : (b < b1) ? 1 : (b < b2a) ? 2 : (b < b2c) ? 3 : 4;
            uint16_t c = (seg == cur) ? s_band_col[seg] : rd_dim(s_band_col[seg]);
            /* 三条并排：176 条 1 px 径向线在 r≈81 处间距 2.9 px，单线画出来是
             * 梳齿（实测覆盖率只有 31%）。k 的单位就是**像素**（偏移量 =
             * (cs*k)>>14 ≈ k*cos），所以这里给 ±1，不能写成 k*16384 ——
             * 那样等于把线甩到 16000 px 之外，等于没画。 */
            for (int k = -1; k <= 1; k++)
                draw_radial_k(buf, y0, rows, b, 78, 84, k, c);
        }
    }

    if (y0 == 0) {
        for (int i = 0; i < 8; i++) s_dbg_buf[i] = buf[i];
    }
}


/* ===================== 多视图渲染 =====================
 *
 * 一张 240x240 圆屏只放一件事，所以切成四种视图，用 `RD VIEW <n>` 串口切换：
 *   0 RING   环带热力（角度 = 频率，径向长度/颜色 = 热力记忆）
 *   1 OCCUPY 信道占用榜（每行 1 个信道：频率(信道号) + 柱 + 时间占空比 %）
 *   2 WFALL  实时频谱 + 2D 滚动瀑布（时间轴向下，附高低颜色状态轴）
 *
 * 【3D 瀑布已砍】C5 单核、21 fps：透视投影每帧上千次三角函数，render_us 翻倍，
 * 而 240 px 圆屏上纵深方向只有几十像素，肉眼分不出差别。换成 2D 滚动瀑布后
 * 信息量不变，成本低一个数量级。
 */

/* POWER 页已合并进 RING 圆心，独立页删除：
 * 圆心 r<74 那片纯黑本来就浪费，而 POWER 只放 4 个数字，两处都亏。
 * 现在 4 页：RING(含圆心读数) / CHART / WATERFALL / UNKNOWN。 */
#define RD_VIEW_RING    0
#define RD_VIEW_CHART   1
#define RD_VIEW_WFALL   2
#define RD_VIEW_COUNT   3          /* 0 ring / 1 chart / 2 waterfall */

static uint8_t s_view;                    /* 当前视图 */
static uint8_t s_ch_val[RD_NCH_MAX];      /* 每个信道的热力均值 */
/* 已知热点登记表：哪几个 WiFi 信道上有我们查得到名字的 AP。
 *
 * 用户要的"无法查到热点名的 WiFi 设备"，从这块只有射频前端、没有 WiFi
 * 协议栈的板子上，唯一能给出的诚实定义就是：
 *   在某个 WiFi 信道上持续有能量，但该信道不在"已知热点"名单里。
 * 名单由上位机通过 RD KNOWN <mhz> 逐条下发（上位机做完 WiFi 扫描后
 * 把 SSID 可见的 AP 中心频率灌进来），固件不去猜 SSID。
 * 名单为空时，所有活跃信道都会被标为未知——这正是"还没做 WiFi 扫描"的
 * 真实状态，不做假。 */
#define RD_KNOWN_MAX 48
static uint16_t s_known[RD_KNOWN_MAX];
static uint8_t s_known_n;

static uint8_t s_spec[RD_SPEC_N];         /* 当前频谱 60 柱 */
static uint16_t s_spec_mhz;
static uint8_t s_wf[RD_WF_N][RD_WF_N];    /* 瀑布历史 64x64 = 4096 B（static，不进栈） */
/* 方案 A（2026-11-24 用户拍板）：换信道**不清**历史，只记"新数据从哪一行开始"
 * （每行 1 bit），渲染成一条灰线。清历史要 41 行 x 3 帧 ≈ 6 s 重填，
 * 用户看到的就是"切过去黑屏/卡很久"。 */
static uint8_t s_wf_sep[(RD_WF_N + 7) / 8];

/* ---- 演示编排（RD DEMO 开 / RD DEMO OFF 停，2026-11-24 用户需求）----
 * 一条命令让屏自己循环走完：页 1 扫描光标转 1.5 圈 -> 页 2 下翻一页再上翻
 * 回来 -> 页 3 钉窗驻留 10 s -> 回页 1。给现场展示用，编排在固件里跑，
 * 断开电脑也继续。相位状态就这几个字节。 */
#define RD_DEMO_PAGE1_DIST        (RD_GRID * 3 / 2)  /* 1.5 圈 = 528 格行程 */
#define RD_DEMO_PAGE1_TIMEOUT_MS  30000              /* 扫描不动时别把演示卡死 */
#define RD_DEMO_PAGE2_STEP_MS     2500               /* 页 2 每个动作的停顿 */
#define RD_DEMO_PAGE3_MS          10000              /* 页 3 驻留 */
static bool s_demo;                       /* 演示总开关 */
static uint8_t s_demo_phase;              /* 0=页1 数行程 1=页2 翻页 2=页3 驻留 */
static uint8_t s_demo_step;               /* 相位内的子步 */
static uint16_t s_demo_dist;              /* 页 1 已走过的格数 */
static int s_demo_last_g = -1;
static int64_t s_demo_t0;
static uint8_t s_wf_head;
/* 渲染结果回读：摄像头对焦不足以分辨 5px 细节，所以直接从固件读真实渲染行。
 * s_probe_y < 0 表示不采样；app_main 设值后等一两帧再读 s_row_q。 */
static volatile int16_t s_probe_y;
static volatile uint8_t s_probe_done;
static uint8_t s_row_q[RD_ROW_N];
static uint8_t s_wf_tick;
/* 瀑布归一用的平滑基线。不能借 s_dbg_noise：那个由 heat_absorb() 在 RING
 * 视图里更新，切到 WATERFALL 后冻在旧量级，会把整行归一成 0（旧行恒黑）。 */
/* CHART 是全频段分页显示的：每页 4 个信道，37 个信道共 10 页。
 * 自动轮播让"什么都不按"也能把全频段看完，和上位机的全频段视图对齐。 */
static uint8_t s_page;                    /* CHART 当前页 */
#define MAX_LIST 12       /* RD ABD? 最多列出的 B 频点条数 */
/* 轮播开关不再单独设变量：任一页秒数 >0 即开，三页全 0 = 关
 * （用户 2026-11-25：每页单独人工设定秒数、到点强制切换）。 */
static uint8_t s_view_sec[RD_VIEW_COUNT] = { 8, 25, 8 };  /* 各页驻留秒数，
                                          * 默认 = 旧写死行为（RING/WFALL 8s、CHART 25s）。
                                          * 某页 0 = 轮到它不停留直接切下一页。 */
static int64_t s_view_ms;                 /* 进入当前视图的时刻（驻留计时起点） */

/* 临时诊断（2026-11-24，查完即删）：用户报"页 3 按键/横滑没反应"，
 * 需要把三段分开看：① 触摸有没有上报按下/抬起 ② 命中判定有没有进 btn/swp
 * ③ 动作有没有真的执行（pin = rd_pin_channel 调用次数）。
 * 有它就省掉"猜是驱动坏了还是几何算错了"。 */
static uint32_t s_dbg_press, s_dbg_release, s_dbg_chart, s_dbg_wfs, s_dbg_pin;
static uint16_t s_dbg_x0, s_dbg_y0, s_dbg_x1, s_dbg_y1;

/* ---- 绘图原语（全部支持条带裁剪：只画落在 [y0, y0+rows) 内的像素） ---- */

static inline void rd_px(uint8_t *buf, int y0, int rows, int x, int y, uint16_t c)
{
    if (y < y0 || y >= y0 + rows || x < 0 || x >= RD_W) return;
    size_t o = (size_t)((y - y0) * RD_W + x) * 2;
    buf[o] = (uint8_t)(c >> 8);
    buf[o + 1] = (uint8_t)(c & 0xFF);
}

static void rd_hline(uint8_t *buf, int y0, int rows, int x1, int x2, int y, uint16_t c)
{
    if (y < y0 || y >= y0 + rows) return;
    if (x1 < 0) x1 = 0;
    if (x2 > RD_W - 1) x2 = RD_W - 1;
    if (x1 > x2) return;
    uint8_t hi = (uint8_t)(c >> 8), lo = (uint8_t)(c & 0xFF);
    uint8_t *row = buf + (size_t)(y - y0) * RD_W * 2;
    for (int x = x1; x <= x2; x++) {
        row[x * 2] = hi;
        row[x * 2 + 1] = lo;
    }
}

static void rd_rect(uint8_t *buf, int y0, int rows, int x, int y, int w, int h, uint16_t c)
{
    if (w <= 0 || h <= 0) return;
    for (int r = 0; r < h; r++) rd_hline(buf, y0, rows, x, x + w - 1, y + r, c);
}

/* 5x7 点阵字符，1x 缩放。240 px 屏上正好一行放 30 多个字符。 */
/* 按倍率画一个字符。5x7 点阵在 240 px 圆屏上只有指甲盖大小，
 * 标题/信道名/数值一律用 2 倍，否则用户第一眼只能看到"很细的线"。 */
/* 两档真点阵：sc<=1 用 10x18 正文，sc>=2 用 16x30 标题。
 * 旧实现是拿 5x7 逐点 rd_rect(sc,sc) 放大，4 倍时每个笔画变成 4x4 方块，
 * 实物上一团马赛克、相邻字粘在一起（用户反馈"看不清、跟别的字混了"），
 * 已彻底换成按像素高度直接光栅化的字形。 */
/* sc 是档位选择器，不是放大倍数：
 *   sc<=1  10x18 正文（consola 18px 光栅化）
 *   sc>=2  16x30 标题（consolab 28px）
 *   sc>=3  22x26 圆心大数字（consolab 36px）—— 圆心直径 148 px，
 *          之前只放 16x30，用户反馈"看不清参数"，其实空间够大。 */
static int rd_scale_w(int sc)
{
    if (sc >= 3) return RD_FONT3_W;
    return sc >= 2 ? RD_FONT2_BW : RD_FONT2_SW;
}
static int rd_scale_h(int sc)
{
    if (sc >= 3) return RD_FONT3_HH;
    return sc >= 2 ? RD_FONT2_BH : RD_FONT2_SH;
}

static void rd_char_s(uint8_t *buf, int y0, int rows, int x, int y, char ch, uint16_t c, int sc)
{
    if (ch < RD_FONT2_FIRST || ch >= RD_FONT2_FIRST + RD_FONT2_CHARS) return;
    int cw = rd_scale_w(sc), chh = rd_scale_h(sc);
    int gi = ch - RD_FONT2_FIRST;
    for (int cy = 0; cy < chh; cy++) {
        uint32_t bits;
        if (sc >= 3)      bits = rd_font3_huge[gi][cy];
        else if (sc >= 2) bits = rd_font2_big[gi][cy];
        else              bits = rd_font2_small[gi][cy];
        if (!bits) continue;
        for (int cx = 0; cx < cw; cx++) {
            if (bits & (1u << (cw - 1 - cx))) rd_px(buf, y0, rows, x + cx, y + cy, c);
        }
    }
}

static int rd_text_w(const char *s)
{
    return (int)strlen(s) * (RD_FONT2_SW + 1) - 1;
}

static void rd_text_s(uint8_t *buf, int y0, int rows, int x, int y, const char *s, uint16_t c, int sc)
{
    for (; *s; s++) {
        rd_char_s(buf, y0, rows, x, y, *s, c, sc);
        x += rd_scale_w(sc) + 1;
    }
}
static void rd_text(uint8_t *buf, int y0, int rows, int x, int y, const char *s, uint16_t c)
{
    rd_text_s(buf, y0, rows, x, y, s, c, 1);
}
static int rd_text_w_s(const char *s, int sc)
{
    return (int)strlen(s) * (rd_scale_w(sc) + 1) - 1;
}


/* 底部 4 个导航点，当前视图高亮加宽。y=224 处圆内可用 x 是 [69,171]。 */
static void draw_nav(uint8_t *buf, int y0, int rows)
{
    static const uint8_t xs[RD_VIEW_COUNT] = { 104, 120, 136 };
    for (int i = 0; i < RD_VIEW_COUNT; i++) {
        uint16_t c = (i == (int)s_view) ? 0xFFFF : 0x39E7;
        int w = (i == (int)s_view) ? 4 : 2;
        rd_rect(buf, y0, rows, xs[i] - w / 2, 224, w, 2, c);
    }
}

/* 标题 4 倍（20x28）：2 倍在实物上仍然只有指甲盖，用户第一眼看不到。
 * 页码挤到标题右侧同一行，3 倍足够且不占额外高度。 */
static void draw_head(uint8_t *buf, int y0, int rows, const char *title)
{
    /* 4 倍字宽 = len*6*4-4，5 字符标题 116 px。y=14 那一行圆的可用宽度只有
     * x∈[73,167]，标题会被圆边裁掉（RD ROW 20 实测 x=24 处仍有字）。
     * 下移到 y=22 后可用区 x∈[61,179]，居中刚好放得下。 */
    /* y=22 处圆内的可用宽度只有 124 px（|dy|=98, R=116 -> 半宽 62）。
     * 2 档字每个 17 px，"WATERFALL" 要 152 px -> 尾巴被圆边切掉，用户看到
     * 的就是 "WATERFAL"。这里先按 2 档量，放不下就降到 1 档，再放不下才
     * 让步进重叠（宁可挤也不丢字）。 */
    int sc = 2;
    int tw = rd_text_w_s(title, sc);
    if (tw > 124) { sc = 1; tw = rd_text_w_s(title, sc); }
    int tx = (RD_W - tw) / 2;
    if (tx < 26) tx = 26;
    int step = rd_scale_w(sc) + 1;
    for (int i = 0; title[i]; i++) {
        int k = 56 + i * 22;                 /* Turbo 青->绿->黄->橙 */
        if (k > 216) k = 216;
        rd_char_s(buf, y0, rows, tx + i * step, 18, title[i],
                  rd_pal256[k], sc);
    }
    /* 页码已移除：它和 4 倍标题挤在同一行会重叠，用户要求屏上不显示页码。
     * 翻到第几页由 CHART 右侧的非文字进度条体现。 */
}

/* ---------------- VIEW 1: CHART 信道占用榜 ---------------- */

static uint8_t ch_heat_avg(uint16_t mhz)
{
    int g = freq_to_grid((uint64_t)mhz * 1000000ull);
    if (g < 0) return 0;
    int lo = g > 3 ? g - 3 : 0;
    int hi = (g + 3 < RD_GRID) ? g + 3 : RD_GRID - 1;
    uint8_t mx = 0;
    for (int i = lo; i <= hi; i++) if (s_lvl[i] > mx) mx = s_lvl[i];
    return mx;
}

static void view_chart(uint8_t *buf, int y0, int rows)
{
    const int per = RD_CH_PER_PAGE;          /* 每页 6 行，行高 22（正文 10x18） */
    int pages = (RD_NCH + per - 1) / per;
    if (s_page >= pages) s_page = 0;
    if (y0 == 0) {
        /* 每帧刷新一次所有信道热力（电平均值）。
         * 注意：**它已经不参与 CHART 显示了**（CHART 改读 s_occ 占空比），
         * 保留是因为 RD RINGDBG / RD VIEWDATA 仍在回读它，属诊断量。
         * 删掉它会改变那两条回包的内容 —— 那是算法/诊断层，本次不动。 */
        for (int i = 0; i < RD_NCH; i++) s_ch_val[i] = ch_heat_avg(s_ch_mhz[i]);
    }

    /* 宽度预算（正文 10x18，字距 11 px，240 px 屏）：
     *   标签 "5825(165)" 最长 9 字符 = 98 px -> x=14..112
     *   bar  x=118..170（52 px）
     *   百分比 "99%" 3 字符 = 32 px -> x=174..206（y<=186 处圆内到 x=215）
     * bar 满量程固定 20%：实测占用率大多 <20%，用固定量程才有可比性，
     * 超过 20% 就满格、右侧百分比给出真实值。 */
    /* 圆屏边缘是弧：y=54 与 y=186 处只有 x∈[25,215] 可用。
     * 原来 x_lbl=14 -> "5825(165)" 的 '5' 会被圆边切掉（用户报的 24xx 的 '2'）。
     * 整排右移并收窄，右侧进度条同步内收，全部落在 26..214 之间。 */
    const int top = 70, lh = 20, x_lbl = 26, x_bar = 128, bar_w = 44, bar_h = 12;
    const int x_rt = 212;                    /* 百分比**右对齐**边界 */
    const int pct_full = 100;                /* bar 满量程 = 100% 占空比 */

    /* 表头（y=50）：把"这两列是什么、单位是什么"直接写在屏上。
     *   MHZ CH = 左列是「中心频率(信道号)」，频率单位 MHz
     *   OCC%   = 右列是时间占空比百分比（含义见页脚）
     * y=50 处圆内可用区只有 x∈[28,212]，所以表头左边界取 28、右对齐到 212。 */
    rd_text_s(buf, y0, rows, 28, 50, "MHz CH", rd_pal256[72], 1);
    rd_text_s(buf, y0, rows, x_rt - rd_text_w("OCC%"), 50, "OCC%", rd_pal256[72], 1);
    /* 【显示层 / 算法层分界 —— 本函数只读已有量，不算任何新统计】
     *   取到的唯一数据是 s_occ[]（时间占空比，算法在 spectrum.c 的 occ_*
     *   与 heat_absorb() 里，本函数一个字节都不参与）。
     *   取值下标规则与圆心 draw_core_readout() **完全相同**：
     *       s_occ[freq_to_grid(mhz * 1e6)]
     *   所以圆心显示「5180 / 12%」时，这一页 5180 那一行必然也是 12%。
     *   这里不做归一、不算相对底噪、不设分组基准 —— 那三样都是算法层的事。 */
    for (int k = 0; k < per; k++) {
        int idx = s_page * per + k;
        if (idx >= RD_NCH) break;
        int y = top + k * lh;
        if (y + RD_FONT2_SH < y0 || y >= y0 + rows) continue;
        uint16_t mhz = s_ch_mhz[idx];
        /* 左侧直接给中心频率，括号里补信道号（2.4G 从 2412 起、5G 从 5000 起，
         * 步进都是 5 MHz）。 */
        int chn = (mhz < 3000) ? ((int)mhz - 2407) / 5 : ((int)mhz - 5000) / 5;
        char lbl[16];
        snprintf(lbl, sizeof lbl, "%u(%d)", (unsigned)mhz, chn);
        rd_text_s(buf, y0, rows, x_lbl, y, lbl, rd_pal256[72], 1);

        /* pct = 该信道的时间占空比（%）；-1 = 该信道不在自扫计划里/不在网格上，
         * 也就是"没有数据"，显示 "--" 而不是假的 0%。 */
        int g = freq_to_grid((uint64_t)mhz * 1000000ull);
        int pct = (!ch_polled(mhz) || g < 0 || g >= RD_GRID) ? -1 : (int)s_occ[g];
        if (pct > 100) pct = 100;
        /* 颜色 = 占空比分档，阈值 5/15/35 与圆心数字同源（圆心等级词已删，
         * 所以现在颜色是唯一的"程度"提示，数字仍是唯一的口径）。 */
        uint16_t tc = 0x8410;                        /* 未测 / 0%：暗灰 */
        if (pct >= RD_OCC_HEAVY)      tc = 0xF800;   /* ≥35% 红 */
        else if (pct >= RD_OCC_CROWD) tc = 0xFD20;   /* ≥15% 橙 */
        else if (pct >= RD_OCC_LIGHT) tc = 0xFFE0;   /* ≥5%  黄 */
        else if (pct > 0)             tc = 0xC618;   /* 0<pct<5% 亮灰 */
        rd_rect(buf, y0, rows, x_bar, y + 3, bar_w, bar_h, 0x18C3);
        int w = pct > 0 ? pct * bar_w / pct_full : 0;
        if (w > bar_w) w = bar_w;
        if (w > 0) rd_rect(buf, y0, rows, x_bar, y + 3, w, bar_h, tc);
        char num[6];
        if (pct < 0) snprintf(num, sizeof num, "--");
        else         snprintf(num, sizeof num, "%d%%", pct);
        /* 右对齐到 x_rt：与表头 "OCC%" 的右边界对齐，且 "100%" 也不会被圆边切掉
         * （左对齐 x=176 时 5 字符会伸到 x=229，y>170 处圆内只到 215）。 */
        rd_text_s(buf, y0, rows, x_rt - rd_text_w(num), y, num, tc, 1);
    }

    /* 页脚（y=172）：一句话说清 OCC% 到底是什么量。
     *   占空比 = 该信道在测量窗内被判「有信号」的帧占比；一窗 8 帧、α=1/8 EMA。
     * 不写 dBm（相对功率码，无绝对校准），也不写"强/弱"这类判断词。 */
    {
        const char *foot = "OCC = BUSY TIME%";
        rd_text_s(buf, y0, rows, (RD_W - rd_text_w(foot)) / 2, 172, foot,
                  rd_pal256[72], 1);
    }

    /* 右侧进度条：只用色块，不写数字（用户明确不要页码文字）。
     * x=208..210 在 y≤186 处仍在圆内（y=186 时可用到 x=215）。 */
    int th = 186 - 54, bh = th / pages;
    if (bh < 5) bh = 5;
    int by = 54 + (pages > 1 ? (th - bh) * (int)s_page / (pages - 1) : 0);
    rd_rect(buf, y0, rows, 212, 54, 2, th, 0x2124);
    rd_rect(buf, y0, rows, 212, by, 2, bh, 0xCE79);
}

/* ---------------- VIEW 2: SPEC 实时频谱 ---------------- */

/* 采集当前 20 MHz 窗的频谱到 s_spec。
 * 必须独立于视图：瀑布也要用同一份数据，之前把它埋在 view_spec 里，
 * 结果切到瀑布视图时 s_spec 永远是空的（瀑布全黑）。 */
static void spec_sample(void)
{
    unsigned bins = 0, mhz = 0, rate = 0;
    if (!spectrum_last_row(s_row, (int)sizeof s_row, &bins, &mhz, &rate)) return;
    if (bins < RD_SPEC_N) return;
    s_spec_mhz = (uint16_t)mhz;
    int per = (int)(bins / RD_SPEC_N);
    for (int i = 0; i < RD_SPEC_N; i++) {
        unsigned s = 0;
        for (int k = 0; k < per; k++) s += s_row[i * per + k];
        s_spec[i] = (uint8_t)(s / (unsigned)per);
    }
}


/* 圆心读数：环带热力图的圆心 r<74 一直是纯黑的空白圆，
 * 而 POWER 页单独占一整页只放 4 个数字——两处都浪费。
 * 把它搬进圆心：一页里同时看到"频谱在哪热"和"最强那个在哪、多强"。
 *
 * 可用宽度受圆约束：|y-120|=60 时弦半宽约 58 px（x∈[62,178]），
 * 所以读数全部居中、最宽不超过 116 px。 */
static void draw_core_readout(uint8_t *buf, int y0, int rows)
{
    if (s_stats.heat_cells == 0) {
        rd_text_s(buf, y0, rows, (RD_W - rd_text_w_s("NO SCAN", 1)) / 2, 106,
                  "NO SCAN", 0x7BEF, 1);
        return;
    }

    /* 频率行显示的是**这一次测量的窗中心**，不是窗内最强 bin 的位置。
     * 旧版沿用 POWER 页的语义（峰值频率）画成 peak_i 换算值，结果：
     *   - 圆心下面那个占空比是按窗中心查的，两行数字指的不是同一个东西；
     *   - 数字在中心 ±rate/2 内一直跳，用户读不出「现在扫的是哪个信道」。
     * 用户定稿（2026-11-24）：就显示单次扫描的窗中心。 */
    int pmhz = (int)s_win_mhz;

    char a[16];
    /* 圆心可用区是一个圆，横向宽度随 y 变化：
     *   y=54 -> |dy|=66 -> 弦半宽 sqrt(84^2-66^2)=52 -> 全宽 104 px
     * 之前用 24x44 的 "2437" 是 99 px，只剩 8 px 余量，视觉上贴死环带、
     * 看起来就像被两边削掉。改 20x36 后 4 字符 = 83 px，两侧各留 18 px。
     *
     * ① 峰值频率 20x36 */
    /* 圆内可用宽度（r=74，圆心 120,120）随 y 变化：
     *   y=82  -> |dy|=38 -> 弦半宽 63.5 -> 全宽 127 px
     *   y=134 -> |dy|=14 -> 弦半宽 72.7 -> 全宽 145 px
     * 22x26 的 4 位数 = 88 px，在 y=82 处左右各留 19 px。
     * 之前 20x36 配 52px 字体，advance 是 29 px > 格宽 20 px，字形右边
     * 被硬切掉 9 px —— 那才是"数字看起来被压缩"的真正原因。 */
    snprintf(a, sizeof a, "%d", pmhz);
    rd_text_s(buf, y0, rows, (RD_W - rd_text_w_s(a, 3)) / 2, 82, a,
              rd_pal256[88], 3);
    /* ② MHz 单位：用户 2026-11-25 要求放频率数字（y=82..108）上方的空位，
     *   写正确大小写 "MHz"。字库已由 tools/gen_font2.py 扩充小写 z
     *   （RD_FONT2_CHARS=91，表 32..122 连续，96..121 空字形，z 与大写 Z
     *   同风格、底部对齐基线）。"MHz" 3 字符 advance 32 px，居中画在
     *   y=62..79，与数字间隙 2 px；用标签色 rd_pal256[72]，与频段名同层。 */
    rd_text_s(buf, y0, rows, (RD_W - rd_text_w_s("MHz", 1)) / 2, 62, "MHz",
              rd_pal256[72], 1);
    /* ③ 频段名：2.4G 段 "2.4G"、UNII 段 "5G-UNII-x"（用户 2026-11-25 要求 5G-
     *   前缀，现场一眼看不出 UNII 是 5G 的段），单独一行居中，与圆心读数
     *   统一居中视角。y=112 处圆心可用宽度 146 px（弦半宽 73.4）：
     *   最长 "5G-UNII-2A" 10 字符 110 px 放得下（组合式 "MHZ 5G-UNII-1" 13 字符
     *   142 px 会擦圆边，弃用；旧 "MHZ 2.4G" 组合行已拆分）。 */
    char u[16];
    uint8_t uband = band_of_mhz(s_win_mhz);
    if (uband == 0)
        snprintf(u, sizeof u, "%s", s_band_name[0]);
    else
        snprintf(u, sizeof u, "5G-%s", s_band_name[uband]);
    rd_text_s(buf, y0, rows, (RD_W - rd_text_w_s(u, 1)) / 2, 112, u,
              rd_pal256[72], 1);
    /* ④ 占用率：本信道**最近多少比例的采集帧被判为有传输**。
     * 原来的 ③ 是 peak_v*100/255（信号多强），用户明确不要强弱判断。
     * 占空比跟信号强弱无关：粗弱的持续载波可能只有 20%，而一串短包可以打满 60%。 */
    int og = freq_to_grid((uint64_t)s_win_mhz * 1000000ull);
    int occ = (og >= 0 && og < RD_GRID) ? (int)s_occ[og] : 0;
    if (occ > 100) occ = 100;
    snprintf(a, sizeof a, "%d", occ);
    int tw = rd_text_w_s(a, 3);
    rd_text_s(buf, y0, rows, (RD_W - tw) / 2 - 8, 134, a, rd_pal256[184], 3);
    rd_text_s(buf, y0, rows, (RD_W + tw) / 2 - 2, 146, "%", rd_pal256[184], 1);
    /* ⑤ 等级词（QUIET/LIGHT/CROWD/HEAVY）按用户 2026-11-24 要求删除：
     *   圆心只留「频率 + 频段 + 占空比数字」，不再给判断词。占用程度由数字
     *   本身和第二页同一组阈值（5/15/35）的颜色档体现。 */
}

/* ---------------- VIEW 4: 未知 WiFi 设备 ---------------- */

/* 某个信道是否在「上位机下发的已知热点名单」里（±3 MHz 算同一个）。
 * 名单是差分基准：板子没有 802.11 协议栈，拿不到 SSID，只能回答
 * 「这个信道有能量，而且不在你给的名单里」。 */
static bool known_near(uint16_t mhz)
{
    for (uint8_t i = 0; i < s_known_n; i++) {
        int d = (int)s_known[i] - (int)mhz;
        if (d >= -3 && d <= 3) return true;
    }
    return false;
}

/* 取该信道中心频率附近 ±2 格的最大强度（网格比信道细，单点采样会漏） */
static uint8_t ch_level(uint16_t mhz)
{
    int g = freq_to_grid((uint64_t)mhz * 1000000ull);
    if (g < 0 || g >= RD_GRID) return 0;
    uint8_t mx = 0;
    for (int k = -2; k <= 2; k++) {
        int gg = g + k;
        if (gg < 0 || gg >= RD_GRID) continue;
        if (s_lvl[gg] > mx) mx = s_lvl[gg];
    }
    return mx;
}

/* 统计「正在收发的 WiFi」里认识的和认不出的。
 *   *pa = 命中上位机下发名单的频点数（能对上名字）
 *   *pb = 不在名单里的频点数（检索不到名字 -> 可疑）
 *   *pthr = 本次使用的强度阈值，一并带出去便于诊断
 * 屏幕第 4 页已删除，现在只有 RD AB? / RD ABD? 两条诊断命令在用。
 * 判据本身留着：异常 WiFi 排查要看「哪些频点有能量但认不出名字」。 */
static void alert_count(int *pthr, int *pa, int *pb)
{
    int thr = (int)s_dbg_noise + 12;
    if (thr < 8) thr = 8;
    int a = 0, b = 0;
    for (int c = 0; c < RD_NCH; c++) {
        uint8_t lv = ch_level(s_ch_mhz[c]);
        if ((int)lv < thr) continue;            /* 没在收发：不计入 */
        if (known_near(s_ch_mhz[c])) a++;
        else                          b++;
    }
    *pthr = thr;
    *pa = a;
    *pb = b;
}

/* ---------------- VIEW 3: WFALL 2D 滚动瀑布 ---------------- */

/* 页 3（频谱 + 瀑布 + 状态轴）专用颜色：0..RD_HEAT_FULL -> 调色板 0..128。
 *
 * 原来这里写 view_color(v, 150, 2)，索引 = v*2+150：
 *   a) 落在调色板 **150..246 这段下降支**上（表本身是 0->128 变亮、128->255 再变暗），
 *      于是「柱越高颜色越暗」，直观是反的；
 *   b) v>52 时索引越过 255 回卷到 0 附近 —— 同一张图里出现硬跳变。
 * 这条映射单增、不回卷：低 = 黑/深蓝，高 = 亮黄/白。 */
static uint16_t wf_color(uint8_t v)
{
    int k = (int)v * 128 / RD_HEAT_FULL;
    if (k > 128) k = 128;
    return rd_pal256[k];
}

static void view_wfall(uint8_t *buf, int y0, int rows)
{
    /* 标准频谱分析仪布局：横轴=频率，纵轴=时间，颜色=幅度。
     * 上半部分是"当前时刻的频谱"（横轴频率），下半部分是同一条横轴上的
     * 时间瀑布（每行 = 过去某一时刻的频谱快照）。这样在纵轴任意截一行，
     * 得到的就是该时刻的频谱，和上面那条实时谱可以直接对照。
     *
     * 之前的实现只有瀑布、没有上方频谱，也没有共享横轴的语义，
     * 看起来就是一块会动的彩色马赛克——这是用户指出"瀑布图可能是错的"。
     *
     * 时间轴：21 fps 下每 3 帧推一行 -> 7 行/秒，52 行约 7.4 秒历史，
     * 最新一行在最底部，向上越来越旧（scroll-up）。
     * 圆屏 RAM 只放得下 64x64；上位机侧才是 10000 帧级的长历史。 */
    /* 当前帧的峰值/峰值位置：只用于频谱区那根白色竖线指示。
     * 注意柱高和颜色**不**按峰值归一 —— 沿用 482d1f4 的口径：
     * 柱高按固定 RD_HEAT_FULL 缩放，颜色走调色板 150 段 gain 2。 */
    int peak = 0, peak_bin = 0;
    for (int c = 0; c < RD_SPEC_N && c < RD_WF_N; c++) {
        int v = s_spec[c];
        if (v > peak) { peak = v; peak_bin = c; }
    }
    if (y0 == 0) {
        if (++s_wf_tick >= 3) {
            s_wf_tick = 0;
            s_wf_head = (uint8_t)((s_wf_head + 1) % RD_WF_N);
            for (int i = 0; i < RD_WF_N; i++) {
                s_wf[s_wf_head][i] = s_spec[i];
            }
        }
    }

    /* 标题占 y=18..48，所以频谱从 y=72 起。之前把频率标注写在 y=30，
     * 正好压在标题上（用户报的"waterfall 文字被覆盖"）。 */
    const int wx = 56, bw = 2;            /* 每 bin 2 px，64 bin = 128 px，居中 */
    const int spec_y = 72, spec_h = 26;   /* 上半：实时频谱 */
    const int base_y = spec_y + spec_h + 2;
    const int wf_rows = 41, wf_h = 2;     /* 下半：时间瀑布 41 行 x 2 px */

    /* ---- 上半：当前时刻频谱（横轴 = 频率） ---- */
    for (int c = 0; c < RD_SPEC_N && c < RD_WF_N; c++) {
        int v = s_spec[c];
        int h = v * spec_h / RD_HEAT_FULL;
        if (h > spec_h) h = spec_h;
        if (h > 0)
            rd_rect(buf, y0, rows, wx + c * bw, spec_y + spec_h - h, bw - 1, h,
                    wf_color((uint8_t)v));
    }
    /* 峰值频率指示：白色竖线，一眼看出当前最强信号在横轴哪个位置 */
    if (peak > 0)
        rd_rect(buf, y0, rows, wx + peak_bin * bw, spec_y, 2, spec_h, 0xFFFF);
    /* 横轴频率标注：没有它这块就是"会动的彩色马赛克"，看不出横轴
     * 到底是频率、范围从多少到多少。左端 = 窗口下沿，右端 = 上沿。 */
    /* 判据必须是 s_win_mhz 而不是 s_win_rate：
     *   - s_win_rate 要到第一次真正扫完才非 0，开机头一两秒它是 0，
     *     原来 `if (s_win_rate)` 会把整条横轴标注跳掉（看着像"数字不见了"）；
     *   - 但也不能只看 s_win_rate 就硬兜一个 20 MHz —— s_win_mhz 同为 0 时
     *     会算出 "-10 / 10" 这种根本不存在的频率，比不显示更糟。
     * s_win_mhz 非 0 就说明至少扫过一次，此时 rate 才有默认值的意义。 */
    if (s_win_mhz) {
        uint32_t rate = s_win_rate ? s_win_rate : 20000000u;
        char lo[10], hi[10];
        int lo_mhz = (int)s_win_mhz - (int)(rate / 2000000u);
        int hi_mhz = (int)s_win_mhz + (int)(rate / 2000000u);
        snprintf(lo, sizeof lo, "%d", lo_mhz);
        snprintf(hi, sizeof hi, "%d", hi_mhz);
        /* y=52 处圆内 x∈[26,214]，原来 16 / RD_W-16-hw 两端都会被圆边切掉。 */
        rd_text_s(buf, y0, rows, 28, 52, lo, rd_pal256[72], 1);
        int hw = rd_text_w(hi);
        rd_text_s(buf, y0, rows, RD_W - 28 - hw, 52, hi, rd_pal256[72], 1);
        /* 两个裸数字 2402 / 2442 看不出是频率还是信道号 —— 中间补单位。
         * x=104..135 落在左数(≤71)与右数(≥169)之间，互不重叠。 */
        rd_text_s(buf, y0, rows, 104, 52, "MHz", rd_pal256[72], 1);
    }

    /* 基线 + 频谱与瀑布的分界 */
    rd_rect(buf, y0, rows, wx, base_y, RD_WF_N * bw, 1, 0x4208);

    /* 高低颜色状态轴（用户 2026-11-24 要求把上位机模拟屏上的这条轴搬进固件）：
     * 8 档色块，取自**与柱体完全相同**的 wf_color()，所以轴上每一格的颜色
     * 必然能在频谱柱/瀑布里找到，HI 在上、LO 在下。
     * x=190..195 在频谱区(56..184)右侧，y=78..101 处圆内可用到 x≈226。 */
    for (int i = 0; i < 8; i++) {
        int v = (i + 1) * RD_HEAT_FULL / 8;
        rd_rect(buf, y0, rows, 190, 78 + (7 - i) * 3, 6, 3,
                wf_color((uint8_t)v));
    }
    rd_text_s(buf, y0, rows, 199, 74, "HI", rd_pal256[72], 1);
    rd_text_s(buf, y0, rows, 199, 104, "LO", rd_pal256[72], 1);

    /* 右栏页码条已删（用户 2026-11-25）：钉住的信道看下方 PIN 行就够了。
     *
     * 钉住的是哪个信道：有了它，横轴那对 lo/hi 才是"这一整块都属于它"的证据。
     * y=188 在瀑布底(185)与导航点(y=224)之间；该 y 处圆内 x∈[41,199]。 */
    if (s_wf_pinned) {
        char tag[48];   /* 48：留给 -Wformat-truncation 对 int 的最坏假设（11 位） */
        snprintf(tag, sizeof tag, "PIN %d/%d %u", s_wf_ch + 1, RD_NSCAN,
                 (unsigned)s_scan_ch[s_wf_ch]);
        int tw = rd_text_w(tag);
        rd_text_s(buf, y0, rows, (RD_W - tw) / 2, 188, tag, rd_pal256[72], 1);
    }

    /* ---- 下半：时间瀑布（横轴与上面同频，纵轴 = 时间） ---- */
    for (int r = 0; r < wf_rows; r++) {
        int y = base_y + 2 + r * wf_h;
        if (y + wf_h <= y0 || y >= y0 + rows) continue;
        /* r 越大越新，最后一行 = s_wf_head（刚写入的当前帧） */
        int src = (s_wf_head - (wf_rows - 1 - r) + RD_WF_N * 2) % RD_WF_N;
        if (s_wf_sep[src >> 3] & (uint8_t)(1u << (src & 7))) {
            /* 分隔行：一条全宽灰线（与 12 点接缝同色 0x8410）。
             * 语义 = "本次测量从这里开始"，线以上是上一个信道的旧历史。 */
            rd_rect(buf, y0, rows, wx, y, RD_WF_N * bw, wf_h, 0x8410);
            continue;
        }
        for (int c = 0; c < RD_WF_N; c++) {
            uint8_t v = s_wf[src][c];
            if (!v) continue;
            rd_rect(buf, y0, rows, wx + c * bw, y, bw - 1, wf_h, wf_color(v));
        }
    }
}

/* 把第 s_probe_y 行的 30 个采样点（每 8 px 取高字节）存下来，供串口回读。 */
static void probe_sample_row(uint8_t *buf, int y0, int rows)
{
    if (s_probe_y < 0) return;
    if (s_probe_y < y0 || s_probe_y >= y0 + rows) return;
    uint8_t *row = buf + (size_t)(s_probe_y - y0) * RD_W * 2;
    for (int i = 0; i < RD_ROW_N; i++) {
        int x = i * RD_ROW_STEP;
        if (x >= RD_W) x = RD_W - 1;
        s_row_q[i] = row[x * 2];
    }
    s_probe_done = 1;
}

/* ---------------- 分派 ---------------- */

static void render_band(uint8_t *buf, int y0, int rows)
{
    if (y0 == 0) {
        s_dbg_fill = s_fill_color;
        s_dbg_n++;
    }
    /* 调试填充优先于一切视图 */
    if (s_fill_color) {
        for (int r = 0; r < rows; r++) {
            for (int x = 0; x < RD_W; x++) {
                size_t o = (size_t)(r * RD_W + x) * 2;
                buf[o] = (uint8_t)(s_fill_color >> 8);
                buf[o + 1] = (uint8_t)(s_fill_color & 0xFF);
            }
        }
        if (y0 == 0) {
            for (int i = 0; i < 8; i++) s_dbg_buf[i] = buf[i];
        }
        return;
    }
    if (!s_tab_ready) tab_build();
    if (y0 == 0) spec_sample();   /* 所有视图共享同一份频谱采样 */
    if (s_view != RD_VIEW_RING) {
        /* 非环带视图先铺黑底（环带视图自己画整圆） */
        for (int r = 0; r < rows; r++) {
            for (int x = 0; x < RD_W; x++) {
                size_t o = (size_t)(r * RD_W + x) * 2;
                buf[o] = 0;
                buf[o + 1] = 0;
            }
        }
    }

    switch (s_view) {
    case RD_VIEW_CHART: draw_head(buf, y0, rows, "OCCUPY");    view_chart(buf, y0, rows); break;
    case RD_VIEW_WFALL: draw_head(buf, y0, rows, "WATERFALL"); view_wfall(buf, y0, rows); break;
    default:            render_ring(buf, y0, rows); break;
    }
    draw_nav(buf, y0, rows);
    probe_sample_row(buf, y0, rows);
    if (y0 == 0) {
        for (int i = 0; i < 8; i++) s_dbg_buf[i] = buf[i];
    }
}

static void render_error_pattern(uint8_t *buf, int y0, int rows, bool on)
{
    uint16_t c = on ? 0xF800 : 0x0000;
    uint8_t hi = (uint8_t)(c >> 8);
    uint8_t lo = (uint8_t)(c & 0xFF);
    (void)y0;
    for (size_t i = 0; i < (size_t)rows * RD_W; i++) {
        buf[i * 2] = hi;
        buf[i * 2 + 1] = lo;
    }
}

/* 重建显示（改时钟 / 改条带 / 首次初始化都走这里）。返回 true = 就绪。 */
static bool display_build(void)
{
    if (s_disp) {
        dsh_display_del(s_disp);
        s_disp = NULL;
        s_ready = false;
    }
    dsh_display_cfg_t cfg = {
        .spi = {
            .host = RD_SPI_HOST,
            .sck = RD_SCK,
            .mosi = RD_MOSI,
            .miso = RD_MISO,
            .cs = RD_CS,
            .dc = RD_DC,
            .bl = RD_BL,
            .freq_hz = (int)s_pclk_hz,
            .frame_bytes = (int)s_band_rows * RD_W * 2,
        },
        .reset_gpio = -1,   /* 面板 RST 接 XIAO RST，软复位路径不适用 */
        .invert = true,     /* Seeed 官方 ips=true → INVON */
        .mirror_x = false,
        .mirror_y = false,
        .swap_xy = false,
    };
    if (dsh_display_init(&s_disp, &cfg) != DSH_DISPLAY_OK) {
        s_disp = NULL;
        s_ready = false;
        return false;
    }
    s_ready = true;
    return true;
}

static void rd_auto_scan(void);   /* 开机自扫，定义在后面；ui_task 屏初始化成功后调 */
/* 演示：切相位。只改 s_view/s_page，钉窗与恢复自扫由 ui_task 的视图切换
 * 副作用块统一做，这里绝不直接下发 FSM 命令（那是 rd_pin_channel 的活）。 */
static void demo_next(int64_t now, uint8_t ph)
{
    s_demo_phase = ph;
    s_demo_step = 0;
    s_demo_dist = 0;
    s_demo_last_g = -1;
    s_demo_t0 = now;
    switch (ph) {
    case 0:
        s_view = RD_VIEW_RING;      /* 离开 WFALL 的副作用块会自动 rd_auto_scan() */
        break;
    case 1:
        s_view = RD_VIEW_CHART;
        s_page = 0;
        break;
    case 2:
        s_view = RD_VIEW_WFALL;     /* 进 WFALL 的副作用块会自动 rd_pin_channel() */
        break;
    }
    s_stats.view = s_view;
    s_view_ms = now;
}

static void demo_tick(int64_t now)
{
    switch (s_demo_phase) {
    case 0: {   /* 页 1：光标沿格序号累计前进量，满 1.5 圈（528 格）就切页 2。
                 * 环上前进量 = (g - last_g + GRID) % GRID，只累计 < 100 的
                 * 正常步进，噪声/回跳不计；换圈那一下贡献 +1，一圈合计 352。 */
        int g = freq_to_grid((uint64_t)s_win_mhz * 1000000ull);
        if (g >= 0) {
            if (s_demo_last_g >= 0) {
                int d = (g - s_demo_last_g + RD_GRID) % RD_GRID;
                if (d < 100) s_demo_dist += (uint16_t)d;
            }
            s_demo_last_g = g;
        }
        if (s_demo_dist >= RD_DEMO_PAGE1_DIST ||
            now - s_demo_t0 > (int64_t)RD_DEMO_PAGE1_TIMEOUT_MS * 1000) {
            demo_next(now, 1);
        }
        break;
    }
    case 1: {   /* 页 2：停 2.5 s -> 下翻一页 -> 停 2.5 s -> 上翻回来 -> 停 1.5 s */
        int pages = (RD_NCH + RD_CH_PER_PAGE - 1) / RD_CH_PER_PAGE;
        int64_t el = now - s_demo_t0;
        if (s_demo_step == 0 && el >= (int64_t)RD_DEMO_PAGE2_STEP_MS * 1000) {
            s_demo_step = 1;
            if (pages > 1 && s_page + 1 < pages) s_page++;
        } else if (s_demo_step == 1 &&
                   el >= 2 * (int64_t)RD_DEMO_PAGE2_STEP_MS * 1000) {
            s_demo_step = 2;
            if (s_page > 0) s_page--;
        } else if (s_demo_step == 2 &&
                   el >= 2 * (int64_t)RD_DEMO_PAGE2_STEP_MS * 1000 + 1500000) {
            demo_next(now, 2);
        }
        break;
    }
    case 2:     /* 页 3：钉窗驻留 10 s（FSM 只扫这一窗，瀑布持续推进） */
        if (now - s_demo_t0 >= (int64_t)RD_DEMO_PAGE3_MS * 1000) {
            demo_next(now, 0);
        }
        break;
    }
}

static void rd_pin_channel(int idx);  /* 页 3：把扫描钉在某个信道上（idx 可越界，内部取模） */

static void ui_task(void *arg)
{
    (void)arg;
    uint8_t state = ROUND_UI_BOOT;
    uint32_t init_err = 0;
    uint32_t sub_err = 0;
    uint32_t fps_x10 = 0;
    uint32_t render_us = 0;
    int y = 0;
    int64_t fps_t0 = esp_timer_get_time();
    int64_t touch_t0 = 0;
    uint32_t win_frames = 0;
    uint32_t win_render_us = 0;
    bool boot_failed = false;
    uint8_t s_view_prev = 0;   /* 只用来捕捉视图切换（页 3 需要钉住单窗） */

    dsh_touch_cfg_t tcfg = {
        .i2c_port = 0,
        .sda = RD_TOUCH_SDA,
        .scl = RD_TOUCH_SCL,
        /* 诊断期先用纯轮询（-1）：如果只剩「INT 引脚映射错」这一种可能，
         * 纯轮询能读到坐标，就能和「I2C 不通」区分开。定位后再决定用哪种。 */
        .int_gpio = -1,
        .freq_hz = 100000,
    };
    if (dsh_touch_init(&tcfg) == DSH_TOUCH_OK) {
        /* CHSC6X 上电后不一定立刻应答（实测同一固件两次启动一次在线
         * 一次不在线），所以先用快速单点 probe 重试，别一次定生死。 */
        for (int i = 0; i < 8 && !s_touch_chsc6x; ++i) {
            if (dsh_touch_present()) {
                s_touch_chsc6x = 1;
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(120));
        }
        /* 在线时再做一次全总线扫描，把同一条线上的设备记下来供诊断。 */
        if (s_touch_chsc6x) {
            dsh_touch_scan_t sc = {0};
            if (dsh_touch_scan(&sc) == DSH_TOUCH_OK) {
                s_touch_i2c_cnt = sc.count;
                s_touch_first_addr = sc.first_addr;
            }
        }
    }

    for (;;) {
        /* ---- ① 收命令（非阻塞） ---- */
        rd_cmd_t cmd;
        while (xQueueReceive(s_q_cmd, &cmd, 0) == pdTRUE) {
            if (cmd.type == RD_CMD_RATE && cmd.arg >= 1000000u && cmd.arg <= 80000000u) {
                s_pclk_hz = cmd.arg;
                state = RD_TR[state][RD_EV_REBUILD];
            } else if (cmd.type == RD_CMD_BAND && cmd.arg >= 8u && cmd.arg <= RD_H) {
                s_band_rows = (uint16_t)cmd.arg;
                state = RD_TR[state][RD_EV_REBUILD];
            } else if (cmd.type == RD_CMD_OFF) {
                s_enabled = false;
            } else if (cmd.type == RD_CMD_ON) {
                s_enabled = true;
            } else if (cmd.type == RD_CMD_FILL) {
                if (cmd.arg == 0) {
                    s_fill_color = 0;
                } else {
                    /* 参数按 RGB888 传（0xRRGGBB），转 RGB565 */
                    uint32_t v = cmd.arg;
                    uint16_t r5 = (uint16_t)(((v >> 16) & 0xFF) >> 3);
                    uint16_t g6 = (uint16_t)(((v >> 8) & 0xFF) >> 2);
                    uint16_t b5 = (uint16_t)((v & 0xFF) >> 3);
                    s_fill_color = (uint16_t)((r5 << 11) | (g6 << 5) | b5);
                }
            } else if (cmd.type == RD_CMD_CLEAR) {
                round_ui_clear_heat();
                memset(s_lvl, 0, sizeof s_lvl);
                memset(s_peak, 0, sizeof s_peak);
                memset(s_wf, 0, sizeof s_wf);
                memset(s_wf_sep, 0, sizeof s_wf_sep);
                s_wf_head = 0;
                s_wf_tick = 0;
            } else if (cmd.type == RD_CMD_KNOWN) {
                if (s_known_n < RD_KNOWN_MAX) s_known[s_known_n++] = (uint16_t)cmd.arg;
            } else if (cmd.type == RD_CMD_PAGE) {
                int pages = (RD_NCH + RD_CH_PER_PAGE - 1) / RD_CH_PER_PAGE;
                if (cmd.arg < (uint32_t)pages) {
                    s_view = RD_VIEW_CHART;
                    s_stats.view = s_view;
                    s_page = (uint8_t)cmd.arg;
                    s_view_ms = esp_timer_get_time();
                }
            } else if (cmd.type == RD_CMD_WFCH) {
                /* 钉住指定信道并切到页 3。即便当前已在页 3 也要重钉（换窗）。
                 * 过渡块统一负责"进页 3 就钉住"，这里先同步好 s_view，
                 * 让它看到的是"已在页 3"，不会再重复下发一遍 FSM 命令。 */
                s_view = RD_VIEW_WFALL;
                s_stats.view = s_view;
                s_view_ms = esp_timer_get_time();
                rd_pin_channel((int)cmd.arg - 1);
                s_view_prev = s_view;
            } else if (cmd.type == RD_CMD_VIEW) {
                if (cmd.arg < RD_VIEW_COUNT) {
                    s_view = (uint8_t)cmd.arg;
                    s_stats.view = s_view;
                    /* 2026-11-25 改强切口径：不再静默 60 s，靠 s_view_ms 重置让
                     * 本页驻留满设定秒数才轮走；该页设 0 s 则立即切走。 */
                    s_page = 0;
                    s_view_ms = esp_timer_get_time();
                    /* 不清瀑布历史：切过去立刻就有画面，历史由 RD CLEAR 统一清 */
                    s_wf_tick = 0;
                }
            } else if (cmd.type == RD_CMD_VIEWSEC) {
                /* arg = view<<8 | sec：设每页驻留秒数（0=该页不停留）。
                 * 改完当前页重新计时，轮播开关由"是否有页非零"隐式决定。 */
                uint8_t vv = (uint8_t)(cmd.arg >> 8);
                s_view_sec[vv] = (uint8_t)(cmd.arg & 0xFF);
                s_view_ms = esp_timer_get_time();
            }
        }

        /* ---- ② 状态机出口动作 ---- */
        if (state == ROUND_UI_BOOT) {
            if (display_build()) {
                state = RD_TR[state][RD_EV_INIT_OK];
                y = 0;
                boot_failed = false;
                /* 开机自扫必须等到**屏初始化成功之后**才启动。
                 * 之前它在 round_ui_start() 里跟在 xTaskCreate 后面，由 app_main
                 * 和 ui_task 同时跑：一边 force_rx_gain() + 30 条 FSM 命令，
                 * 一边 GC9A01 复位/初始化序列。SPI 时序被打断就落到 ERROR 态，
                 * 表现就是「上电屏幕不亮/不刷新」，而再软复位一次又正常。 */
                rd_auto_scan();
            } else {
                init_err++;
                boot_failed = true;
                state = RD_TR[state][RD_EV_INIT_FAIL];
            }
        }

        if (!s_enabled) {
            stats_publish(state, fps_x10, render_us, init_err, sub_err);
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        if (state == ROUND_UI_ERROR) {
            /* 出错态：LED 亮 + 整屏红黑闪烁，不碰屏驱动（可能已经挂了） */
            dsh_hal_gpio_out(RD_LED_GPIO, true);
            uint8_t *buf = NULL;
            size_t cap = 0;
            if (dsh_display_back_buffer(s_disp, &buf, &cap) == DSH_DISPLAY_OK) {
                int rows = (int)(cap / (RD_W * 2));
                render_error_pattern(buf, 0, rows, (s_frame_no & 8) != 0);
                (void)dsh_display_submit_band(s_disp, 0, (uint16_t)rows);
            }
            stats_publish(state, 0, 0, init_err, sub_err);
            vTaskDelay(pdMS_TO_TICKS(20));
            /* 有命令就重试（REBUILD 会把 state 带回 BOOT），否则停在 ERROR */
            if (xQueueReceive(s_q_cmd, &cmd, 0) == pdTRUE) {
                if (cmd.type == RD_CMD_RATE) {
                    s_pclk_hz = cmd.arg;
                } else if (cmd.type == RD_CMD_BAND) {
                    s_band_rows = (uint16_t)cmd.arg;
                }
                state = RD_TR[state][RD_EV_REBUILD];
                s_enabled = true;
            }
            continue;
        }

        /* ---- ③ 触摸（限速 30 ms 一次，I2C 读 5 字节约 0.5 ms）----
         * 启动扫描没发现 CHSC6X(0x2E) 时整个跳过：芯片不在线时每次
         * i2c_master_receive 都要超时 20 ms，而轮询周期才 30 ms，
         * 实测把帧率从 42 拖到 34.9。INT 电平仍然采样，方便诊断。 */
        int64_t now = esp_timer_get_time();
        if (now - touch_t0 >= RD_TOUCH_PERIOD_US) {
            touch_t0 = now;
            s_pen_int_lvl = (int8_t)gpio_get_level((gpio_num_t)RD_TOUCH_INT);
            /* 没在线就低频重试（每 2 s），扫到就把触摸轮询打开。
             * 无芯片时这个分支几乎零成本（只是一次 probe）。 */
            if (!s_touch_chsc6x) {
                if (now - s_touch_retry_t >= RD_TOUCH_RETRY_US) {
                    s_touch_retry_t = now;
                    if (dsh_touch_present()) {
                        s_touch_chsc6x = 1;
                    }
                }
            } else if (s_touch_chsc6x) {
                uint16_t tx = 0, ty = 0;
                bool down = dsh_touch_read(&tx, &ty);
                if (down) {
                    /* CHSC6X 单字节返回 0..255，屏是 240：不缩放右侧 16 px 永远点不到 */
                    s_pen_x = (uint16_t)((uint32_t)tx * RD_W / 256u);
                    s_pen_y = (uint16_t)((uint32_t)ty * RD_H / 256u);
                }
                if (down && !s_pen_down) {              /* 按下沿：记起点 */
                    s_tap_x = s_pen_x;
                    s_tap_y = s_pen_y;
                    s_tap_active = 1;
                    s_dbg_press++;
                    s_dbg_x0 = s_pen_x;
                    s_dbg_y0 = s_pen_y;
                } else if (!down && s_pen_down && s_tap_active) {   /* 抬起沿：判手势 */
                    s_tap_active = 0;
                    s_dbg_release++;
                    s_dbg_x1 = s_pen_x;
                    s_dbg_y1 = s_pen_y;
                    /* 切换区限定在屏幕左侧一条（x < 92）：圆屏左边缘最好按，
                     * 右侧/中部留给"看数据"，不会误触把画面翻走。
                     * 底部 4 个小方块仍可直接点选具体视图。 */
                    int dx = (int)s_pen_x - (int)s_tap_x;
                    int dy = (int)s_pen_y - (int)s_tap_y;
                    /* CHART 里上下滑动翻阅信道：手指往上推 -> 下一页（频率更高），
                     * 往下拉 -> 回上一页。用户明确要求手动查阅，不要自动播报。 */
                    int ady = dy > 0 ? dy : -dy;
                    int adx = dx > 0 ? dx : -dx;
                    bool chartslide = (s_view == RD_VIEW_CHART && ady > 30 && ady > adx);
                    /* 页 3 横滑 = 换信道：往左推看更高频（表里后一个），往右拉回前一个。
                     * 瀑布必须钉在单一频窗上，所以换信道 = 重新钉 + 清历史。 */
                    /* 页 3 换信道：**与页 2 同一套手势** —— 上下滑一次走一格。
                     * 方向也一致：页 2 上推 = 下一页（频率更高），页 3 上推 = 后一个信道。
                     * 原来这里是"左右滑"，两页手势不一致，现场容易滑错方向。 */
                    bool wfslide = (s_view == RD_VIEW_WFALL && ady > 30 && ady > adx);
                    if (wfslide) {
                        s_dbg_wfs++;
                        rd_pin_channel(s_wf_ch + (dy < 0 ? 1 : -1));
                        s_view_ms = now;   /* 本页重新驻留（轮播强切口径，2026-11-25） */
                    }
                    if (chartslide) {
                        s_dbg_chart++;
                        int pages = (RD_NCH + RD_CH_PER_PAGE - 1) / RD_CH_PER_PAGE;
                        if (dy < 0 && s_page + 1 < pages) s_page++;
                        else if (dy > 0 && s_page > 0) s_page--;
                        s_view_ms = now;   /* 本页重新驻留（轮播强切口径，2026-11-25） */
                    }
                    /* 除了上面的竖向滑动，只剩两种明确动作：左侧条切视图、
                     * 底部 3 个小方块直选视图。CHART 的其余区域不响应，
                     * 免得想滑动看数据时被误判成切视图。 */
                    uint8_t nv = s_view;
                    bool hit = false;
                    if (chartslide || wfslide) {
                        /* 已由上面的滑动/按键处理 */
                    } else if (s_tap_x < RD_SWITCH_ZONE_W) {
                        nv = (uint8_t)((s_view + 1) % RD_VIEW_COUNT);
                        hit = true;
                    } else if (s_tap_y >= 200) {
                        int k = ((int)s_tap_x - 96) / 16;
                        if (k < 0) k = 0;
                        if (k > RD_VIEW_COUNT - 1) k = RD_VIEW_COUNT - 1;
                        nv = (uint8_t)k;
                        hit = true;
                    }
                    if (hit && nv != s_view) {
                        s_view = nv;
                        s_stats.view = nv;
                        s_wf_tick = 0;
                        s_page = 0;
                        s_view_ms = esp_timer_get_time();   /* 手动切了就从现在起算，驻留满本页秒数再轮 */
                    }
                }
                s_pen_down = down;
            }
        }

        /* ---- ③b 自动轮播：每页单独设定驻留秒数（RD VIEWSEC），到点强制切换 ----
         * 用户 2026-11-25 拍板：取消旧"手动操作后静默 60 s"豁免——轮播开着就
         * 按当前页设定秒数强切，手动切换只是把本页计时重置，不再豁免。
         * 任一页 >0 即开轮播，三页全 0 = 关；某页 0 s = 轮到它不停留直接切走
         * （0/0/0 之外只剩一页非零时就在那一页驻留，到点切自己？不会：转一圈
         * 找不到非零页时 nv == s_view，保持不动，等效轮播关闭）。
         * 页内自动翻页已移除：CHART 的信道由用户上下滑动查阅。 */
        if (!s_demo && (s_view_sec[0] || s_view_sec[1] || s_view_sec[2])) {
            if (!s_view_ms) s_view_ms = now;
            if (now - s_view_ms >= (int64_t)s_view_sec[s_view] * 1000000) {
                uint8_t nv = s_view;
                for (int i = 0; i < RD_VIEW_COUNT; ++i) {
                    nv = (uint8_t)((nv + 1) % RD_VIEW_COUNT);
                    if (s_view_sec[nv]) break;      /* 跳过 0 s 的页 */
                }
                if (nv != s_view) {
                    s_view = nv;
                    s_stats.view = nv;
                    s_wf_tick = 0;
                    s_page = 0;
                }
                s_view_ms = now;
            }
        }

        /* ---- ③d 视图切换的副作用（页 3 的单窗前提） ---- */
        if (s_view != s_view_prev) {
            if (s_view == RD_VIEW_WFALL) {
                rd_pin_channel(s_wf_ch);
            } else if (s_view_prev == RD_VIEW_WFALL) {
                /* 回到轮播：恢复整表自扫。瀑布缓冲**不清**（方案 A）：推行只在
                 * view_wfall 里发生，离开页 3 后缓冲自然冻结；再进页 3 时
                 * rd_pin_channel() 会打新的分隔线，旧数据在线以上、一眼可辨。 */
                rd_auto_scan();
                s_wf_pinned = false;
                s_wf_tick = 0;
            }
            s_view_prev = s_view;
        }

        /* ---- ③e 演示编排：RD DEMO 之后的自动导览（相位内部推进） ---- */
        if (s_demo) {
            demo_tick(now);
        }

        /* ---- ③f 主机静默自恢复：上位机关闭/退出采集后会发 FSM STOP
         * 然后走人，板子必须自己回到巡扫，否则频谱快照冻结，圆屏整个
         * 「卡死」（2026-11-25 用户实测）。距上条主机 FSM 命令 ≥ 3 s
         * 且 FSM 停着 → 重跑自扫（页 3 钉住态则恢复单窗钉扫）。
         * 3 s 而不是更保守的 10 s：用户拍板「停止后 3 s 恢复本地运行」；
         * 实测误启无破坏性——主机 CAP/GAIN 等序列命令间隔 <1 s，调试台
         * 单步被误启后再 STOP 一次即可，无状态损坏。轮播开着时通常轮播
         * 切页副作用先恢复（≤8 s 内），此处兜底。恢复动作本身也是 FSM
         * 命令，会把计时重新归零，不会连发。 */
        if (!s_demo && scan_fsm_idle_ms() >= 3000) {
            if (s_view == RD_VIEW_WFALL && s_wf_pinned) {
                rd_pin_channel(s_wf_ch);
            } else {
                rd_auto_scan();
                s_wf_pinned = false;
                s_wf_tick = 0;
            }
        }

        /* ---- ④ 画一块并提交（DMA 异步，下次 submit 收口） ---- */
        int rows = s_band_rows;
        if (y + rows > RD_H) {
            rows = RD_H - y;
        }
        uint8_t *buf = NULL;
        size_t cap = 0;
        if (dsh_display_back_buffer(s_disp, &buf, &cap) != DSH_DISPLAY_OK) {
            state = RD_TR[state][RD_EV_FAULT];
            sub_err++;
            continue;
        }
        int64_t r0 = esp_timer_get_time();
        render_band(buf, y, rows);
        win_render_us += (uint32_t)(esp_timer_get_time() - r0);
        if (dsh_display_submit_band(s_disp, (uint16_t)y, (uint16_t)rows) != DSH_DISPLAY_OK) {
            state = RD_TR[state][RD_EV_FAULT];
            sub_err++;
            continue;
        }

        y += rows;
        if (y >= RD_H) {
            y = 0;
            s_frame_no++;
            win_frames++;
            heat_absorb();          /* 一整屏画完 = 结算一次热力图 */
        }

        /* ---- ⑤ fps 窗口 + 发布统计（每 1 s 一次，别每次都抢临界区） ---- */
        now = esp_timer_get_time();
        if (now - fps_t0 >= 1000000) {
            fps_x10 = win_frames ? (uint32_t)((int64_t)win_frames * 10000 / ((now - fps_t0) / 1000)) : 0;
            render_us = win_frames ? (win_render_us / win_frames) : 0;
            stats_publish(state, fps_x10, render_us, init_err, sub_err);
            fps_t0 = now;
            win_frames = 0;
            win_render_us = 0;
        }

        /* ---- ⑥ 让出：这是「不抢射频/串口」的关键，每块必让 ---- */
        vTaskDelay(1);
    }
    (void)boot_failed;
}

/* 开机自扫：本固件给普通人用，不能指望上位机先发 FSM START。
 * 分组按频段来，驻留 60 ms，跑满一圈约 3 s；上位机随时可 FSM STOP 接管。 */
/* 自扫固定增益的档位（PHY 表索引，不是 dB）。 */
#define RD_SCAN_GAIN 60
static round_ui_gain_fn s_gain_hook;

void round_ui_set_gain_hook(round_ui_gain_fn fn) { s_gain_hook = fn; }

/* 把扫描钉在 s_scan_ch[idx] 一个信道上：单窗计划 + 无限重复。
 * 与自扫同增益（RD_SCAN_GAIN），这样两边的功率码/占用率横向可比。 */
static void rd_pin_channel(int idx)
{
    if (RD_NSCAN <= 0) return;
    idx %= RD_NSCAN;
    if (idx < 0) idx += RD_NSCAN;
    s_wf_ch = idx;
    s_dbg_pin++;
    char cmd[48];
    if (s_gain_hook) s_gain_hook(RD_SCAN_GAIN);
    scan_fsm_command("FSM STREAM OFF");
    scan_fsm_command("FSMPLAN CLR");
    snprintf(cmd, sizeof cmd, "FSMPLAN ADD %u 60", (unsigned)s_scan_ch[idx]);
    scan_fsm_command(cmd);
    /* repeats=0 → 一直转；计划只有一条 → 永远采同一个窗 */
    scan_fsm_command("FSM START 512 1 0");
    /* 方案 A：换窗不清历史（清了 = 6 s 黑屏重填），改为在新数据起点打分隔线：
     * 线以下是本次测量，线以上是上一个信道留下的旧历史，64 行 ≈ 9 s 后被
     * 环形覆盖。FSM 重调谐的头 1~2 帧可能仍是旧信道频谱，落在分隔线下方
     * 1~2 行，7 行/s 的推进速度下可忽略。 */
    memset(s_wf_sep, 0, sizeof s_wf_sep);
    int sep_row = (int)((s_wf_head + 1u) % RD_WF_N);  /* 下一个要写的行 = 新数据起点 */
    s_wf_sep[sep_row >> 3] |= (uint8_t)(1u << (sep_row & 7));
    s_wf_tick = 0;
    s_wf_pinned = true;
}

static void rd_auto_scan(void)
{
    /* 信道表已提到模块作用域（s_scan_ch），这里只遍历，内容一字未改。 */
    char cmd[48];
    /* 圆心读的是「占用率」，而硬件 AGC 会把每个信道都拉到相近电平
     * （实测各信道 p50 全落 62~77 码）→ 必须固定增益，否则读数全挤在一起。
     * 用 60：与 AGENTS.md 记录的实测定稿一致，2.4G 忙段不会被推到饱和。
     * 主机若自己发 GAIN，以主机为准（这里只在开机自扫时设一次）。 */
    if (s_gain_hook) s_gain_hook(RD_SCAN_GAIN);
    /* 先关串口流：屏读本地快照，不抢串口带宽；主机要流自己 FSM STREAM ON */
    scan_fsm_command("FSM STREAM OFF");
    scan_fsm_command("FSMPLAN CLR");
    for (int i = 0; i < RD_NSCAN; i++) {
        snprintf(cmd, sizeof cmd, "FSMPLAN ADD %u 60", (unsigned)s_scan_ch[i]);
        scan_fsm_command(cmd);
    }
    scan_fsm_command("FSM START 512 1 0");
}

void round_ui_start(void)
{
    s_q_cmd = xQueueCreate(RD_CMD_DEPTH, sizeof(rd_cmd_t));
    if (!s_q_cmd) {
        return;
    }
    dsh_hal_gpio_out(RD_LED_GPIO, false);
    xTaskCreate(ui_task, "round_ui", RD_TASK_STACK, NULL, RD_TASK_PRIO, &s_task);
    /* 开机自扫不在这里发：它要等屏初始化成功之后由 ui_task 触发，
     * 否则会和 GC9A01 的初始化时序抢时间（见 ui_task 里 BOOT 出口）。 */
}

/* ---------------- 命令入口（app_main 任务上下文，唯一应答者） ---------------- */

static void cmd_send(uint8_t type, uint32_t arg)
{
    if (!s_q_cmd) {
        rd_reply("ERR rd_not_started\n");
        return;
    }
    rd_cmd_t c = { .type = type, .arg = arg };
    if (xQueueSend(s_q_cmd, &c, 0) != pdTRUE) {
        rd_reply("ERR rd_busy\n");
        return;
    }
    rd_reply("OK\n");
}

bool round_ui_command(char *line)
{
    if (!strcmp(line, "RD?")) {
        round_ui_stats_t st;
        round_ui_snapshot(&st);
        char out[192];
        snprintf(out, sizeof(out),
                 "RD state=%u ready=%u frames=%u fps=%u.%u pclk=%u band=%u render_us=%u "
                 "touch=%u,%u down=%u int=%d i2c=%u:%u:%02X ierr=%u serr=%u "
                 "heat=%u/%u peak=%u view=%u\n",
                 (unsigned)st.state, (unsigned)st.ready,
                 (unsigned)st.frames,
                 (unsigned)(st.fps_x10 / 10), (unsigned)(st.fps_x10 % 10),
                 (unsigned)st.pclk_hz, (unsigned)st.band_rows, (unsigned)st.render_us,
                 (unsigned)st.touch_x, (unsigned)st.touch_y, (unsigned)st.touch_down,
                 (int)st.touch_int_lvl,
                 (unsigned)st.touch_i2c_cnt, (unsigned)st.touch_chsc6x,
                 (unsigned)st.touch_first_addr,
                 (unsigned)st.init_errors, (unsigned)st.submit_errors,
                 (unsigned)st.heat_cells, (unsigned)RD_GRID, (unsigned)st.heat_peak,
                 (unsigned)st.view);
        rd_reply(out);
        return true;
    }
    unsigned v;
    unsigned sec;
    char extra;
    if (sscanf(line, "RD RATE %u %c", &v, &extra) == 1) {
        if (v < 1 || v > 80) {
            rd_reply("ERR rd_rate\n");
            return true;
        }
        cmd_send(RD_CMD_RATE, v * 1000000u);
        return true;
    }
    if (!strcmp(line, "RD RATE")) {
        rd_reply("ERR rd_rate\n");
        return true;
    }
    if (sscanf(line, "RD BAND %u %c", &v, &extra) == 1) {
        if (v < 8 || v > RD_H) {
            rd_reply("ERR rd_band\n");
            return true;
        }
        cmd_send(RD_CMD_BAND, v);
        return true;
    }
    if (!strcmp(line, "RD BAND")) {
        rd_reply("ERR rd_band\n");
        return true;
    }
    if (!strcmp(line, "RD OFF")) {
        cmd_send(RD_CMD_OFF, 0);
        return true;
    }
    if (!strcmp(line, "RD ON")) {
        cmd_send(RD_CMD_ON, 0);
        return true;
    }
    if (sscanf(line, "RD VIEW %u %c", &v, &extra) == 1) {
        if (v < RD_VIEW_COUNT) {
            cmd_send(RD_CMD_VIEW, v);
        } else {
            rd_reply("ERR rd_view_range\n");
        }
        return true;
    }
    /* RD VIEWSEC <view> <sec> / RD VIEWSEC?：每页轮播驻留秒数（用户 2026-11-25）。
     * 每页单独 0..255 s，到点强制切下一非零页；任一页 >0 即开轮播，三页全 0 = 关。
     * 默认 {8,25,8}（= 旧写死行为）。当前页 0 s 时 RD VIEW 切过去会被立即轮走，
     * 想停留看清某页就给它非零秒数。 */
    if (!strcmp(line, "RD VIEWSEC?")) {
        char out[64];
        int n = snprintf(out, sizeof(out), "RDVIEWSEC %u %u %u\n",
                         s_view_sec[0], s_view_sec[1], s_view_sec[2]);
        if (n > 0) rd_reply(out);
        return true;
    }
    if (sscanf(line, "RD VIEWSEC %u %u %c", &v, &sec, &extra) == 2) {
        if (v >= RD_VIEW_COUNT) {
            rd_reply("ERR rd_viewsec_view\n");
        } else if (sec > 255) {
            rd_reply("ERR rd_viewsec_sec\n");
        } else {
            cmd_send(RD_CMD_VIEWSEC, (v << 8) | sec);
        }
        return true;
    }
    /* 页 3 钉住的信道（1 基）：触摸上下滑是主路径，但固件侧没有可编程触摸源，
     * 这条命令让"换信道 -> 重钉 -> 打分隔线 -> 横轴跟着变"这件事能被脚本验证。 */
    if (sscanf(line, "RD WFCH %u %c", &v, &extra) == 1) {
        if (v < 1 || v > (unsigned)RD_NSCAN) {
            rd_reply("ERR rd_wfch_range\n");
        } else {
            cmd_send(RD_CMD_WFCH, v);
        }
        return true;
    }
    /* 调试：直接定 CHART 页号。触摸上下滑动是主路径，但固件侧没有可编程的
     * 触摸源，这条命令让翻页渲染本身能被脚本验证（7 页内容必须各不相同）。 */
    if (sscanf(line, "RD PAGE %u %c", &v, &extra) == 1) {
        cmd_send(RD_CMD_PAGE, v);
        return true;
    }
    /* 演示编排：RD DEMO 开始自动导览循环，RD DEMO OFF 停止并回到页 1。
     * 触摸在演示期间仍生效，但下一个相位会被编排接管；要彻底交还控制权就发 OFF。 */
    if (!strcmp(line, "RD DEMO")) {
        s_demo = true;
        demo_next(esp_timer_get_time(), 0);
        rd_reply("RDDEMO on\n");
        return true;
    }
    if (!strcmp(line, "RD DEMO OFF")) {
        s_demo = false;
        s_view = RD_VIEW_RING;      /* 若正停在页 3，副作用块会自动恢复整表自扫 */
        s_stats.view = s_view;
        s_page = 0;
        rd_reply("RDDEMO off\n");
        return true;
    }
    /* RD KNOWN CLR / RD KNOWN <mhz>：上位机做完 WiFi 扫描后，把"查得到
     * 名字的 AP"的中心频率逐条灌进来。固件据此把"有能量但不在名单里"
     * 的信道判为未知设备。 */
    /* RD KNOWN?：把固件里实际记着的名单回显出来。
     * 上位机下发完可以用它做闭环自检（"发了 13 条 / 固件认了 13 条"），
     * 屏上的 UNKNOWN 页也能间接对上。 */
    if (!strcmp(line, "RD KNOWN?")) {
        char out[96];
        int n = snprintf(out, sizeof(out),
                         "RDKNOWN n=%u max=%u first=%u last=%u\n",
                         (unsigned)s_known_n, (unsigned)RD_KNOWN_MAX,
                         (unsigned)(s_known_n ? s_known[0] : 0),
                         (unsigned)(s_known_n ? s_known[s_known_n - 1] : 0));
        if (n > 0) rd_reply(out);
        return true;
    }
    if (!strncmp(line, "RD KNOWN CLR", 12)) {
        s_known_n = 0;
        rd_reply("RDKNOWN cleared");
        return true;
    }
    if (sscanf(line, "RD KNOWN %u %c", &v, &extra) == 1) {
        if (s_known_n < RD_KNOWN_MAX) {
            s_known[s_known_n++] = (uint16_t)v;
            rd_reply("RDKNOWN add");
        } else {
            rd_reply("RDKNOWN full");
        }
        return true;
    }
    if (!strcmp(line, "RD VIEW")) {
        round_ui_stats_t st;
        round_ui_snapshot(&st);
        char out[80];
        int n = snprintf(out, sizeof(out), "RD view=%u of 0..%u "
                         "(0 ring 1 chart 2 waterfall)\n",
                         (unsigned)st.view, (unsigned)(RD_VIEW_COUNT - 1));
        if (n > 0) rd_reply(out);
        return true;
    }
    /* RD STATS?：UNKNOWN 页/CHART 页「全是空行」时的一眼诊断。
     * 只回数字，不上报名字，方便直接对比 s_dbg_noise 与 s_lvl 的量级。 */
    /* 诊断：A(B) 的原始计数。屏幕只看像素读不出具体数字（RD_ROW_STEP=4
     * 采样太粗），语义对不对只能靠这条回读。 */
    if (!strcmp(line, "RD AB?")) {
        int thr = 0, na = 0, nb = 0;
        alert_count(&thr, &na, &nb);
        char out[128];
        snprintf(out, sizeof out, "RDAB known=%u thr=%d A=%d B=%d",
                 (unsigned)s_known_n, thr, na, nb);
        rd_reply(out);
        return true;
    }
    /* RD ABD?：B（认不出的频段）明细，按强度降序。
     * 屏幕只显示 A(B) 一个数字，但要排查"哪个频段在异常收发"必须能定位。
     * 这里不做协议过滤：隐藏 SSID、模拟图传、蓝牙、微波炉都可能出现在
     * 名单外，它们正是报警的价值所在，不能当噪声滤掉。 */
    if (!strcmp(line, "RD ABD?")) {
        int thr = 0, na = 0, nb = 0;
        alert_count(&thr, &na, &nb);
        char out[256];
        int n = snprintf(out, sizeof out, "RDABD n=%d thr=%d A=%d", nb, thr, na);

        /* RD_NCH 只有 38，直接 O(n^2) 反复取最大，比搬一个通用排序省事 */
        bool used[RD_NCH];
        for (int i = 0; i < RD_NCH; i++) used[i] = false;
        for (int shown = 0; shown < MAX_LIST; shown++) {
            int best = -1, best_lv = thr - 1;
            for (int c = 0; c < RD_NCH; c++) {
                if (used[c] || known_near(s_ch_mhz[c])) continue;
                int lv = (int)ch_level(s_ch_mhz[c]);
                if (lv > best_lv) { best_lv = lv; best = c; }
            }
            if (best < 0) break;                 /* 再没有超阈值的了 */
            used[best] = true;
            if (n > (int)sizeof out - 20) break; /* 留够余量 */
            n += snprintf(out + n, sizeof out - (size_t)n, " %u:%d",
                          (unsigned)s_ch_mhz[best], best_lv);
        }
        rd_reply(out);
        return true;
    }
    if (!strcmp(line, "RD STATS?")) {
        int lmax = 0;
        long sum = 0;
        int nz = 0;
        for (int i = 0; i < RD_GRID; i++) {
            if (s_lvl[i] > lmax) lmax = s_lvl[i];
            if (s_lvl[i]) { sum += s_lvl[i]; nz++; }
        }
        char out[176];
        int og = freq_to_grid((uint64_t)s_win_mhz * 1000000ull);
        int occ = (og >= 0 && og < RD_GRID) ? (int)s_occ[og] : 0;
        int n = snprintf(out, sizeof(out),
                         "RDSTATS noise=%u lvl_max=%d lvl_avg=%ld lvl_nz=%d/%d "
                         "ch0=%u ch1=%u ch2=%u ch3=%u ch4=%u ch5=%u known=%u "
                         "occ=%d occwin=%u p50raw=%u rbins=%u rrate=%lu rnz=%u\n",
                         (unsigned)s_dbg_noise, lmax, nz ? sum / nz : 0L, nz, RD_GRID,
                         (unsigned)ch_heat_avg(s_ch_mhz[0]),
                         (unsigned)ch_heat_avg(s_ch_mhz[1]),
                         (unsigned)ch_heat_avg(s_ch_mhz[2]),
                         (unsigned)ch_heat_avg(s_ch_mhz[3]),
                         (unsigned)ch_heat_avg(s_ch_mhz[4]),
                         (unsigned)ch_heat_avg(s_ch_mhz[5]),
                         (unsigned)s_known_n, occ, (unsigned)s_win_mhz,
                         (unsigned)s_dbg_p50_raw, (unsigned)s_dbg_bins,
                         (unsigned long)s_dbg_rate, s_dbg_nz);
        if (n > 0) rd_reply(out);
        return true;
    }
    /* RD OCC? —— 逐信道占空比（0..100，-1 = 该格还没扫到），专用于定量校验圆心口径。
     * 回包：RDOCC <mhz>:<pct> ...
     * 缓冲必须够长：37 个信道 × 最坏 " 5825:100"（10 字节）= 375 字节 + 头。
     * 之前给 220，结果 5.7~5.8G 那一段被静默截掉，看起来像"没扫到"（-1）。 */
    if (!strcmp(line, "RD OCC?")) {
        char out[512];
        int n = snprintf(out, sizeof(out), "RDOCC");
        for (int c = 0; c < RD_NCH; c++) {
            int g = freq_to_grid((uint64_t)s_ch_mhz[c] * 1000000ull);
            int v = (g >= 0 && g < RD_GRID) ? (int)s_occ[g] : -1;
            if (n > (int)sizeof out - 16) break;
            n += snprintf(out + n, sizeof out - (size_t)n, " %u:%d",
                          (unsigned)s_ch_mhz[c], v);
        }
        snprintf(out + n, sizeof out - (size_t)n, "\n");
        rd_reply(out);
        return true;
    }
    /* RD RAW? —— 直接把圆屏正在用的那一帧原始码吐出来（定量核验用）。
     * 目的：确认 heat_absorb/圆心口径看到的数据和主机 SPEC 拿到的是不是同一
     * 个码尺度。真板实测两者比值一直在 2.2 附近且圆屏侧非常平，所以必须拿
     * 原始字节对账，不能靠推测。回包：RDRAW mhz= bins= rate= p10..p90= head= */
    if (!strcmp(line, "RD RAW?")) {
        unsigned bins = 0, mhz = 0, rate = 0;
        if (!spectrum_last_row(s_dbg_row, sizeof(s_dbg_row), &bins, &mhz, &rate) || !bins) {
            rd_reply("ERR rd_raw_none\n");
            return true;
        }
        if (bins > sizeof(s_dbg_row)) bins = sizeof(s_dbg_row);
        memset(s_dbg_hist, 0, sizeof(s_dbg_hist));
        for (unsigned i = 0; i < bins; i++) s_dbg_hist[s_dbg_row[i]]++;
        char out[240];
        int n = snprintf(out, sizeof(out),
                         "RDRAW mhz=%u bins=%u rate=%lu p10=%u p25=%u p50=%u p75=%u p90=%u head=",
                         (unsigned)mhz, bins, (unsigned long)rate,
                         (unsigned)hist_quantile(s_dbg_hist, bins, 1, 10),
                         (unsigned)hist_quantile(s_dbg_hist, bins, 1, 4),
                         (unsigned)hist_quantile(s_dbg_hist, bins, 1, 2),
                         (unsigned)hist_quantile(s_dbg_hist, bins, 3, 4),
                         (unsigned)hist_quantile(s_dbg_hist, bins, 9, 10));
        for (unsigned i = 0; i < 12 && n < (int)sizeof(out) - 5; i++)
            n += snprintf(out + n, sizeof(out) - (size_t)n, "%02X", s_dbg_row[i]);
        snprintf(out + n, sizeof out - (size_t)n, "\n");
        rd_reply(out);
        return true;
    }
    if (!strcmp(line, "RD CLEAR")) {
        cmd_send(RD_CMD_CLEAR, 0);
        return true;
    }
    if (!strcmp(line, "RD RINGDBG")) {
        char out[200];
        /* 定量判据（不靠人眼）：rl_* 是 RING 电平的 max/中位数，
         * warm = 176 块里调色板索引 >120（黄/橙/红段）的块数。
         * 「环带一片蓝」在这三个数上表现为 rl_mid 很小、warm 接近 0。 */
        uint8_t rl_max = 0;
        uint32_t rl_sum = 0;
        int rl_n = 0, warm = 0;
        for (int b = 0; b < RD_BLOCKS; b++) {
            uint8_t a = s_ring_lvl[2 * b], c2 = s_ring_lvl[2 * b + 1];
            uint8_t m = a > c2 ? a : c2;
            if (m > rl_max) rl_max = m;
            if (m) { rl_sum += m; rl_n++; }
            uint32_t x = (uint32_t)m * 2u;
            if (x > 255u) x = 255u;
            if (x > 120u) warm++;
        }
        int n = snprintf(out, sizeof(out),
                         "RDRING pmax=%u cells=%u cell0=%u rl_max=%u rl_mid=%u warm=%d "
                         "b=%u len=%u col=%04X idx=",
                         (unsigned)s_dbg_pmax, (unsigned)s_stats.heat_cells,
                         (unsigned)s_heat[0], (unsigned)rl_max,
                         (unsigned)(rl_n ? rl_sum / (uint32_t)rl_n : 0), warm,
                         (unsigned)s_dbg_b, (unsigned)s_dbg_len, (unsigned)s_dbg_col);
        int shown = 0;
        for (int g = 0; g < RD_GRID && shown < 8; g++) {
            if (s_heat[g]) {
                n += snprintf(out + n, sizeof(out) - n, "%d:%u(blk%d) ", g, (unsigned)s_heat[g], g / 2);
                shown++;
            }
        }
        if (n < (int)sizeof(out) - 1) { out[n++] = '\n'; out[n] = 0; }
        rd_reply(out);
        return true;
    }

    /* 诊断：回读第 y 行真实渲染结果（30 点，每 8 px 一个，取 RGB565 高字节）。
     * 摄像头拍不清 5x7 点阵，这条命令提供不依赖摄像头的渲染证据。 */
    if (sscanf(line, "RD ROW %u %c", &v, &extra) == 1) {
        if (v < RD_H) {
            s_probe_done = 0;
            s_probe_y = (int16_t)v;
            vTaskDelay(pdMS_TO_TICKS(150));   /* 等一两帧渲染 */
            if (s_probe_done) {
                char out[16 + RD_ROW_N * 3];
                int n = snprintf(out, sizeof(out), "RDROW y=%u", (unsigned)v);
                for (int i = 0; i < RD_ROW_N && n < (int)sizeof(out) - 4; i++)
                    n += snprintf(out + n, sizeof(out) - n, " %02X", s_row_q[i]);
                if (n < (int)sizeof(out) - 1) { out[n++] = '\n'; out[n] = 0; }
                rd_reply(out);
            } else {
                rd_reply("ERR rd_row_timeout\n");
            }
            s_probe_y = -1;
        } else {
            rd_reply("ERR rd_row_range\n");
        }
        return true;
    }

    /* 诊断（临时）：触摸手势计数，见 s_dbg_* 注释。查完即删。 */
    if (!strcmp(line, "RD TCH?")) {
        char out[192];
        snprintf(out, sizeof(out),
                 "RDTCH press=%u release=%u chart=%u wfs=%u pin=%u "
                 "start=%u,%u end=%u,%u view=%u pinned=%u ch=%u "
                 "sep=%02X%02X%02X%02X%02X%02X%02X%02X head=%u\n",
                 (unsigned)s_dbg_press, (unsigned)s_dbg_release,
                 (unsigned)s_dbg_chart, (unsigned)s_dbg_wfs, (unsigned)s_dbg_pin,
                 (unsigned)s_dbg_x0, (unsigned)s_dbg_y0,
                 (unsigned)s_dbg_x1, (unsigned)s_dbg_y1,
                 (unsigned)s_view, s_wf_pinned ? 1u : 0u, (unsigned)(s_wf_ch + 1),
                 s_wf_sep[0], s_wf_sep[1], s_wf_sep[2], s_wf_sep[3],
                 s_wf_sep[4], s_wf_sep[5], s_wf_sep[6], s_wf_sep[7],
                 (unsigned)s_wf_head);
        rd_reply(out);
        return true;
    }

    /* 诊断（临时）：直接吐历史缓冲某个槽的内容，排查瀑布黑带用。
     * RD WFS <slot>  -> RDWFS slot=%u %02X... （16 列） */
    {
        unsigned slot = 0;
        char ex = 0;
        if (sscanf(line, "RD WFS %u %c", &slot, &ex) == 1 && slot < RD_WF_N) {
            char out[96];
            int n = snprintf(out, sizeof(out), "RDWFS slot=%u", slot);
            for (int i = 0; i < 16 && n < (int)sizeof(out) - 4; i++)
                n += snprintf(out + n, sizeof(out) - n, " %02X", s_wf[slot][i]);
            if (n < (int)sizeof(out) - 1) { out[n++] = '\n'; out[n] = 0; }
            rd_reply(out);
            return true;
        }
    }

    /* 诊断：回当前视图的数据源采样（24 点），用于外部客观核对"屏上画的是什么"。
     * 摄像头受对焦/距离/白平衡限制，看不清 5x7 点阵，所以数据真相直接从固件取。 */
    if (!strcmp(line, "RD VIEWDATA")) {
        uint8_t v[24];
        uint8_t view;
        taskENTER_CRITICAL(&s_stats_lock);
        view = s_view;
        if (view == RD_VIEW_CHART) {
            /* 当前页的信道值放最前，其余放全频段降采样，
             * 这样外部一次读就能同时核对"当前页"和"全频段是否都有值"。
             * 每页几个必须跟 RD_CH_PER_PAGE 走：这里原来写死 4，而屏上每页显示 5，
             * 于是翻到第 2 页时回包的第 1 个数是第 4 个信道、屏上第 1 行是第 5 个
             * —— 差一位，外部核对会得出"屏和固件不一致"的错误结论（纯诊断量）。 */
            for (int i = 0; i < RD_CH_PER_PAGE; i++) {
                int idx = (int)s_page * RD_CH_PER_PAGE + i;
                v[i] = (idx < RD_NCH) ? s_ch_val[idx] : 0;
            }
            for (int i = RD_CH_PER_PAGE; i < 24; i++)
                v[i] = s_ch_val[(i - RD_CH_PER_PAGE) * RD_NCH / (24 - RD_CH_PER_PAGE)];
        } else if (view == RD_VIEW_WFALL) {
            for (int i = 0; i < 24; i++) v[i] = s_wf[s_wf_head][i * RD_WF_N / 24];
        } else {
            for (int i = 0; i < 24; i++) v[i] = s_heat[i * RD_GRID / 24];
        }
        taskEXIT_CRITICAL(&s_stats_lock);
        char out[192];
        int n = snprintf(out, sizeof(out), "RDDATA view=%u sh=%u tk=%u",
                         (unsigned)view, (unsigned)s_wf_head, (unsigned)s_wf_tick);
        for (int i = 0; i < 24 && n < (int)sizeof(out) - 5; i++)
            n += snprintf(out + n, sizeof(out) - n, " %02X", v[i]);
        if (n < (int)sizeof(out) - 1) { out[n++] = '\n'; out[n] = 0; }
        rd_reply(out);
        return true;
    }

    /* 诊断：回显 render_band 实际写进 buffer 的字节。 */
    if (!strcmp(line, "RD DUMP")) {
        round_ui_stats_t st;
        round_ui_snapshot(&st);
        char out[192];
        int n = snprintf(out, sizeof(out),
                         "RDDUMP fill=%04X n=%u buf=%02X,%02X,%02X,%02X,%02X,%02X,%02X,%02X"
                         " band=%u state=%u frames=%u sub_err=%u pclk=%u\n",
                         (unsigned)s_dbg_fill, (unsigned)s_dbg_n, s_dbg_buf[0], s_dbg_buf[1],
                         s_dbg_buf[2], s_dbg_buf[3], s_dbg_buf[4], s_dbg_buf[5], s_dbg_buf[6],
                         s_dbg_buf[7], (unsigned)st.band_rows, (unsigned)st.state,
                         (unsigned)st.frames, (unsigned)st.submit_errors, (unsigned)st.pclk_hz);
        if (n > 0) rd_reply(out);
        return true;
    }
    /* 调试：整屏填纯色。RD FILL FF00FF = 品红；RD FILL 0 = 恢复正常渲染。 */
    if (!strncmp(line, "RD FILL ", 8)) {
        uint32_t v = (uint32_t)strtoul(line + 8, NULL, 16);
        cmd_send(RD_CMD_FILL, v);
        return true;
    }
    /* 触摸原始字节诊断：RD TOUCHRAW
     * 回答「read 为什么一直 false」——是被 INT 拦掉(1)、I2C 失败(2)，
     * 还是读到了但状态字节不等于 0x01(0)。 */
    if (!strcmp(line, "RD TOUCHRAW")) {
        char out[192];
        round_ui_stats_t st;
        round_ui_snapshot(&st);
        const uint8_t *rw = dsh_touch_last_raw();
        snprintf(out, sizeof(out),
                 "TOUCHRAW stage=%d int=%d raw=%02X,%02X,%02X,%02X,%02X "
                 "down=%u x=%u y=%u present=%u\n",
                 dsh_touch_last_stage(),
                 (int)gpio_get_level((gpio_num_t)RD_TOUCH_INT),
                 rw[0], rw[1], rw[2], rw[3], rw[4],
                 (unsigned)st.touch_down,
                 (unsigned)st.touch_x, (unsigned)st.touch_y,
                 dsh_touch_present() ? 1u : 0u);
        rd_reply(out);
        return true;
    }
    /* 反复采样：RD TOUCHWATCH <n> —— 连续 n 次 read，打印每次的 stage/raw，
     * 用来观察「按住屏幕时原始字节到底怎么变」。n 上限 40。 */
    if (!strncmp(line, "RD TOUCHWATCH", 13)) {
        unsigned n = 8;
        char tail;
        if (sscanf(line + 13, " %u %c", &n, &tail) != 1) {
            n = 8;
        }
        if (n > 40) {
            n = 40;
        }
        char out[160];
        for (unsigned i = 0; i < n; ++i) {
            uint16_t tx = 0, ty = 0;
            bool dn = dsh_touch_read(&tx, &ty);
            const uint8_t *rw = dsh_touch_last_raw();
            snprintf(out, sizeof(out),
                     "W%02u st=%d int=%d raw=%02X,%02X,%02X,%02X,%02X dn=%u x=%u y=%u\n",
                     i, dsh_touch_last_stage(),
                     (int)gpio_get_level((gpio_num_t)RD_TOUCH_INT),
                     rw[0], rw[1], rw[2], rw[3], rw[4],
                     dn ? 1u : 0u, (unsigned)tx, (unsigned)ty);
            rd_reply(out);
            vTaskDelay(pdMS_TO_TICKS(60));
        }
        rd_reply("TOUCHWATCH END\n");
        return true;
    }
    /* 内存体检：静态段的余量（sram_guard 断言）不等于运行时余量，
     * 这里报 heap 的真实可用量 + 栈高水位 + PSRAM 到底识别出多少。 */
    if (!strcmp(line, "RD HEAP")) {
        char out[192];
        unsigned stack_hwm = s_task ? (unsigned)uxTaskGetStackHighWaterMark(s_task) : 0u;
        snprintf(out, sizeof(out),
                 "RD HEAP int_free=%u int_min=%u int_total=%u spiram_free=%u spiram_total=%u "
                 "task_stack_hwm=%u\n",
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_total_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                 (unsigned)heap_caps_get_total_size(MALLOC_CAP_SPIRAM),
                 stack_hwm);
        rd_reply(out);
        return true;
    }
    return false;
}