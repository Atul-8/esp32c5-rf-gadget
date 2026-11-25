/* The burst protocol is shared by native USB Serial/JTAG and UART0. */
#include "burst_serial.h"
#include "burst_gpio.h"
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "driver/uart.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_heap_caps.h"

/* 发送队列的前向声明：burst_serial_init() 在文件前部就要用到它们，
 * 而具体存储与 tx_task 定义在中段（依赖 tx_raw）。 */
#define BURST_TXQ_SLOTS 4
#define BURST_TXQ_MAX   768
/* BSS 只剩不到 1 KB（C5 RF dump 与 BSS 相邻，超出直接链接失败），
 * 3 KB 的发送队列只能放 PSRAM；PSRAM 不可用时退回内部堆。 */
static uint8_t *s_txq;
static SemaphoreHandle_t s_tx_mux;
static TaskHandle_t s_tx_task;
static void tx_task(void *arg);
#include "soc/soc_caps.h"
#if SOC_USB_SERIAL_JTAG_SUPPORTED
#include "hal/usb_serial_jtag_ll.h"
#endif
#include "sdkconfig.h"
#if CONFIG_IDF_TARGET_ESP32S2
#include "esp_private/usb_console.h"
#endif

#ifndef CONFIG_ESP_SDR_UART_BAUD
#define CONFIG_ESP_SDR_UART_BAUD 2000000
#endif

#define COMMAND_SIZE 128
static burst_serial_port_t active_port = BURST_SERIAL_USB;
static unsigned next_port;
static unsigned uart_baud = CONFIG_ESP_SDR_UART_BAUD;
static struct {
    char line[COMMAND_SIZE];
    size_t used;
    bool overflow;
    int64_t last_byte;
} input[BURST_SERIAL_COUNT];

/* Transport commands are consumed here for every receiver backend. The ACK
 * is completely transmitted at the old rate before changing UART0. The rate
 * is session-only; every boot starts with CONFIG_ESP_SDR_UART_BAUD. */
static bool baud_command(const char *line) {
    if (strcmp(line, "BAUD?") && strcmp(line, "BAUD") && strncmp(line, "BAUD ", 5)) return false;
#if CONFIG_ESP_SDR_UART_ENABLED
    if (active_port == BURST_SERIAL_UART) {
        char response[32];
        if (!strcmp(line, "BAUD?")) {
            int length = snprintf(response, sizeof(response), "BAUD %u\n", uart_baud);
            burst_serial_send(response, length);
            return true;
        }
        unsigned baud = !strcmp(line, "BAUD 1000000") ? 1000000 : !strcmp(line, "BAUD 2000000") ? 2000000 : 0;
        if (!baud) {
            burst_serial_send("ERR baud_args\n", sizeof("ERR baud_args\n") - 1);
            return true;
        }
        int length = snprintf(response, sizeof(response), "OK BAUD %u\n", baud);
        if (!burst_serial_send(response, length) ||
            uart_wait_tx_done(UART_NUM_0, pdMS_TO_TICKS(1000)) != ESP_OK ||
            uart_set_baudrate(UART_NUM_0, baud) != ESP_OK) {
            return true;
        }
        uart_baud = baud;
        uart_flush_input(UART_NUM_0);
        memset(&input[BURST_SERIAL_UART], 0, sizeof(input[BURST_SERIAL_UART]));
        return true;
    }
#endif
    burst_serial_send("ERR baud_transport\n", sizeof("ERR baud_transport\n") - 1);
    return true;
}

