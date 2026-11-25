/**
 * @file dsh_touch_idf.c
 * @brief dsh_touch 的 ESP-IDF 实现（core 层）：CHSC6X over I2C master。
 *
 * 对照 Seeed 官方 lv_xiao_round_screen.h：INT 低有效（高时 1ms 后复查去抖），
 * I2C 0x2E 读 5 字节：[0]=状态 0x01 按下，[2]=x，[4]=y（单字节）。
 */
#include "dsh_touch.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_rom_sys.h"

#define CHSC6X_I2C_ADDR      0x2E
#define CHSC6X_READ_LEN      5

static i2c_master_dev_handle_t s_dev = NULL;
static i2c_master_bus_handle_t s_bus = NULL;   /* 保留给总线扫描诊断 */
static int s_int_gpio = -1;

/* 诊断：保存最近一次读取的原始字节与卡点，供 RD TOUCHRAW 打印 */
static uint8_t s_last_raw[CHSC6X_READ_LEN];
static int s_last_stage = -1;

dsh_touch_err_t dsh_touch_init(const dsh_touch_cfg_t *cfg)
{
    if (!cfg || cfg->sda < 0 || cfg->scl < 0) {
        return DSH_TOUCH_ERR_INVALID_ARG;
    }

    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = cfg->i2c_port >= 0 ? cfg->i2c_port : 0,
        .sda_io_num = (gpio_num_t)cfg->sda,
        .scl_io_num = (gpio_num_t)cfg->scl,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,   /* XIAO 模块内已带上拉，双保险 */
    };
    i2c_master_bus_handle_t bus = NULL;
    if (i2c_new_master_bus(&bus_cfg, &bus) != ESP_OK) {
        return DSH_TOUCH_ERR_BUS;
    }
    s_bus = bus;

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = CHSC6X_I2C_ADDR,
        .scl_speed_hz = cfg->freq_hz ? cfg->freq_hz : 100000,
    };
    if (i2c_master_bus_add_device(bus, &dev_cfg, &s_dev) != ESP_OK) {
        return DSH_TOUCH_ERR_BUS;
    }

    s_int_gpio = cfg->int_gpio;
    if (s_int_gpio >= 0) {
        gpio_config_t io = {
            .pin_bit_mask = (1ULL << s_int_gpio),
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,   /* 官方 INPUT_PULLUP */
        };
        gpio_config(&io);
    }
    return DSH_TOUCH_OK;
}

bool dsh_touch_read(uint16_t *x, uint16_t *y)
{
    if (!s_dev) {
        return false;
    }
    /* 官方去抖：INT 高（未按下）时 1ms 后复查一次 */
    if (s_int_gpio >= 0 && gpio_get_level((gpio_num_t)s_int_gpio) != 0) {
        esp_rom_delay_us(1000);
        if (gpio_get_level((gpio_num_t)s_int_gpio) != 0) {
            s_last_stage = 1;
            return false;
        }
    }

    uint8_t temp[CHSC6X_READ_LEN] = {0};
    if (i2c_master_receive(s_dev, temp, CHSC6X_READ_LEN, 20) != ESP_OK) {
        s_last_stage = 2;
        return false;
    }
    for (int i = 0; i < CHSC6X_READ_LEN; ++i) {
        s_last_raw[i] = temp[i];
    }
    s_last_stage = 0;
    if (temp[0] != 0x01) {
        return false;
    }
    if (x) {
        *x = temp[2];
    }
    if (y) {
        *y = temp[4];
    }
    return true;
}

dsh_touch_err_t dsh_touch_scan(dsh_touch_scan_t *out)
{
    if (!out) {
        return DSH_TOUCH_ERR_INVALID_ARG;
    }
    out->count = 0;
    out->has_chsc6x = 0;
    out->first_addr = 0;
    if (!s_bus) {
        return DSH_TOUCH_ERR_BUS;
    }
    /* 0x08..0x77 是 7 位地址的可用区。逐个 probe，超时给短一点，
     * 全扫一遍最坏 112 * 20ms，只在启动时做一次。 */
    for (uint8_t addr = 0x08; addr <= 0x77; ++addr) {
        if (i2c_master_probe(s_bus, addr, 5) == ESP_OK) {
            if (out->count == 0) {
                out->first_addr = addr;
            }
            out->count++;
            if (addr == CHSC6X_I2C_ADDR) {
                out->has_chsc6x = 1;
            }
        }
    }
    return DSH_TOUCH_OK;
}

bool dsh_touch_present(void)
{
    if (!s_bus) {
        return false;
    }
    return i2c_master_probe(s_bus, CHSC6X_I2C_ADDR, 20) == ESP_OK;
}

const uint8_t *dsh_touch_last_raw(void)
{
    return s_last_raw;
}

int dsh_touch_last_stage(void)
{
    return s_last_stage;
}
