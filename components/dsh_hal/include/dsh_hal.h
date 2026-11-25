#ifndef DSH_HAL_H
#define DSH_HAL_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @file dsh_hal.h
 * @brief DSH 硬件抽象层契约（interface 层，平台无关，冻结管理）。
 *
 * 上层组件（dsh_display / dsh_touch / dsh_comm）只依赖本契约，
 * 不直接触碰平台 API（IDF / Arduino）。平台差异由 src/dsh_hal_*.c 实现吸收。
 *
 * v0.2（2026-10-31，ERR-002 修正）：SPI 显示传输从裸 spi_master 改为官方
 * esp_lcd panel_io 栈（与 esp-iot-solution usb_extend_screen 同栈），
 * CS/DC/DMA 全部由官方栈管理；契约从「裸命令/数据」演进为「命令+参数/像素」。
 *
 * v0.3（2026-10-31）：整帧刷新升级为 ping-pong 双缓冲 —— hal 持有 2 块全帧
 * DMA 缓冲，CPU 经 get_buffer 直写后台缓冲（无拷贝），flush 异步启动 DMA，
 * wait 同步收口；发 A 帧期间可写 B 帧（USB 流送 CPU/DMA 重叠的基础）。
 */

/** 统一错误码（平台无关） */
typedef enum {
    DSH_HAL_OK = 0,
    DSH_HAL_ERR_INVALID_ARG = -1,
    DSH_HAL_ERR_SPI = -2,
    DSH_HAL_ERR_GPIO = -3,
    DSH_HAL_ERR_NOT_INIT = -4,
} dsh_hal_err_t;

/** SPI 总线 + 显示控制引脚配置（GPIO 编号语义由实现层解释） */
typedef struct {
    int host;     /**< SPI 外设索引（实现层映射，如 IDF 的 SPI2_HOST） */
    int sck;      /**< 时钟引脚 */
    int mosi;     /**< 数据输出引脚 */
    int miso;     /**< 数据输入引脚，-1 表示不用（GC9A01 只写不读） */
    int cs;       /**< 片选引脚 */
    int dc;       /**< 数据/命令选择引脚，-1 表示不用 */
    int bl;       /**< 背光引脚，-1 表示不用 */
    int freq_hz;  /**< SPI 时钟频率 */
    /** ping-pong 单块缓冲字节数；0 = 默认全帧 240*240*2 = 115200。
     *  无 PSRAM 的板子（如 ESP32-C5）必须传条带大小，例如 24 行 = 11520。 */
    int frame_bytes;
} dsh_hal_spi_cfg_t;

/** 不透明面板句柄（实现层持有 esp_lcd io 与 DMA 暂存缓冲） */
typedef struct dsh_hal_panel dsh_hal_panel_t;

/* ---- GPIO ---- */

/** 配置为输出并设初值 */
dsh_hal_err_t dsh_hal_gpio_out(int gpio, bool level);
/** 设置输出电平 */
dsh_hal_err_t dsh_hal_gpio_set(int gpio, bool level);
/** 配置为输入，可选上拉 */
dsh_hal_err_t dsh_hal_gpio_in(int gpio, bool pullup);
/** 读取输入电平，返回 0/1；失败返回 -1 */
int dsh_hal_gpio_read(int gpio);

/* ---- 显示面板传输（esp_lcd 官方栈） ---- */

/**
 * 初始化 SPI 总线 + 面板 IO（CS/DC 由 esp_lcd 管理，背光默认点亮）。
 * 实现层内部一次性分配 2 块全帧 DMA 缓冲（当前 240x240x2 x2，ping-pong）。
 */
dsh_hal_err_t dsh_hal_panel_init(const dsh_hal_spi_cfg_t *cfg, dsh_hal_panel_t **out);

/** 释放面板资源 */
dsh_hal_err_t dsh_hal_panel_del(dsh_hal_panel_t *p);

/** 写一条命令（无参数，DC=0） */
dsh_hal_err_t dsh_hal_panel_cmd(dsh_hal_panel_t *p, uint8_t cmd);

/** 写命令 + 参数块（DC: 命令=0，参数=1；用于寄存器/初始化序列） */
dsh_hal_err_t dsh_hal_panel_param(dsh_hal_panel_t *p, uint8_t cmd,
        const uint8_t *params, size_t len);

/**
 * 取当前绘制缓冲（ping-pong 后台块，CPU 直写，无拷贝）。
 * 返回的缓冲在下次 flush 前有效；容量 = 全帧字节数（当前 240x240x2）。
 * 有 flush 在飞行时返回的是另一块缓冲（与 DMA 读的那块互不干扰）。
 */
dsh_hal_err_t dsh_hal_panel_get_buffer(dsh_hal_panel_t *p, uint8_t **buf, size_t *cap);

/**
 * 提交绘制缓冲（异步）：先等上一次 flush 完成，再以「命令+数据」启动 DMA。
 * bytes ≤ 缓冲容量；返回即表示本帧数据已进入 DMA 管线（尾块可能仍在飞行）。
 * 调用序列：get_buffer → 填充像素 → flush（可先去绘制下一帧再 wait）。
 */
dsh_hal_err_t dsh_hal_panel_flush(dsh_hal_panel_t *p, uint8_t cmd, size_t bytes);

/** 等待最近一次 flush 的 DMA 完成（同步收口，无在飞事务时立即返回） */
dsh_hal_err_t dsh_hal_panel_wait(dsh_hal_panel_t *p);

/* ---- 延迟 ---- */

void dsh_hal_delay_ms(uint32_t ms);
void dsh_hal_delay_us(uint32_t us);

#ifdef __cplusplus
}
#endif

#endif /* DSH_HAL_H */