#if CONFIG_IDF_TARGET_ESP32S2
static void usb_ready(void *arg) {
    (void)arg;
}
#endif
void burst_serial_init(void) {
    if (!s_txq) {
        s_txq = heap_caps_malloc(BURST_TXQ_SLOTS * BURST_TXQ_MAX,
                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!s_txq) s_txq = malloc(BURST_TXQ_SLOTS * BURST_TXQ_MAX);
    }
    if (!s_tx_mux) s_tx_mux = xSemaphoreCreateMutex();
    if (!s_tx_task) {
        /* 优先级高于 idle 主循环：大帧要能抢在命令应答之间尽快吐出去 */
        xTaskCreate(tx_task, "burst_tx", 3072, NULL, 3, &s_tx_task);
    }
#if CONFIG_IDF_TARGET_ESP32S2
    ESP_ERROR_CHECK(esp_usb_console_set_cb(usb_ready, usb_ready, NULL));
#endif
#if CONFIG_ESP_SDR_UART_ENABLED
    uart_baud = CONFIG_ESP_SDR_UART_BAUD;
    const uart_config_t config = {
        .baud_rate = uart_baud,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_param_config(UART_NUM_0, &config));
    ESP_ERROR_CHECK(uart_set_pin(UART_NUM_0, CONFIG_ESP_SDR_UART_TX_PIN, CONFIG_ESP_SDR_UART_RX_PIN,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    /* Interrupt-driven RX keeps uploads buffered while the command is parsed.
     * TX uses the FIFO directly so all waits have a bounded deadline. */
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0, 8192, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_flush_input(UART_NUM_0));
#endif
    burst_gpio_init();
}

burst_serial_port_t burst_serial_port(void) {
    return active_port;
}
unsigned burst_serial_baud(void) {
    return active_port == BURST_SERIAL_UART ? uart_baud : 0;
}

static int read_port(burst_serial_port_t port, void *buffer, size_t size) {
    if (port == BURST_SERIAL_UART) {
#if CONFIG_ESP_SDR_UART_ENABLED
        return uart_read_bytes(UART_NUM_0, buffer, size, 0);
#else
        return 0;
#endif
    }
#if SOC_USB_SERIAL_JTAG_SUPPORTED
    return usb_serial_jtag_ll_read_rxfifo(buffer, size > 64 ? 64 : size);
#elif CONFIG_IDF_TARGET_ESP32S2
    return esp_usb_console_read_buf(buffer, size);
#else
    return 0;
#endif
}

bool burst_serial_stop_requested(void) {
    char c;
    bool stopped = false;
    for (unsigned j = 0; j < 128 && read_port(active_port, &c, 1) == 1; j++) {
        stopped = true;
        if (c == '\n') break;
    }
    return stopped;
}

int burst_serial_poll_line(char *line, size_t capacity) {
    for (unsigned j = 0; j < BURST_SERIAL_COUNT; ++j) {
        unsigned port = (next_port + j) % BURST_SERIAL_COUNT;
        int64_t now = esp_timer_get_time();
        if (now - input[port].last_byte > 3000000) {
            input[port].used = 0;
            input[port].overflow = false;
        }
        /* Bound polling so an unterminated command cannot starve the peer. */
        for (unsigned k = 0; k < COMMAND_SIZE; ++k) {
            char ch;
            if (read_port(port, &ch, 1) != 1) break;
            input[port].last_byte = now;
            if (ch == '\r') continue;
            if (ch != '\n') {
                if (input[port].used < COMMAND_SIZE - 1)
                    input[port].line[input[port].used++] = ch;
                else
                    input[port].overflow = true;
                continue;
            }
            size_t used = input[port].used;
            bool overflow = input[port].overflow;
            input[port].used = 0;
            input[port].overflow = false;
            if (!used && !overflow) continue;
            active_port = port;
            next_port = (port + 1) % BURST_SERIAL_COUNT;
            if (overflow || used >= capacity) return -1;
            memcpy(line, input[port].line, used);
            line[used] = '\0';
            if (baud_command(line)) return 0;
            return 1;
        }
    }
    return 0;
}

static int64_t transfer_deadline(size_t size) {
    /* UART 8N1 needs ten wire bits per byte. Leave two seconds for host
     * scheduling, including full frames at a deliberately reduced UART baud rate.
     * USB：主机不读（关了上位机/点了停止）时 FIFO 长期满，原 3 s deadline
     * 会让每条命令应答都卡满 3 s——rd_auto_scan 的 30+ 条命令在 ui_task 里
     * 同步发 ≈ 96 s 屏幕冻结（2026-11-25 用户实测「停止后还是卡死」）。
     * 主机在位时应答 <1 ms 就写完，100 ms 足够；不在位时快速放弃，别拖住
     * 渲染任务。 */
    int64_t timeout =
        active_port == BURST_SERIAL_UART ? 2000000 + (int64_t)size * 10000000 / uart_baud : 100000;
    return esp_timer_get_time() + timeout;
}

/* S2 ROM CDC writes: 256 bytes per call queue several USB packets at once.
 * This doubles throughput over 64-byte writes; larger chunks gain nothing. */
#define S2_USB_CHUNK 256
size_t burst_serial_try_send(const void *data, size_t size) {
    const uint8_t *p = data;
    size_t done = 0;
    while (done < size) {
        int sent = 0;
        if (active_port == BURST_SERIAL_UART) {
            sent = uart_tx_chars(UART_NUM_0, (const char *)p + done, size - done > 128 ? 128 : size - done);
        } else {
#if SOC_USB_SERIAL_JTAG_SUPPORTED
            if (!usb_serial_jtag_ll_txfifo_writable()) break;
            sent = usb_serial_jtag_ll_write_txfifo(p + done, size - done > 64 ? 64 : size - done);
            usb_serial_jtag_ll_txfifo_flush();
#elif CONFIG_IDF_TARGET_ESP32S2
            sent = esp_usb_console_write_direct(p + done, size - done > S2_USB_CHUNK ? S2_USB_CHUNK : size - done);
#endif
        }
        if (sent <= 0) break;
        done += (unsigned)sent;
    }
    return done;
}

/* 真正把字节塞进 FIFO 的地方（阻塞式）。只允许两个调用方：
 * 本文件的命令应答路径，以及 tx_task 消费队列。用 s_tx_mux 互斥，
 * 否则应答和频谱帧的字节会在 FIFO 里交错，主机解析必崩。 */
static bool tx_raw(const void *data, size_t size);

static bool IRAM_ATTR burst_serial_send_sync(const void *data, size_t size) {
    const uint8_t *p = data;
    size_t original = size;
    int64_t deadline = transfer_deadline(size);
    while (size) {
        if (esp_timer_get_time() >= deadline) return false;
        int sent;
        if (active_port == BURST_SERIAL_UART) {
            sent = uart_tx_chars(UART_NUM_0, (const char *)p, size > 128 ? 128 : size);
            if (sent < 0) return false;
            if (!sent) vTaskDelay(1);
        } else {
#if SOC_USB_SERIAL_JTAG_SUPPORTED
            /* FIFO 满时必须让出 CPU（vTaskDelay 阻塞 1 tick）：无人读串口时
             * tx_task(优先级 3) 若在这里纯 spin，会把 ui_task(优先级 1) 饿死
             * ——自动恢复自扫后主机可能已关闭，屏幕会二次「卡死」（2026-11-25）。
             * UART 分支 sent==0 时的 vTaskDelay(1) 同款姿势；等待超过
             * transfer_deadline 后由调用方放弃本帧，不阻塞不丢失同步。 */
            if (!usb_serial_jtag_ll_txfifo_writable()) {
                vTaskDelay(1);
                continue;
            }
            sent = usb_serial_jtag_ll_write_txfifo(p, size > 64 ? 64 : size);
            usb_serial_jtag_ll_txfifo_flush();
#elif CONFIG_IDF_TARGET_ESP32S2

            sent = esp_usb_console_write_direct(p, size > S2_USB_CHUNK ? S2_USB_CHUNK : size);
            if (sent < 0) return false;
            if (!sent) taskYIELD();
#else
            return false;
#endif
        }
        p += sent;
        size -= sent;
    }
#if SOC_USB_SERIAL_JTAG_SUPPORTED
    if (active_port == BURST_SERIAL_USB && original && original % 64 == 0) {
        while (!usb_serial_jtag_ll_txfifo_writable())
            if (esp_timer_get_time() >= deadline) return false;
        usb_serial_jtag_ll_txfifo_flush();
    }
#else
    (void)original;
#endif
    return true;
}

/* 同步发送入口（命令应答用）。内部与 tx_task 共用 tx_raw 的互斥，
 * 保证应答字节不会插进正在发送的频谱帧中间。 */
bool IRAM_ATTR burst_serial_send(const void *data, size_t size)
{
    return tx_raw(data, size);
}

static bool IRAM_ATTR tx_raw(const void *data, size_t size)
{
    bool ok;
    if (s_tx_mux) xSemaphoreTake(s_tx_mux, portMAX_DELAY);
    ok = burst_serial_send_sync(data, size);
    if (s_tx_mux) xSemaphoreGive(s_tx_mux);
    return ok;
}

/* ---------------- 大帧发送队列（生产者/消费者）----------------
 * 频谱每窗要发 ~530 B，115200 下约 46 ms。此前这一步同步发生在
 * scan_fsm_idle() 里，等于把主循环（命令通道）按住 46 ms/窗，
 * 主机想发任何命令都要排在这后面。
 * 现在：scan 只把帧拷进槽位就返回，由独立 tx_task 慢慢发。 */

static volatile uint16_t s_txq_len[BURST_TXQ_SLOTS];
static volatile int s_txq_head, s_txq_tail;
static portMUX_TYPE s_txq_mux = portMUX_INITIALIZER_UNLOCKED;

static void tx_task(void *arg)
{
    (void)arg;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        for (;;) {
            int slot = -1;
            uint16_t len = 0;
            portENTER_CRITICAL(&s_txq_mux);
            if (s_txq_tail != s_txq_head) {
                slot = s_txq_tail % BURST_TXQ_SLOTS;
                len = s_txq_len[slot];
            }
            portEXIT_CRITICAL(&s_txq_mux);
            if (slot < 0) break;
            if (len) tx_raw(s_txq + (size_t)slot * BURST_TXQ_MAX, len);
            portENTER_CRITICAL(&s_txq_mux);
            s_txq_len[slot] = 0;
            s_txq_tail++;
            portEXIT_CRITICAL(&s_txq_mux);
        }
    }
}

