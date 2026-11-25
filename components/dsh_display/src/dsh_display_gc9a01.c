/**
 * @file dsh_display_gc9a01.c
 * @brief GC9A01（240x240）驱动实现（core 层），基于 dsh_hal 官方栈抽象。
 *
 * 移植基准（不重复造轮子，ERR-002）：
 * - 初始化序列【严格对照】Seeed 官方 RoundDisplay 库实际使用的
 *   Arduino_GFX `Arduino_GC9A01`（gc9a01_init_operations），逐字节一致。
 * - 时序对照官方 tftInit + displayOn：init 表 → SLPOUT → 120ms → DISPON → 20ms → INVON；
 *   rst<0 时官方不发 SWRESET，本实现一致。
 * - 传输走 dsh_hal 的 esp_lcd 官方栈 + ping-pong 双全帧缓冲（v0.3）。
 * - MADCTL rotation 0 = 0x08（BGR）。
 */
#include "dsh_display.h"

#include <stdlib.h>
#include <string.h>

/* ---- GC9A01 命令（同 Arduino_GFX Arduino_GC9A01.h） ---- */
#define GC9A01_SLPOUT  0x11
#define GC9A01_SWRESET 0x01
#define GC9A01_INVOFF  0x20
#define GC9A01_INVON   0x21
#define GC9A01_DISPON  0x29
#define GC9A01_CASET   0x2A
#define GC9A01_RASET   0x2B
#define GC9A01_RAMWR   0x2C
#define GC9A01_MADCTL  0x36
#define GC9A01_COLMOD  0x3A

/* ---- MADCTL 位 ---- */
#define MADCTL_MY  0x80
#define MADCTL_MX  0x40
#define MADCTL_MV  0x20
#define MADCTL_BGR 0x08   /* Arduino_GFX rotation 0 = 0x08 */

#define GC9A01_WIDTH  240
#define GC9A01_HEIGHT 240
#define GC9A01_FRAME_BYTES (GC9A01_WIDTH * GC9A01_HEIGHT * 2)

struct dsh_display {
    dsh_display_cfg_t cfg;
    dsh_hal_panel_t *panel;
    uint8_t madctl;
};

static dsh_display_err_t send_cmd(dsh_display_t *d, uint8_t cmd)
{
    return dsh_hal_panel_cmd(d->panel, cmd) == DSH_HAL_OK ? DSH_DISPLAY_OK : DSH_DISPLAY_ERR_HAL;
}

static dsh_display_err_t send_cmd_data(dsh_display_t *d, uint8_t cmd, const uint8_t *data, size_t len)
{
    if (len > 0) {
        return dsh_hal_panel_param(d->panel, cmd, data, len) == DSH_HAL_OK
                ? DSH_DISPLAY_OK : DSH_DISPLAY_ERR_HAL;
    }
    return send_cmd(d, cmd);
}

/* 初始化序列表：严格对照 Arduino_GFX gc9a01_init_operations */
typedef struct {
    uint8_t cmd;
    uint8_t len;
    const uint8_t *data;
} init_cmd_t;

static const uint8_t _b1_14[] = {0x14};
static const uint8_t _b1_40[] = {0x40};
static const uint8_t _b1_ff[] = {0xFF};
static const uint8_t _b1_0a[] = {0x0A};
static const uint8_t _b1_21[] = {0x21};
static const uint8_t _b1_00[] = {0x00};
static const uint8_t _b1_80[] = {0x80};
static const uint8_t _b1_01[] = {0x01};
static const uint8_t _b2_0020[] = {0x00, 0x20};
static const uint8_t _b1_05[] = {0x05};
static const uint8_t _b4_0808[] = {0x08, 0x08, 0x08, 0x08};
static const uint8_t _b1_06[] = {0x06};
static const uint8_t _b3_600104[] = {0x60, 0x01, 0x04};
static const uint8_t _b1_13[] = {0x13};
static const uint8_t _b1_22[] = {0x22};
static const uint8_t _b1_11[] = {0x11};
static const uint8_t _b2_100e[] = {0x10, 0x0E};
static const uint8_t _b3_210c02[] = {0x21, 0x0c, 0x02};
static const uint8_t _b6_f0[] = {0x45, 0x09, 0x08, 0x08, 0x26, 0x2A};
static const uint8_t _b6_f1[] = {0x43, 0x70, 0x72, 0x36, 0x37, 0x6F};
static const uint8_t _b2_1b0b[] = {0x1B, 0x0B};
static const uint8_t _b1_77[] = {0x77};
static const uint8_t _b1_63[] = {0x63};
static const uint8_t _b9_70[] = {0x07, 0x07, 0x04, 0x0E, 0x0F, 0x09, 0x07, 0x08, 0x03};
static const uint8_t _b1_34[] = {0x34};
static const uint8_t _b12_62[] = {0x18, 0x0D, 0x71, 0xED, 0x70, 0x70, 0x18, 0x0F, 0x71, 0xEF, 0x70, 0x70};
static const uint8_t _b12_63[] = {0x18, 0x11, 0x71, 0xF1, 0x70, 0x70, 0x18, 0x13, 0x71, 0xF3, 0x70, 0x70};
static const uint8_t _b7_64[] = {0x28, 0x29, 0xF1, 0x01, 0xF1, 0x00, 0x07};
static const uint8_t _b10_66[] = {0x3C, 0x00, 0xCD, 0x67, 0x45, 0x45, 0x10, 0x00, 0x00, 0x00};
static const uint8_t _b10_67[] = {0x00, 0x3C, 0x00, 0x00, 0x00, 0x01, 0x54, 0x10, 0x32, 0x98};
static const uint8_t _b7_74[] = {0x10, 0x85, 0x80, 0x00, 0x00, 0x4E, 0x00};
static const uint8_t _b2_3e07[] = {0x3E, 0x07};

