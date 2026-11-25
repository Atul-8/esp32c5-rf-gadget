/**
 * @file dsh_hal_idf.c
 * @brief dsh_hal 的 ESP-IDF 实现（core 层）。
 *
 * v0.3：显示传输基于乐鑫官方 esp_lcd panel_io 栈 + ping-pong 双全帧缓冲（ERR-002）。
 * CPU 经 get_buffer 直写后台缓冲（无拷贝），flush 异步启动 DMA，wait 同步收口；
 * 发 A 帧期间可写/收 B 帧（USB 流送 CPU/DMA 重叠的基础）。
 * CS/DC/DMA 时序全部由 esp_lcd 管理；tx_param 走 polling，tx_color 尾块异步
 * （release_bus 不等待），用完成回调 + 信号量收口。
 */
#include "dsh_hal.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_cache.h"
#include "esp_memory_utils.h"   /* esp_ptr_external_ram：C5 无 PSRAM 时恒 false */
#include "soc/soc_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_io_spi.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

static const char *TAG = "dsh_hal";

/** 全帧缓冲字节数：当前最大面板 240x240 RGB565（ping-pong x2） */
#define DSH_HAL_FRAME_BYTES (240 * 240 * 2)

struct dsh_hal_panel {
    esp_lcd_panel_io_handle_t io;   /**< esp_lcd panel io（管理 CS/DC/SPI 事务） */
    uint8_t *buf[2];                /**< ping-pong DMA 全帧缓冲 */
    int sent_idx;                   /**< 最近一次 flush 使用的缓冲下标 */
    bool in_flight;                 /**< 最近一次 flush 的 DMA 是否仍在飞行 */
    int bl;                         /**< 背光引脚，-1 无 */
    size_t buf_bytes;               /**< ping-pong 单块字节数（全帧或条带） */
    int host;                       /**< SPI 外设索引，del 时释放总线 */
    SemaphoreHandle_t done_sem;     /**< flush 完成信号（esp_lcd 尾块异步，需收口） */
};

/* esp_lcd tx_color 数据块走异步队列，最后一个分块在 tx_color 返回时可能仍在 DMA 飞行；
 * 用完成回调 + 信号量把 color 路径收口为同步（tx_param 全 polling，无需此机制） */
static IRAM_ATTR bool spi_color_done_cb(esp_lcd_panel_io_handle_t io,
        esp_lcd_panel_io_event_data_t *edata, void *user_ctx)
{
    dsh_hal_panel_t *p = (dsh_hal_panel_t *)user_ctx;
    if (p && p->done_sem) {
        BaseType_t high_task_wakeup = pdFALSE;
        xSemaphoreGiveFromISR(p->done_sem, &high_task_wakeup);
        return high_task_wakeup == pdTRUE;
    }
    return false;
}

dsh_hal_err_t dsh_hal_gpio_out(int gpio, bool level)
{
    if (gpio < 0) {
        return DSH_HAL_ERR_INVALID_ARG;
    }
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << gpio),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&cfg) != ESP_OK) {
        return DSH_HAL_ERR_GPIO;
    }
    if (gpio_set_level((gpio_num_t)gpio, level ? 1 : 0) != ESP_OK) {
        return DSH_HAL_ERR_GPIO;
    }
    return DSH_HAL_OK;
}

dsh_hal_err_t dsh_hal_gpio_set(int gpio, bool level)
{
    if (gpio < 0) {
        return DSH_HAL_ERR_INVALID_ARG;
    }
    if (gpio_set_level((gpio_num_t)gpio, level ? 1 : 0) != ESP_OK) {
        return DSH_HAL_ERR_GPIO;
    }
    return DSH_HAL_OK;
}

dsh_hal_err_t dsh_hal_gpio_in(int gpio, bool pullup)
{
    if (gpio < 0) {
        return DSH_HAL_ERR_INVALID_ARG;
    }
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << gpio),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = pullup ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&cfg) != ESP_OK) {
        return DSH_HAL_ERR_GPIO;
    }
    return DSH_HAL_OK;
}

int dsh_hal_gpio_read(int gpio)
{
    if (gpio < 0) {
        return -1;
    }
    return gpio_get_level((gpio_num_t)gpio);
}