/* 非阻塞入队。队列满时丢「最旧」的未发帧并返回 false ——
 * 对屏幕/主机来说最新的频谱永远比陈旧的更有价值。 */
bool burst_serial_send_async(const void *data, size_t size)
{
    if (!s_tx_task || !s_txq || !data || !size || size > BURST_TXQ_MAX) return false;
    bool ok = true;
    portENTER_CRITICAL(&s_txq_mux);
    if (s_txq_head - s_txq_tail >= BURST_TXQ_SLOTS) {
        s_txq_tail++;                 /* 丢最旧 */
        ok = false;
    }
    int slot = s_txq_head % BURST_TXQ_SLOTS;
    memcpy(s_txq + (size_t)slot * BURST_TXQ_MAX, data, size);
    s_txq_len[slot] = (uint16_t)size;
    s_txq_head++;
    portEXIT_CRITICAL(&s_txq_mux);
    xTaskNotifyGive(s_tx_task);
    return ok;
}

/* 队列里还有没有没发完的大帧（主机侧流控/诊断用） */
unsigned burst_serial_tx_pending(void)
{
    unsigned n;
    portENTER_CRITICAL(&s_txq_mux);
    n = (unsigned)(s_txq_head - s_txq_tail);
    portEXIT_CRITICAL(&s_txq_mux);
    return n;
}

static void serve_reply(const char *text) {
    (void)burst_serial_send(text, strlen(text));
}

void burst_serial_serve_idle(void (*handle)(char *line), bool (*idle)(void)) {
    char line[128];
    int owner = -1;
    int64_t lease_deadline = 0;
    for (;;) {
        if (esp_timer_get_time() >= lease_deadline) owner = -1;
        int status = burst_serial_poll_line(line, sizeof(line));
        if (!status) {
            if (idle && idle()) continue;
            vTaskDelay(1);
            continue;
        }
        int port = burst_serial_port();
        if (owner >= 0 && owner != port) {
            serve_reply("ERR busy\n");
            continue;
        }
        if (status < 0) {
            serve_reply("ERR command_length\n");
            continue;
        }
        owner = port;
        if (!strcmp(line, "RELEASE")) {
            serve_reply("OK\n");
            owner = -1;
        } else {
            handle(line);
        }
        /* Ownership covers the entire binary transaction. */
        lease_deadline = esp_timer_get_time() + 5000000;
    }
}

void burst_serial_serve(void (*handle)(char *line)) { burst_serial_serve_idle(handle, NULL); }
