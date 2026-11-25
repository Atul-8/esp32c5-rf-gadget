#pragma once

#include <stdbool.h>
#include <stddef.h>

typedef enum { BURST_SERIAL_USB, BURST_SERIAL_UART, BURST_SERIAL_COUNT } burst_serial_port_t;

void burst_serial_init(void);
/* 1: complete line, -1: overlong line, 0: idle. Each port has its own parser.
 * A complete line selects the port for all subsequent replies and payloads.
 * BAUD? and BAUD <1000000|2000000> are consumed internally. Baud changes
 * last until reset; OK BAUD <rate> is sent at the previous rate. */
int burst_serial_poll_line(char *line, size_t capacity);
burst_serial_port_t burst_serial_port(void);
unsigned burst_serial_baud(void);
bool burst_serial_send(const void *data, size_t size);
/* 非阻塞：把整帧拷进发送队列就返回，由内部 tx_task 真正写串口。
 * 用于频谱这类每帧几百字节的数据；命令应答仍用 burst_serial_send（同步），
 * 保证 "发完即生效" 的语义不被破坏。 */
bool burst_serial_send_async(const void *data, size_t size);
unsigned burst_serial_tx_pending(void);
/* Write only what fits now; never wait for the transport. */
size_t burst_serial_try_send(const void *data, size_t size);

/* Consume a stream stop request on the owning port only. */
bool burst_serial_stop_requested(void);

/* Read commands forever and pass each line to `handle`. One port at a time
 * owns the receiver: the other gets "ERR busy" until the owner sends RELEASE
 * or stays silent for five seconds. RELEASE itself is answered here. */
void burst_serial_serve(void (*handle)(char *line));

/* Same as burst_serial_serve, but calls `idle` on every poll that produced no
 * complete command. `idle` returns true when it did work, in which case the
 * loop skips its 1 ms delay (otherwise the delay keeps the idle path cheap).
 * Used by the device-side sweep state machine. */
void burst_serial_serve_idle(void (*handle)(char *line), bool (*idle)(void));