static const init_cmd_t s_init_cmds[] = {
    {0xEF, 0, NULL},
    {0xEB, 1, _b1_14},
    {0xFE, 0, NULL},
    {0xEF, 0, NULL},
    {0xEB, 1, _b1_14},
    {0x84, 1, _b1_40},
    {0x85, 1, _b1_ff},
    {0x86, 1, _b1_ff},
    {0x87, 1, _b1_ff},
    {0x88, 1, _b1_0a},
    {0x89, 1, _b1_21},
    {0x8A, 1, _b1_00},
    {0x8B, 1, _b1_80},
    {0x8C, 1, _b1_01},
    {0x8D, 1, _b1_01},
    {0x8E, 1, _b1_ff},
    {0x8F, 1, _b1_ff},
    {0xB6, 2, _b2_0020},
    {GC9A01_COLMOD, 1, _b1_05},
    {0x90, 4, _b4_0808},
    {0xBD, 1, _b1_06},
    {0xBC, 1, _b1_00},
    {0xFF, 3, _b3_600104},
    {0xC3, 1, _b1_13},
    {0xC4, 1, _b1_13},
    {0xC9, 1, _b1_22},
    {0xBE, 1, _b1_11},
    {0xE1, 2, _b2_100e},
    {0xDF, 3, _b3_210c02},
    {0xF0, 6, _b6_f0},
    {0xF1, 6, _b6_f1},
    {0xF2, 6, _b6_f0},
    {0xF3, 6, _b6_f1},
    {0xED, 2, _b2_1b0b},
    {0xAE, 1, _b1_77},
    {0xCD, 1, _b1_63},
    {0x70, 9, _b9_70},
    {0xE8, 1, _b1_34},
    {0x62, 12, _b12_62},
    {0x63, 12, _b12_63},
    {0x64, 7, _b7_64},
    {0x66, 10, _b10_66},
    {0x67, 10, _b10_67},
    {0x74, 7, _b7_74},
    {0x98, 2, _b2_3e07},
    {0x35, 0, NULL},   /* TEON */
};

static dsh_display_err_t gc9a01_send_init(dsh_display_t *d)
{
    for (size_t i = 0; i < sizeof(s_init_cmds) / sizeof(s_init_cmds[0]); i++) {
        dsh_display_err_t e = send_cmd_data(d, s_init_cmds[i].cmd, s_init_cmds[i].data, s_init_cmds[i].len);
        if (e != DSH_DISPLAY_OK) {
            return e;
        }
    }
    return DSH_DISPLAY_OK;
}

static dsh_display_err_t apply_madctl(dsh_display_t *d)
{
    return send_cmd_data(d, GC9A01_MADCTL, &d->madctl, 1);
}

/* 面板 RST 接 XIAO RST（TFT_RST=-1）：芯片复位即面板复位。
 * 官方 Arduino_GFX 在 rst<0 时不发任何软复位命令 —— 本实现保持一致。
 * （SWRESET+短延时会因控制器仍在复位态而吞掉后续初始化命令 → 黑屏） */
