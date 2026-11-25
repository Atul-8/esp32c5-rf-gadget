#pragma once
/* Device-side sweep state machine (plan A).

 * 分工：**调度搬进设备，判据留在主机**。设备只负责「按计划表轮着换频点、每窗
 * 出一帧」，怎么解读这一帧（WiFi 体检 / BLE / 5.8G 守卫 / 抓档分类）全在 PC 侧
 * 的 Python 里 —— 判据一行都没搬过来，所以真板标定、回放、场景矩阵全部照旧有效。
 *
 * 线上格式**不改**：每个窗口仍然是现有协议里的两段
 *     1. 文本头  ``SPEC <bins> <fs_hz> <bins> <freq_mhz>\n``（与 SPEC 命令的启动回复同格式）
 *     2. 一帧二进制 ``SPC1``（与 SPEC 命令的帧同格式，只有 header.frames 恒为 1）
 * 主机侧因此可以复用已有的帧解析器，只是把「谁掐表」从 PC 换成了设备。
 *
 * 为什么值得搬：PC 侧原来是「FREQ 一个窗 → SPEC 驻留 → 再 FREQ 下一个窗」，每个窗
 * 至少一个来回的串口空转；搬到设备后换频与出帧在同一个循环里接续，省的是往返延迟
 * （不省射频时间，采样率与点数一点没变）。 */
#include <stdbool.h>
#include <stdint.h>

#define SCAN_FSM_MAX_WINDOWS 128u

/* 设备没有频谱采集能力时（其它 family）用不到这一层：注册的 ops 为空即报 ERR。 */
typedef struct {
    /* 把射频切到 freq_mhz；失败返回 false（越界频点会被这里挡掉）。 */
    bool (*tune)(unsigned freq_mhz);
    /* 在当前频点采一帧并把它发出去（含文本头）；失败返回 false。 */
    bool (*frame)(unsigned bins, unsigned rate_code, unsigned detector, unsigned freq_mhz);
} scan_fsm_ops_t;

void scan_fsm_register(const scan_fsm_ops_t *ops);

/* 返回 true 表示这行是本模块的命令（已应答）。支持：
 *   FSMPLAN CLR | FSMPLAN ADD <mhz> <dwell_ms> | FSMPLAN? | FSM PLAN?
 *   FSM START <bins> <rate> <det> [repeats]   repeats=0 表示一直转
 *   FSM STOP | FSM? */
bool scan_fsm_command(char *line);

/* 串口空闲时被调用：按计划推进一个窗口。返回 true 表示干了活（调用方就别睡）。 */
bool scan_fsm_idle(void);

/* 是否正在扫（主机据此决定能不能发 GAIN / CAP，避免抢射频）。 */
bool scan_fsm_running(void);

/* 主机静默毫秒数：FSM 停止时距上次主机 FSM 命令过了多久；FSM 在扫时返回 -1。
 * 固件 ui_task 据此在主机走人后自动恢复巡扫（见 round_ui.c）。 */
int64_t scan_fsm_idle_ms(void);