#ifndef DSH_TOUCH_H
#define DSH_TOUCH_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @file dsh_touch.h
 * @brief DSH 触摸驱动契约（interface 层，平台无关，冻结管理）。
 *
 * 目标硬件：Seeed Round Display 的 CHSC6X（I2C 0x2E，XIAO ESP32-S3:
 * SDA=D4/GPIO5, SCL=D5/GPIO6, INT=D7/GPIO44 低有效）。
 * 协议严格对照 Seeed 官方 lv_xiao_round_screen.h：
 * 读 5 字节，[0]=状态(0x01 按下)，[2]=x，[4]=y（单字节 0..239）。
 */

typedef enum {
    DSH_TOUCH_OK = 0,
    DSH_TOUCH_ERR_INVALID_ARG = -1,
    DSH_TOUCH_ERR_BUS = -2,
} dsh_touch_err_t;

typedef struct {
    int i2c_port;     /**< I2C 控制器号（IDF 语义由实现层解释） */
    int sda;          /**< SDA GPIO */
    int scl;          /**< SCL GPIO */
    int int_gpio;     /**< INT GPIO（低有效），-1 = 不用 INT 纯轮询 */
    uint32_t freq_hz; /**< I2C 时钟，0 = 100kHz（官方 Wire 默认） */
} dsh_touch_cfg_t;

/** 初始化 I2C 总线 + 触摸设备 + INT 引脚 */
dsh_touch_err_t dsh_touch_init(const dsh_touch_cfg_t *cfg);

/**
 * 读取触摸点。返回 true = 按下（x/y 有效，0..239）；false = 未按下/读取失败。
 * 内部带官方同款 INT 去抖（INT 高时 1ms 后复查）。
 */
bool dsh_touch_read(uint16_t *x, uint16_t *y);

/** 总线扫描结果（诊断用，不影响正常触摸读取）。 */
typedef struct {
    uint8_t count;      /**< 0x08..0x77 内应答的设备总数 */
    uint8_t has_chsc6x; /**< CHSC6X(0x2E) 是否应答 */
    uint8_t first_addr; /**< 首个应答地址，无则 0 */
} dsh_touch_scan_t;

/**
 * 扫描 I2C 总线，判断触摸控制器是否真的在线上。
 * 用途：触摸读不到坐标时区分「I2C 不通」与「INT 引脚映射错」。
 * 必须由持有 I2C 的任务调用（单写者原则）。
 */
dsh_touch_err_t dsh_touch_scan(dsh_touch_scan_t *out);

/**
 * 单点探测 CHSC6X(0x2E) 是否应答，比全总线扫描快得多（一次 probe）。
 * 用于上电重试与运行期周期重扫。
 */
bool dsh_touch_present(void);

/**
 * 最近一次 dsh_touch_read() 的原始 5 字节（无论是否判定为按下都保存）。
 * 用途：read 返回 false 时区分「I2C 读失败」与「状态字节不等于 0x01」。
 * 内容永不为 NULL；未读过时全 0。
 */
const uint8_t *dsh_touch_last_raw(void);

/**
 * 最近一次 dsh_touch_read() 卡在哪一步（诊断用）：
 *  -1 = 还没读过
 *   0 = I2C 成功拿到 5 字节（此时看 last_raw()[0] 是否为 0x01）
 *   1 = 被 INT 高电平去抖逻辑拦掉，I2C 根本没读
 *   2 = i2c_master_receive 失败
 */
int dsh_touch_last_stage(void);


#ifdef __cplusplus
}
#endif

#endif /* DSH_TOUCH_H */