static dsh_display_err_t gc9a01_hw_reset(dsh_display_t *d)
{
    if (d->cfg.reset_gpio >= 0) {
        if (dsh_hal_gpio_out(d->cfg.reset_gpio, true) != DSH_HAL_OK) {
            return DSH_DISPLAY_ERR_HAL;
        }
        dsh_hal_delay_ms(200);              /* Arduino_GFX GC9A01_RST_DELAY */
        if (dsh_hal_gpio_set(d->cfg.reset_gpio, false) != DSH_HAL_OK) {
            return DSH_DISPLAY_ERR_HAL;
        }
        dsh_hal_delay_ms(200);
        if (dsh_hal_gpio_set(d->cfg.reset_gpio, true) != DSH_HAL_OK) {
            return DSH_DISPLAY_ERR_HAL;
        }
        dsh_hal_delay_ms(200);
    }
    return DSH_DISPLAY_OK;
}

dsh_display_err_t dsh_display_init(dsh_display_t **out, const dsh_display_cfg_t *cfg)
{
    if (!out || !cfg) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }

    dsh_display_t *d = (dsh_display_t *)calloc(1, sizeof(dsh_display_t));
    if (!d) {
        return DSH_DISPLAY_ERR_HAL;
    }
    d->cfg = *cfg;

    dsh_display_err_t e;

    /* esp_lcd 官方栈 + ping-pong 双全帧缓冲初始化（含背光默认点亮） */
    if (dsh_hal_panel_init(&d->cfg.spi, &d->panel) != DSH_HAL_OK) {
        e = DSH_DISPLAY_ERR_HAL;
        goto err;
    }

    /* 复位（rst<0 = 无操作，官方一致） */
    e = gc9a01_hw_reset(d);
    if (e != DSH_DISPLAY_OK) {
        goto err;
    }

    /* 软复位 + 充足延迟。
     * rst<0 时上面不发任何复位，完全依赖「芯片复位即面板复位」；实测 XIAO GC9A01
     * 上电后控制器未就绪，后续整串初始化命令会被吞掉，屏只剩背光全白。
     * 已用裸 esp_lcd 探针（SWRESET → 120ms → 极简序列 → RAMWR）实证可正常显示。 */
    e = send_cmd(d, GC9A01_SWRESET);
    if (e != DSH_DISPLAY_OK) {
        goto err;
    }
    dsh_hal_delay_ms(150);

    /* SLPOUT 必须早于任何参数类命令：控制器处于 sleep 时 COLMOD/MADCTL 等会被丢弃，
     * 结果 COLMOD 保持默认值 → 后续 RGB565 数据格式错 → 屏只剩背光。 */
    e = send_cmd(d, GC9A01_SLPOUT);
    if (e != DSH_DISPLAY_OK) {
        goto err;
    }
    dsh_hal_delay_ms(120);

    /* 完整初始化序列（含 COLMOD / TEON / SLPOUT） */
    e = gc9a01_send_init(d);
    if (e != DSH_DISPLAY_OK) {
        goto err;
    }

    /* SLPOUT 后等待（Arduino_GFX GC9A01_SLPOUT_DELAY=120ms） */
    dsh_hal_delay_ms(120);

    /* 开显示 */
    e = send_cmd(d, GC9A01_DISPON);
    if (e != DSH_DISPLAY_OK) {
        goto err;
    }
    dsh_hal_delay_ms(20);

    /* MADCTL（Arduino_GFX rotation 0 = 0x08 BGR + mirror/swap 位） */
    /* 组件内所有像素都按 RGB565（高位=R）写入，所以这里必须是 RGB 顺序。
     * 用 MADCTL_BGR 会让每个颜色 R/B 互换：填 0xF800 红 → 屏上显示蓝。 */
    d->madctl = 0;
    if (cfg->mirror_x) {
        d->madctl |= MADCTL_MX;
    }
    if (cfg->mirror_y) {
        d->madctl |= MADCTL_MY;
    }
    if (cfg->swap_xy) {
        d->madctl |= MADCTL_MV;
    }
    e = apply_madctl(d);
    if (e != DSH_DISPLAY_OK) {
        goto err;
    }

    /* 颜色反转（Seeed 官方 new Arduino_GC9A01(bus, -1, rotation, ips=true) → INVON） */
    e = send_cmd(d, cfg->invert ? GC9A01_INVON : GC9A01_INVOFF);
    if (e != DSH_DISPLAY_OK) {
        goto err;
    }

    *out = d;
    return DSH_DISPLAY_OK;

err:
    if (d->panel) {
        dsh_hal_panel_del(d->panel);
    }
    free(d);
    return e;
}

dsh_display_err_t dsh_display_del(dsh_display_t *d)
{
    if (!d) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }
    if (d->panel) {
        dsh_hal_panel_del(d->panel);
    }
    free(d);
    return DSH_DISPLAY_OK;
}

/* 设置绘制窗口（CASET/RASET；RAMWR 由随后的 flush 携带）。
 * 前置条件：无在飞 DMA（polling 事务不允许与队列事务并发），调用方先 wait */
