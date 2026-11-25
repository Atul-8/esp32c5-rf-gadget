#ifndef DSH_DISPLAY_H
#define DSH_DISPLAY_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "dsh_hal.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @file dsh_display.h
 * @brief DSH 显示驱动契约（interface 层，平台无关）。
 *
 * 通过 dsh_hal 访问 SPI/GPIO，不直接触碰平台 API。
 * 当前实现：GC9A01（240x240 RGB565）。
 */

typedef enum {
    DSH_DISPLAY_OK = 0,
    DSH_DISPLAY_ERR_INVALID_ARG = -1,
    DSH_DISPLAY_ERR_HAL = -2,
    DSH_DISPLAY_ERR_NOT_INIT = -3,
} dsh_display_err_t;

/** 不透明句柄 */
typedef struct dsh_display dsh_display_t;

typedef struct {
    dsh_hal_spi_cfg_t spi;  /**< SPI 总线 + 引脚配置（含 dc/bl） */
    int reset_gpio;         /**< 复位引脚，-1 = 软复位 */
    bool invert;            /**< 颜色反转（GC9A01 通常 true，即 INVON） */
    bool mirror_x;          /**< 水平镜像 */
    bool mirror_y;          /**< 垂直镜像 */
    bool swap_xy;           /**< 交换 XY（旋转 90°） */
} dsh_display_cfg_t;

/** 初始化显示（含 SPI 初始化 + GC9A01 初始化 + 点亮背光 + 开显示） */
dsh_display_err_t dsh_display_init(dsh_display_t **out, const dsh_display_cfg_t *cfg);

/** 释放显示资源 */
dsh_display_err_t dsh_display_del(dsh_display_t *d);

/** 整屏填充颜色（RGB565） */
dsh_display_err_t dsh_display_fill(dsh_display_t *d, uint16_t color);

/** 绘制位图到指定窗口（坐标含两端点），pixels 为 RGB565，长度 = w*h */
dsh_display_err_t dsh_display_draw_bitmap(dsh_display_t *d,
        uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1,
        const uint16_t *pixels);

/** 背光开关 */
dsh_display_err_t dsh_display_backlight(dsh_display_t *d, bool on);

/**
 * 整帧刷新（ping-pong 后台缓冲路径）：把 RGB565 大端字节流拷入 hal 后台
 * 全帧缓冲，设窗口后异步提交 DMA；返回时 DMA 在飞，下次调用自动收口。
 * bytes 必须 = 240*240*2。流送场景：发下一帧期间上一帧 DMA 与之重叠。
 */
dsh_display_err_t dsh_display_flush_full(dsh_display_t *d,
        const uint8_t *rgb565_be, size_t bytes);

/** 条带刷新：把 y0 起 rows 行的 RGB565 大端字节流提交 DMA（rows*240*2 <= 缓冲容量） */
dsh_display_err_t dsh_display_flush_band(dsh_display_t *d, uint16_t y0, uint16_t rows,
        const uint8_t *rgb565_be);

/** 取下一块后台缓冲（直写零拷贝），cap 返回该块字节数 */
dsh_display_err_t dsh_display_back_buffer(dsh_display_t *d, uint8_t **buf, size_t *cap);

/** 提交已写好的后台缓冲到屏上 y0 起 rows 行 */
dsh_display_err_t dsh_display_submit_band(dsh_display_t *d, uint16_t y0, uint16_t rows);

#ifdef __cplusplus
}
#endif

#endif /* DSH_DISPLAY_H */
