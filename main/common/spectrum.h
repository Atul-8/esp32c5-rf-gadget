/* Shared on-device spectrum protocol. Acquisition callbacks return normalized
 * IQ10 words (I in bits 0..9, Q in 10..19) in stable CPU-owned memory. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef bool (*spectrum_acquire_fn)(unsigned n, unsigned rate, const uint32_t **words, unsigned *elapsed_us);
bool spectrum_command(const char *line, unsigned frequency_mhz, spectrum_acquire_fn acquire);

/* 扫频状态机（scan_fsm.c）专用：在**当前**频点采一帧并发出去。
 * 线上格式与 ``SPEC`` 完全相同（先文本头 ``SPEC n fs n freq``，再一帧 SPC1），
 * 只是 header.frames 恒为 1、帧数上限也为 1 —— 主机侧解析器可以原样复用。 */
bool spectrum_sweep_frame(unsigned n, unsigned rate_code, unsigned detector, unsigned freq_mhz,
                          spectrum_acquire_fn acquire);

bool spectrum_fft_init(void);
/* 扫频帧是否发往串口（默认开，保持与既有主机协议一致）。
 * 圆屏固件在开机自扫前关掉它：屏读本地快照，串口留给命令。 */
void spectrum_set_emit(bool on);
bool spectrum_emit_enabled(void);

/* 圆屏（ui_task）用的数据出口：拷出最近一帧的功率码。
 * 返回 false = 还没收到过任何帧（或 bins 超过给定缓冲）。
 * 参数意义：out 长度 max_bins，从低频到高频；freq_mhz 为该窗中心；
 * rate_hz 为该帧采样率（= 窗宽）。数据在临界区里整体拷出，调用者不会读到半帧。 */
bool spectrum_last_row(uint8_t *out, unsigned max_bins, unsigned *bins, unsigned *freq_mhz,
                       unsigned *rate_hz);

/* 占用统计（圆屏圆心读数用）：逐帧在本采集路径里判定「这一帧信道是否被占」。
 *
 * 为什么必须在采集路径里做：扫频状态机一窗只出一帧，单帧的 0/1 判不了占空比；
 * 而 ui_task 20 fps 且整个采集期间只结算一次窗口 —— 靠屏去逐帧统计根本采不全。
 * 所以由采集路径在一窗里多采 OCC_FRAMES 帧（只在设备自扫、不发流时）。
 *
 * 判据：只看窗内**中间一半** bin（所有采样率下都是有效通带；40 MS/s 窗的
 * 外侧 17% bin 是模拟滤波器外的零码，含进去会把 25% 分位拉到 0），
 * 帧内 (中位 − 25%分位) > 15 码即算被占。差分对「整带一起抬」的接收机
 * 跳变免疫，绝对电平则会被它骗。
 *
 * take 语义：临界区内取走累计值并清零，返回 false 表示还没有新数据。 */
typedef struct {
    unsigned frames;    /* 该批累计的帧数 */
    unsigned hits;      /* 其中被判为「被占」的帧数 */
    unsigned freq_mhz;  /* 这批帧的窗中心（窗口切换时取最后一帧的） */
    unsigned p50;       /* 最后一帧的中间一半中位码（诊断） */
    unsigned p25;
} spectrum_occ_t;
bool spectrum_occ_take(spectrum_occ_t *out);

/* Snapshot and continuous backends run exclusively. Reuse their large buffers
 * on SRAM-constrained targets; acquiring this workspace invalidates the cached
 * snapshot window so a later transport switch rebuilds it. */
typedef struct {
    int16_t *fft, *window;
    float *power;
    uint8_t *frame;
} spectrum_workspace_t;
spectrum_workspace_t spectrum_workspace(void);