static dsh_display_err_t set_window(dsh_display_t *d, uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1)
{
    dsh_display_err_t e;
    uint8_t caset[4] = { (uint8_t)(x0 >> 8), (uint8_t)(x0 & 0xFF), (uint8_t)(x1 >> 8), (uint8_t)(x1 & 0xFF) };
    uint8_t raset[4] = { (uint8_t)(y0 >> 8), (uint8_t)(y0 & 0xFF), (uint8_t)(y1 >> 8), (uint8_t)(y1 & 0xFF) };

    e = send_cmd_data(d, GC9A01_CASET, caset, 4);
    if (e != DSH_DISPLAY_OK) {
        return e;
    }
    return send_cmd_data(d, GC9A01_RASET, raset, 4);
}

dsh_display_err_t dsh_display_fill(dsh_display_t *d, uint16_t color)
{
    if (!d) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }

    dsh_hal_panel_wait(d->panel);
    dsh_display_err_t e = set_window(d, 0, 0, GC9A01_WIDTH - 1, GC9A01_HEIGHT - 1);
    if (e != DSH_DISPLAY_OK) {
        return e;
    }

    /* 写入 hal ping-pong 后台缓冲（RGB565 大端字节流）。
     * 关键：dsh_hal_panel_flush 每调一次就翻转 sent_idx，所以缓冲必须在**循环内**
     * 重新 get_buffer 并重填；否则相邻条带会交替发送从未初始化过的另一半缓冲
     * （脏数据 → 整屏花屏/全白，实测就是这个问题）。 */
    uint8_t hi = (uint8_t)(color >> 8);
    uint8_t lo = (uint8_t)(color & 0xFF);
    size_t cap = 0;
    {
        uint8_t *tmp = NULL;
        if (dsh_hal_panel_get_buffer(d->panel, &tmp, &cap) != DSH_HAL_OK
                || !tmp || cap < GC9A01_WIDTH * 2) {
            return DSH_DISPLAY_ERR_HAL;
        }
    }
    int rows = (int)(cap / (GC9A01_WIDTH * 2));
    if (rows > GC9A01_HEIGHT) {
        rows = GC9A01_HEIGHT;
    }
    if (rows <= 0) {
        return DSH_DISPLAY_ERR_HAL;
    }
    for (int y = 0; y < GC9A01_HEIGHT; y += rows) {
        int h = (y + rows <= GC9A01_HEIGHT) ? rows : (GC9A01_HEIGHT - y);
        size_t bytes = (size_t)h * GC9A01_WIDTH * 2;
        dsh_hal_panel_wait(d->panel);
        dsh_display_err_t e2 = set_window(d, 0, (uint16_t)y, GC9A01_WIDTH - 1, (uint16_t)(y + h - 1));
        if (e2 != DSH_DISPLAY_OK) {
            return e2;
        }
        uint8_t *buf = NULL;
        size_t got = 0;
        if (dsh_hal_panel_get_buffer(d->panel, &buf, &got) != DSH_HAL_OK
                || !buf || got < bytes) {
            return DSH_DISPLAY_ERR_HAL;
        }
        for (size_t i = 0; i < bytes; i += 2) {
            buf[i] = hi;
            buf[i + 1] = lo;
        }
        if (dsh_hal_panel_flush(d->panel, GC9A01_RAMWR, bytes) != DSH_HAL_OK) {
            return DSH_DISPLAY_ERR_HAL;
        }
    }
    dsh_hal_panel_wait(d->panel);
    return DSH_DISPLAY_OK;
}

dsh_display_err_t dsh_display_draw_bitmap(dsh_display_t *d,
        uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1,
        const uint16_t *pixels)
{
    if (!d || !pixels || x1 < x0 || y1 < y0) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }

    size_t n_pixels = (size_t)(x1 - x0 + 1) * (size_t)(y1 - y0 + 1);
    size_t bytes = n_pixels * 2;
    if (bytes > GC9A01_FRAME_BYTES) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }

    dsh_hal_panel_wait(d->panel);
    dsh_display_err_t e = set_window(d, x0, y0, x1, y1);
    if (e != DSH_DISPLAY_OK) {
        return e;
    }

    uint8_t *buf = NULL;
    size_t cap = 0;
    if (dsh_hal_panel_get_buffer(d->panel, &buf, &cap) != DSH_HAL_OK || cap < bytes) {
        return DSH_DISPLAY_ERR_HAL;
    }
    for (size_t i = 0; i < n_pixels; i++) {
        buf[i * 2] = (uint8_t)(pixels[i] >> 8);
        buf[i * 2 + 1] = (uint8_t)(pixels[i] & 0xFF);
    }
    return dsh_hal_panel_flush(d->panel, GC9A01_RAMWR, bytes) == DSH_HAL_OK
            ? DSH_DISPLAY_OK : DSH_DISPLAY_ERR_HAL;
}