dsh_hal_err_t dsh_hal_panel_init(const dsh_hal_spi_cfg_t *cfg, dsh_hal_panel_t **out)
{
    if (!out || !cfg) {
        return DSH_HAL_ERR_INVALID_ARG;
    }
    if (cfg->sck < 0 || cfg->mosi < 0 || cfg->cs < 0 || cfg->dc < 0) {
        return DSH_HAL_ERR_INVALID_ARG;
    }

    dsh_hal_panel_t *p = (dsh_hal_panel_t *)calloc(1, sizeof(dsh_hal_panel_t));
    if (!p) {
        return DSH_HAL_ERR_SPI;
    }
    p->bl = cfg->bl;
    p->host = cfg->host;
    p->sent_idx = 0;

    /* 背光先行（Seeed 官方 xiao_disp_init：digitalWrite(XIAO_BL, HIGH)） */
    if (p->bl >= 0) {
        if (dsh_hal_gpio_out(p->bl, true) != DSH_HAL_OK) {
            free(p);
            return DSH_HAL_ERR_GPIO;
        }
    }

    /* v6.x：esp_lcd 不再提供总线初始化助手，先用官方 spi_master 建总线，
     * 事务/CS/DC/DMA 管理全部由 esp_lcd panel_io 接管 */
    spi_bus_config_t bus = {
        .mosi_io_num = cfg->mosi,
        .miso_io_num = cfg->miso,
        .sclk_io_num = cfg->sck,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = DSH_HAL_FRAME_BYTES,
    };
    if (spi_bus_initialize((spi_host_device_t)cfg->host, &bus, SPI_DMA_CH_AUTO) != ESP_OK) {
        ESP_LOGE(TAG, "spi bus init failed (sck=%d mosi=%d miso=%d)", cfg->sck, cfg->mosi, cfg->miso);
        free(p);
        return DSH_HAL_ERR_SPI;
    }

    p->done_sem = xSemaphoreCreateBinary();
    if (!p->done_sem) {
        ESP_LOGE(TAG, "done sem alloc failed");
        free(p);
        return DSH_HAL_ERR_SPI;
    }

    esp_lcd_panel_io_spi_config_t io_cfg = {
        .dc_gpio_num = (gpio_num_t)cfg->dc,
        .cs_gpio_num = (gpio_num_t)cfg->cs,
        .pclk_hz = cfg->freq_hz,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .spi_mode = 0,
        .trans_queue_depth = 8,
        .on_color_trans_done = spi_color_done_cb,
        .user_ctx = p,
    };
    if (esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)cfg->host, &io_cfg, &p->io) != ESP_OK) {
        ESP_LOGE(TAG, "panel io init failed (cs=%d dc=%d freq=%d)", cfg->cs, cfg->dc, cfg->freq_hz);
        vSemaphoreDelete(p->done_sem);
        free(p);
        return DSH_HAL_ERR_SPI;
    }

    p->buf_bytes = (cfg->frame_bytes > 0 && (size_t)cfg->frame_bytes <= DSH_HAL_FRAME_BYTES)
            ? (size_t)cfg->frame_bytes : DSH_HAL_FRAME_BYTES;
    if (cfg->frame_bytes > 0 && (size_t)cfg->frame_bytes > DSH_HAL_FRAME_BYTES) {
        ESP_LOGE(TAG, "frame_bytes %d > full frame %d", cfg->frame_bytes, DSH_HAL_FRAME_BYTES);
        vSemaphoreDelete(p->done_sem);
        free(p);
        return DSH_HAL_ERR_INVALID_ARG;
    }

    /* ping-pong 双缓冲：**优先内部 DMA 内存**。
     * ESP32-C5 的 SPI-DMA 从 PSRAM 取颜色数据会静默失败（DMA 正常结束、无错误、
     * 面板收不到有效像素 —— 实测：同一 esp_lcd 配置下 DMA 内存能显示、PSRAM 缓冲全白）。
     * 因此条带尺寸必须放得下内部 RAM；只有条带过大时才退 PSRAM。 */
    const char *buf_loc = "dma";
    for (int i = 0; i < 2; i++) {
        p->buf[i] = (uint8_t *)heap_caps_aligned_alloc(64, p->buf_bytes, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!p->buf[i]) {
            buf_loc = "psram";
            p->buf[i] = (uint8_t *)heap_caps_aligned_alloc(64, p->buf_bytes, MALLOC_CAP_SPIRAM);
        }
        if (!p->buf[i]) {
            ESP_LOGE(TAG, "buf[%d] alloc failed (%u bytes)", i, (unsigned)p->buf_bytes);
            for (int j = 0; j < i; j++) {
                heap_caps_free(p->buf[j]);
            }
            vSemaphoreDelete(p->done_sem);
            free(p);
            return DSH_HAL_ERR_SPI;
        }
    }

    ESP_LOGI(TAG, "panel io ok: sck=%d mosi=%d miso=%d cs=%d dc=%d bl=%d freq=%dHz mode0 (esp_lcd, pingpong 2x%dKB @%s)",
             cfg->sck, cfg->mosi, cfg->miso, cfg->cs, cfg->dc, cfg->bl, cfg->freq_hz,
             (unsigned)(p->buf_bytes / 1024), buf_loc);
    *out = p;
    return DSH_HAL_OK;
}

dsh_hal_err_t dsh_hal_panel_del(dsh_hal_panel_t *p)
{
    if (!p) {
        return DSH_HAL_ERR_INVALID_ARG;
    }
    /* 收口在飞 DMA，再按 io → 总线 的顺序释放（否则重复 init 会因总线占用失败） */
    if (p->in_flight && p->done_sem) {
        xSemaphoreTake(p->done_sem, portMAX_DELAY);
        p->in_flight = false;
    }
    if (p->io) {
        esp_lcd_panel_io_del(p->io);
    }
    for (int i = 0; i < 2; i++) {
        if (p->buf[i]) {
            heap_caps_free(p->buf[i]);
        }
    }
    if (p->done_sem) {
        vSemaphoreDelete(p->done_sem);
    }
    spi_bus_free((spi_host_device_t)p->host);
    free(p);
    return DSH_HAL_OK;
}

dsh_hal_err_t dsh_hal_panel_cmd(dsh_hal_panel_t *p, uint8_t cmd)
{
    if (!p || !p->io) {
        return DSH_HAL_ERR_NOT_INIT;
    }
    if (esp_lcd_panel_io_tx_param(p->io, cmd, NULL, 0) != ESP_OK) {
        return DSH_HAL_ERR_SPI;
    }
    return DSH_HAL_OK;
}

dsh_hal_err_t dsh_hal_panel_param(dsh_hal_panel_t *p, uint8_t cmd,
        const uint8_t *params, size_t len)
{
    if (!p || !p->io) {
        return DSH_HAL_ERR_NOT_INIT;
    }
    if (len > 0 && !params) {
        return DSH_HAL_ERR_INVALID_ARG;
    }
    if (esp_lcd_panel_io_tx_param(p->io, cmd, params, len) != ESP_OK) {
        return DSH_HAL_ERR_SPI;
    }
    return DSH_HAL_OK;
}

dsh_hal_err_t dsh_hal_panel_get_buffer(dsh_hal_panel_t *p, uint8_t **buf, size_t *cap)
{
    if (!p || !buf || !cap) {
        return DSH_HAL_ERR_INVALID_ARG;
    }
    *buf = p->buf[p->sent_idx ^ 1];
    *cap = p->buf_bytes;
    return DSH_HAL_OK;
}

dsh_hal_err_t dsh_hal_panel_flush(dsh_hal_panel_t *p, uint8_t cmd, size_t bytes)
{
    if (!p || !p->io) {
        return DSH_HAL_ERR_NOT_INIT;
    }
    if (bytes == 0 || bytes > p->buf_bytes) {
        return DSH_HAL_ERR_INVALID_ARG;
    }
    /* 等上一次 flush 完成（同设备的 polling 事务不允许与在飞队列事务并发） */
    if (p->in_flight) {
        xSemaphoreTake(p->done_sem, portMAX_DELAY);
        p->in_flight = false;
    }
    int idx = p->sent_idx ^ 1;   /* 与 get_buffer 一致的后台缓冲 */
    p->sent_idx = idx;
    /* PSRAM 缓冲：CPU 写停于 writeback cache，SPI-DMA 绕 cache 直读内存
     * —— 不写回 = 条纹花屏（RAW 时代缓冲在内部 SRAM 无此问题） */
    if (esp_ptr_external_ram(p->buf[idx])) {
        esp_cache_msync(p->buf[idx], bytes, ESP_CACHE_MSYNC_FLAG_DIR_C2M);
    }
    if (esp_lcd_panel_io_tx_color(p->io, cmd, p->buf[idx], bytes) != ESP_OK) {
        return DSH_HAL_ERR_SPI;
    }
    p->in_flight = true;
    return DSH_HAL_OK;
}

dsh_hal_err_t dsh_hal_panel_wait(dsh_hal_panel_t *p)
{
    if (!p) {
        return DSH_HAL_ERR_INVALID_ARG;
    }
    if (p->in_flight) {
        xSemaphoreTake(p->done_sem, portMAX_DELAY);
        p->in_flight = false;
    }
    return DSH_HAL_OK;
}

void dsh_hal_delay_ms(uint32_t ms)
{
    vTaskDelay(pdMS_TO_TICKS(ms));
}

void dsh_hal_delay_us(uint32_t us)
{
    esp_rom_delay_us(us);
}