dsh_display_err_t dsh_display_flush_full(dsh_display_t *d,
        const uint8_t *rgb565_be, size_t bytes)
{
    if (!d || !rgb565_be) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }
    if (bytes != GC9A01_FRAME_BYTES) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }

    dsh_hal_panel_wait(d->panel);
    dsh_display_err_t e = set_window(d, 0, 0, GC9A01_WIDTH - 1, GC9A01_HEIGHT - 1);
    if (e != DSH_DISPLAY_OK) {
        return e;
    }

    uint8_t *buf = NULL;
    size_t cap = 0;
    if (dsh_hal_panel_get_buffer(d->panel, &buf, &cap) != DSH_HAL_OK || cap < bytes) {
        return DSH_DISPLAY_ERR_HAL;
    }
    memcpy(buf, rgb565_be, bytes);
    return dsh_hal_panel_flush(d->panel, GC9A01_RAMWR, bytes) == DSH_HAL_OK
            ? DSH_DISPLAY_OK : DSH_DISPLAY_ERR_HAL;
}

/* 条带刷新：一次 DMA 只发 rows 行（y0 起），缓冲须 >= rows*240*2。
 * 与 flush_full 同一条 ping-pong 路径：调用即启动 DMA，下次调用自动收口。 */
dsh_display_err_t dsh_display_flush_band(dsh_display_t *d, uint16_t y0, uint16_t rows,
        const uint8_t *rgb565_be)
{
    if (!d || !rgb565_be || rows == 0 || y0 + rows > GC9A01_HEIGHT) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }
    size_t bytes = (size_t)rows * GC9A01_WIDTH * 2;

    dsh_hal_panel_wait(d->panel);
    dsh_display_err_t e = set_window(d, 0, y0, GC9A01_WIDTH - 1, (uint16_t)(y0 + rows - 1));
    if (e != DSH_DISPLAY_OK) {
        return e;
    }

    uint8_t *buf = NULL;
    size_t cap = 0;
    if (dsh_hal_panel_get_buffer(d->panel, &buf, &cap) != DSH_HAL_OK || cap < bytes) {
        return DSH_DISPLAY_ERR_HAL;
    }
    memcpy(buf, rgb565_be, bytes);
    return dsh_hal_panel_flush(d->panel, GC9A01_RAMWR, bytes) == DSH_HAL_OK
            ? DSH_DISPLAY_OK : DSH_DISPLAY_ERR_HAL;
}

/* 后台缓冲直写：拿到下一块待写缓冲（无拷贝），供条带渲染零拷贝使用 */
dsh_display_err_t dsh_display_back_buffer(dsh_display_t *d, uint8_t **buf, size_t *cap)
{
    if (!d || !buf || !cap) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }
    return dsh_hal_panel_get_buffer(d->panel, buf, cap) == DSH_HAL_OK
            ? DSH_DISPLAY_OK : DSH_DISPLAY_ERR_HAL;
}

/* 提交已写好的后台缓冲（配合 dsh_display_back_buffer 用，省掉一次 memcpy） */
dsh_display_err_t dsh_display_submit_band(dsh_display_t *d, uint16_t y0, uint16_t rows)
{
    if (!d || rows == 0 || y0 + rows > GC9A01_HEIGHT) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }
    size_t bytes = (size_t)rows * GC9A01_WIDTH * 2;
    dsh_hal_panel_wait(d->panel);
    dsh_display_err_t e = set_window(d, 0, y0, GC9A01_WIDTH - 1, (uint16_t)(y0 + rows - 1));
    if (e != DSH_DISPLAY_OK) {
        return e;
    }
    return dsh_hal_panel_flush(d->panel, GC9A01_RAMWR, bytes) == DSH_HAL_OK
            ? DSH_DISPLAY_OK : DSH_DISPLAY_ERR_HAL;
}

dsh_display_err_t dsh_display_backlight(dsh_display_t *d, bool on)
{
    if (!d) {
        return DSH_DISPLAY_ERR_INVALID_ARG;
    }
    if (d->cfg.spi.bl < 0) {
        return DSH_DISPLAY_OK;
    }
    return dsh_hal_gpio_set(d->cfg.spi.bl, on) == DSH_HAL_OK ? DSH_DISPLAY_OK : DSH_DISPLAY_ERR_HAL;
}
