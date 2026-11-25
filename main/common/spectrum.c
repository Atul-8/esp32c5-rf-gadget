/* Portable on-chip spectra for targets without a validated continuous backend.
 * Each frame is an independent snapshot. Frame indices use elapsed wall time,
 * and flag bit 3 explicitly marks the gaps between acquisitions. */
#include "spectrum.h"
#include "spectrum_dc.h"
#include "spectrum_math.h"
#include "spectrum_fft.h"
#include "spectrum_stats.h"
#include "esp_cpu.h"
#include "burst_serial.h"
#include "dsps_fft2r.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

/* FFT 上限 = 主机实际会要的最大 bins。上位机扫频固定 512 bins（10 MHz / 512
 * → 19.5 kHz 每 bin），所以 2048 的余量从第一天起就没被用过，却让下面五个数组
 * 全按 2048 开、白占 26 KB SRAM（BSS 26,656 B 是 C5 上最大的一块静态区）。
 * 降到 512 后 bins 只能取 256/512；要再开大就把这个值和 ring_capture.h 的
 * RING_SPEC_NFFT_MAX 一起改。 */
#define MAX_FFT      512u
#define HEADER_BYTES 28u
#if CONFIG_IDF_TARGET_ESP32C2
/* Quiescent during RF capture; keep heap/stacks in the other SRAM banks. */
#define SPEC_STORAGE __attribute__((section(".c2_spectrum"), aligned(16)))
#else
#define SPEC_STORAGE
#endif
static int16_t fft_data[2 * MAX_FFT] __attribute__((aligned(16))) SPEC_STORAGE;
static int16_t window[MAX_FFT] SPEC_STORAGE;
/* Same workspace size for both detectors. Max-hold never needs floats;
 * mean accumulation retains fractional precision without extra SRAM. */
static union {
    float mean[MAX_FFT];
    uint32_t maximum[MAX_FFT];
} powers SPEC_STORAGE;
static uint8_t frame[HEADER_BYTES + MAX_FFT + 4] SPEC_STORAGE;
static unsigned setup_n;
static bool ready;

/* --- 圆屏用的最近一帧快照 --------------------------------------------------
 * 屏要显示频谱，但它是另一个任务（ui_task）。不能让它直接读 ``frame[]``：
 * 下一帧的 pack_spectrum 随时会改写那块缓冲。所以在这里留一份定长副本，
 * 只在发包完成后（此时 frame 已不再变动）用临界区整体换掉。
 * 代价：512 B BSS，换 ui_task 一个安全、无锁等待的数据源。 */
static uint8_t last_row[MAX_FFT];
static unsigned last_bins;
static unsigned last_freq_mhz;
static unsigned last_rate_hz;
static volatile bool last_valid;
static portMUX_TYPE last_mux = portMUX_INITIALIZER_UNLOCKED;

bool spectrum_last_row(uint8_t *out, unsigned max_bins, unsigned *bins, unsigned *freq_mhz,
                       unsigned *rate_hz) {
    if (!out || !bins || !freq_mhz || !rate_hz) return false;
    bool ok = false;
    portENTER_CRITICAL(&last_mux);
    if (last_valid && last_bins <= max_bins) {
        for (unsigned i = 0; i < last_bins; i++) out[i] = last_row[i];
        *bins = last_bins;
        *freq_mhz = last_freq_mhz;
        *rate_hz = last_rate_hz;
        ok = true;
    }
    portEXIT_CRITICAL(&last_mux);
    return ok;
}
/* ---- 占用统计 ------------------------------------------------------------
 * 逐帧判「这一帧信道有没有被占」，累计到 occ_* 里等 ui_task 取走。
 *
 * 判据只用帧内差分 (中位 − 25%分位)：真板实测接收机会整带跳变（阻带 bin 从
 * 1~5 码抬到 38~47 码，中心只抬 20 码），任何绝对门限都会被它骗；差分只跟
 * 本帧自身的谱形状有关。
 *
 * 只统计窗内**中间一半** bin：设备自扫是 512 bins @ 40 MS/s，实测 17% 的 bin
 * 落在模拟滤波器外、恒为 0 码，含进直方图会把 25% 分位压到 0，判据就恒为真
 * （真板上跑出过 88~100% 的占空比）。中间一半 = ±10 MHz，所有采样率下都是
 * 有效通带。
 *
 * 门限 15 码来自主机侧同配置实测：空信道(5745) 0.9%、5500 2.6%、
 * 有 AP 的 2437 16.2%、2442 区段 29~32%。 */
#define OCC_DB 15
static uint16_t occ_hist[256];
static uint32_t occ_frames, occ_hits, occ_last_mhz, occ_p50, occ_p25;
static portMUX_TYPE occ_mux = portMUX_INITIALIZER_UNLOCKED;

/* 直方图三段式：occ_begin/occ_add/occ_finish。
 * 拆开是因为「额外帧」路径按 bit-reversal 顺序遍历、只把通带内的 bin 塞进来，
 * 不必先存一整行码（省 2 KB BSS，C5 的 BSS 有硬上限）。 */
static unsigned occ_cnt;
static void occ_begin(void) {
    memset(occ_hist, 0, sizeof(occ_hist));
    occ_cnt = 0;
}
static void occ_add(uint8_t code) {
    occ_hist[code]++;
    occ_cnt++;
}
/* 返回 1 = 本帧判为「被占」。 */
static int occ_finish(void) {
    if (occ_cnt < 8) return 0;
    unsigned q25 = 0, q50 = 0, acc = 0;
    unsigned t25 = occ_cnt / 4 ? occ_cnt / 4 : 1, t50 = occ_cnt / 2 ? occ_cnt / 2 : 1;
    bool got25 = false;
    for (unsigned v = 0; v < 256; v++) {
        acc += occ_hist[v];
        if (!got25 && acc >= t25) {
            q25 = v;
            got25 = true;
        }
        if (acc >= t50) {
            q50 = v;
            break;
        }
    }
    occ_p25 = q25;
    occ_p50 = q50;
    return (q50 > q25 && (q50 - q25) > OCC_DB) ? 1 : 0;
}

/* 一帧的码（频率升序）→ 只取中间一半 → 判一次。 */
static int occ_absorb(const uint8_t *codes, unsigned n) {
    unsigned lo = n / 4, hi = n - n / 4;
    if (hi <= lo || hi - lo < 8) return 0;
    occ_begin();
    for (unsigned i = lo; i < hi; i++) occ_add(codes[i]);
    return occ_finish();
}

bool spectrum_occ_take(spectrum_occ_t *out) {
    if (!out) return false;
    bool ok = false;
    portENTER_CRITICAL(&occ_mux);
    if (occ_frames) {
        out->frames = occ_frames;
        out->hits = occ_hits;
        out->freq_mhz = occ_last_mhz;
        out->p50 = occ_p50;
        out->p25 = occ_p25;
        occ_frames = 0;
        occ_hits = 0;
        ok = true;
    }
    portEXIT_CRITICAL(&occ_mux);
    return ok;
}

spectrum_workspace_t spectrum_workspace(void) {
    setup_n = 0;
    return (spectrum_workspace_t){fft_data, window, powers.mean, frame};
}

static int16_t twiddles[MAX_FFT] __attribute__((aligned(16))) SPEC_STORAGE;
bool spectrum_fft_init(void) {
    if (!ready) ready = dsps_fft2r_init_sc16(twiddles, MAX_FFT) == ESP_OK;
    return ready;
}

static const unsigned rates[] = {
#if CONFIG_IDF_TARGET_ESP32H2
    0,        0,        0,        0,      0, 0,
    16000000, 32000000, 10666667, 6400000
#elif CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32C2 ||                   \
    CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C6
    80000000, 40000000, 0,       0,
    0,        0,        16000000
#else
    80000000, 40000000, 20000000, 10000000,
    8000000,  4000000
#endif
};
static unsigned rate_hz(unsigned code) {
    return code < sizeof(rates) / sizeof(rates[0]) ? rates[code] : 0;
}
static bool send_text(const char *s) {
    return burst_serial_send(s, strlen(s));
}
static void put16(unsigned at, uint16_t v) {
    frame[at] = v;
    frame[at + 1] = v >> 8;
}
static void put32(unsigned at, uint32_t v) {
    put16(at, v);
    put16(at + 2, v >> 16);
}
static void put64(unsigned at, uint64_t v) {
    put32(at, v);
    put32(at + 4, v >> 32);
}
static int16_t clamp16(int32_t v) {
    return v > 32767 ? 32767 : v < -32768 ? -32768 : v;
}

static void capabilities(void) {
    send_text("SPECINFO {\"continuous\":false,\"transports\":["
#if CONFIG_IDF_TARGET_ESP32C2
              "\"UART\""
#else
              "\"USB\",\"UART\""
#endif
              "],\"profiles\":[");
    bool first = true;
    for (unsigned r = 0; r < sizeof(rates) / sizeof(rates[0]); r++)
        if (rates[r]) {
            for (unsigned n = 256; n <= MAX_FFT; n *= 2) {
                char text[96];
                snprintf(text, sizeof(text), "%s[%u,%u,%u,1,1,%u]", first ? "" : ",", rates[r], r, n,
#if CONFIG_IDF_TARGET_ESP32C61 || CONFIG_IDF_TARGET_ESP32C6
                         n == 256 && burst_serial_port() != BURST_SERIAL_UART
#else
                         0u
#endif
                );
                send_text(text);
                first = false;
            }
        }
    send_text("]}\n");
}

typedef struct {
    size_t pending, sent;
    uint32_t *frames;
    int64_t progress;
} spectrum_output_t;
static void pump_spectrum(void *context) {
    spectrum_output_t *out = context;
    if (!out->pending) return;
    size_t written = burst_serial_try_send(frame + out->sent, out->pending - out->sent);
    out->sent += written;
    if (written) out->progress = esp_timer_get_time();
    if (out->sent == out->pending) {
        out->pending = out->sent = 0;
        (*out->frames)++;
    }
}

/* Encoding consumes the completed batch; transmission owns frame[] while
 * the next batch accumulates independently in powers. */
static size_t pack_spectrum(unsigned n, unsigned log2n, unsigned det, unsigned merged, unsigned frames,
                            uint64_t index, uint8_t gain) {
    memcpy(frame, "SPC1", 4);
    put32(4, frames);
    put64(8, index);
    put32(16, n * merged);
    put16(20, merged);
    frame[22] = 8 | (det ? 1 : 0);
    frame[23] = gain;
    put16(24, 0);
    frame[26] = log2n;
    frame[27] = 2;
    const float mean_scale = 1.0f / merged;
    for (unsigned j = 0, bin = 0; j < n; j++) {
        frame[HEADER_BYTES + bin] = det ? spectrum_power_code(powers.maximum[j])
                                        : spectrum_mean_power_code(powers.mean[j] * mean_scale);
        unsigned bit = n >> 1;
        while (bin & bit) {
            bin ^= bit;
            bit >>= 1;
        }
        bin ^= bit;
    }
    put32(HEADER_BYTES + n, esp_rom_crc32_le(0, frame, HEADER_BYTES + n));
    memset(&powers, 0, n * sizeof(float));
    return HEADER_BYTES + n + 4;
}

/* 单帧版：不驻留、不累计，一个窗口一帧。刻意与 SPEC 的循环体保持同一套运算
 * （同窗口函数、同 DC 处理、同 pack_spectrum），差别只在于「只算一帧就返回」。 */
/* 扫频时是否把每一帧打到串口。
 * 关掉的理由：USJ 是双向共用通道，25 窗/s × ~530 B ≈ 13 KB/s 已压过
 * 115200 的 11.5 KB/s，主机发来的命令字节会被数据流埋掉（实测 RD? 全丢）。
 * 关掉后固件仍照常采集并维护本地快照（spectrum_last_row），
 * 圆屏走本地快照，命令通道恢复即时响应；主机要连续流再打开即可。 */
static volatile bool s_emit = true;

void spectrum_set_emit(bool on) { s_emit = on; }
bool spectrum_emit_enabled(void) { return s_emit; }

/* 额外采 count 帧，只喂占用统计，不发包、不改 last_row。
 * 运算与主帧完全一致（同窗口函数、同 DC 处理、同码换算），只把码直接塞进
 * 直方图而不留整行 —— 判定口径与圆屏看到的帧同源。
 *
 * 帧间必须**摊开**（OCC_GAP_MS）：背靠背连着采 8 帧只占 ~0.3 ms，对短包/周期性
 * 突发类干扰等价于"只采了一个样本"，8 帧高度相关 → 单窗几乎只有 0/100 两态，
 * 加上 EMA 后系统性低估占空比（实测 2412 MHz：主机 40 ms 一帧摊 0.4 s 算 26%，
 * 板端背靠背 8 帧连续几分钟都是 0%）。自扫窗口驻留 60 ms，8 帧 × 5 ms 间隔
 * ≈ 40 ms，仍在预算内。 */
#define OCC_FRAMES 8
#define OCC_GAP_MS 5
static void occ_more_frames(unsigned n, unsigned rate_code, spectrum_acquire_fn acquire, unsigned count) {
    unsigned lo = n / 4, hi = n - n / 4;
    for (unsigned k = 0; k < count; k++) {
        const uint32_t *words;
        unsigned elapsed;
        if (k) vTaskDelay(pdMS_TO_TICKS(OCC_GAP_MS));
        if (!acquire(n, rate_code, &words, &elapsed)) return;
        for (unsigned j = 0; j < n; j++) {
            int32_t i = ((int32_t)(words[j] << 22) >> 22), q = ((int32_t)(words[j] << 12) >> 22);
            fft_data[2 * j] = clamp16((i * window[j]) >> 9);
            fft_data[2 * j + 1] = clamp16((q * window[j]) >> 9);
        }
        uint32_t frames = 0;
        spectrum_output_t out = {.frames = &frames, .progress = esp_timer_get_time()};
        spectrum_fft_poll(fft_data, n, dsps_fft_w_table_sc16, pump_spectrum, &out);
        spectrum_dc_t dc = {0};
        spectrum_dc_apply(&dc, fft_data, n);
        occ_begin();
        for (unsigned j = 0, bin = 0; j < n; j++) {
            if (bin >= lo && bin < hi)
                occ_add(spectrum_mean_power_code(
                    spectrum_complex_power(fft_data[2 * j], fft_data[2 * j + 1])));
            unsigned bit = n >> 1;
            while (bin & bit) {
                bin ^= bit;
                bit >>= 1;
            }
            bin ^= bit;
        }
        int hit = occ_finish();
        portENTER_CRITICAL(&occ_mux);
        occ_frames++;
        occ_hits += (uint32_t)hit;
        portEXIT_CRITICAL(&occ_mux);
    }
}

bool spectrum_sweep_frame(unsigned n, unsigned rate_code, unsigned detector, unsigned freq_mhz,
                          spectrum_acquire_fn acquire) {
    if (n < 256 || n > MAX_FFT || (n & (n - 1)) || !rate_hz(rate_code) || detector > 1) return false;
    spectrum_fft_init();
    if (!ready) return false;
    if (setup_n != n) {
        for (unsigned j = 0; j < n; j++) window[j] = (int16_t)lrintf(16383.5f * (1 - cosf(2 * M_PI * j / n)));
        setup_n = n;
    }
    unsigned log2n = 0;
    while ((1u << log2n) < n) log2n++;
    bool emit = s_emit;
    char text[192];
    if (emit) {
        snprintf(text, sizeof(text), "SPEC %u %u %u %u" "\n", n, rate_hz(rate_code), n, freq_mhz);
        if (!send_text(text)) return false;
    }
    const uint32_t *words;
    unsigned elapsed;
    if (!acquire(n, rate_code, &words, &elapsed)) return false;
    uint8_t gain = (uint8_t)(words[0] >> 20);
    for (unsigned j = 0; j < n; j++) {
        int32_t i = ((int32_t)(words[j] << 22) >> 22), q = ((int32_t)(words[j] << 12) >> 22);
        fft_data[2 * j] = clamp16((i * window[j]) >> 9);
        fft_data[2 * j + 1] = clamp16((q * window[j]) >> 9);
    }
    uint32_t frames = 0;
    spectrum_output_t out = {.frames = &frames, .progress = esp_timer_get_time()};
    spectrum_fft_poll(fft_data, n, dsps_fft_w_table_sc16, pump_spectrum, &out);
    spectrum_dc_t dc = {0};
    spectrum_dc_apply(&dc, fft_data, n);
    for (unsigned j = 0; j < n; j++) {
        uint32_t p = spectrum_complex_power(fft_data[2 * j], fft_data[2 * j + 1]);
        if (detector) {
            if (p > powers.maximum[j]) powers.maximum[j] = p;
        } else
            powers.mean[j] = (float)p;
    }
    uint64_t index = (uint64_t)esp_timer_get_time() * rate_hz(rate_code) / 1000000u;
    size_t size = pack_spectrum(n, log2n, detector, 1, frames, index, gain);
    /* 大帧走队列：不再在采集路径里阻塞 ~46 ms。
     * 队列满（主机消费不过来）只丢帧不算失败，采集继续跑。 */
    if (emit) (void)burst_serial_send_async(frame, size);
    /* 发包已结束，frame[] 不再会被本函数改写：把纯频率升序的功率码存一份给圆屏。
     * pack_spectrum 已经做完 bit-reversal，frame[HEADER_BYTES + j] 就是第 j 个
     * bin（低频→高频），屏那边直接按频率线性铺开即可。 */
    /* 占用统计要在发布 last_row 之前算：frame[] 里的 bin 码已做完 bit-reversal，
     * 和圆屏看到的是同一份数据，两者口径不会分歧。 */
    int occ_hit = occ_absorb(&frame[HEADER_BYTES], n);
    {
        portENTER_CRITICAL(&occ_mux);
        occ_last_mhz = freq_mhz;
        occ_frames++;
        occ_hits += (uint32_t)occ_hit;
        portEXIT_CRITICAL(&occ_mux);
    }
    /* 每窗只出一帧 → 单帧的 0/1 判不了「占空比」。设备自扫（不往串口发流）时
     * 再多采 OCC_FRAMES-1 帧只为统计：一次采集 12.8 µs + 一次 512 点 FFT，
     * 8 帧合计仍是 60 ms 驻留里的一小部分。主机驱动（emit=true）时不额外采，
     * 免得拖慢主机的帧率；那种场景主机自己有全帧流可数。 */
    if (!emit) occ_more_frames(n, rate_code, acquire, OCC_FRAMES - 1);
    portENTER_CRITICAL(&last_mux);
    for (unsigned j = 0; j < n; j++) last_row[j] = frame[HEADER_BYTES + j];
    last_bins = n;
    last_freq_mhz = freq_mhz;
    last_rate_hz = rate_hz(rate_code);
    last_valid = true;
    portEXIT_CRITICAL(&last_mux);
    return true;
}

bool spectrum_command(const char *line, unsigned frequency_mhz, spectrum_acquire_fn acquire) {
    unsigned dc_mode;
    char dc_extra;
    if (!strcmp(line, "DC?")) {
        char text[16];
        snprintf(text, sizeof(text), "DC %u\n", spectrum_dc_mode);
        send_text(text);
        return true;
    }
    if (sscanf(line, "DC %u %c", &dc_mode, &dc_extra) == 1 && dc_mode < 2) {
        spectrum_dc_mode = dc_mode;
        send_text("OK\n");
        return true;
    }
    if (!strcmp(line, "SPECINFO?")) {
        capabilities();
        return true;
    }
    if (strncmp(line, "SPEC ", 5)) return false;
    unsigned ms, stride, units, det, rate, n, stats = 0;
    char extra;
    int fields =
        sscanf(line, "SPEC %u %u %u %u %u %u %u %c", &ms, &stride, &units, &det, &rate, &n, &stats, &extra);
    if ((fields != 6 && fields != 7) || stats > 1 || ms > 86400000u || stride != 1 || !units || units > 8 ||
        det > 1 || !rate_hz(rate) || n < 256 || n > MAX_FFT || (n & (n - 1))) {
        send_text("ERR spec_args\n");
        return true;
    }
    spectrum_fft_init();
    if (!ready) {
        send_text("ERR spectrum_memory\n");
        return true;
    }
    if (setup_n != n) {
        for (unsigned j = 0; j < n; j++) window[j] = (int16_t)lrintf(16383.5f * (1 - cosf(2 * M_PI * j / n)));
        setup_n = n;
    }
    unsigned log2n = 0;
    while ((1u << log2n) < n) log2n++;
    char text[192];
    snprintf(text, sizeof(text), "SPEC %u %u %u %u\n", n, rate_hz(rate), n, frequency_mhz);
    if (!send_text(text)) return true;
    int64_t start = esp_timer_get_time(), yield_at = start;
    uint32_t frames = 0, ffts = 0, status = 0;
    uint64_t pairs = 0;
    bool stopped = false;
    spectrum_dc_t dc = {0};
    spectrum_stats_t telemetry;
    spectrum_stats_init(&telemetry);
    unsigned merged = 0;
    uint8_t gain = 0;
    uint64_t index = 0;
    spectrum_output_t out = {.frames = &frames, .progress = start};
    memset(&powers, 0, n * sizeof(float));
    do {
        if (burst_serial_stop_requested()) {
            stopped = true;
            break;
        }
        uint32_t busy_start = esp_cpu_get_cycle_count();
        if (!merged) index = (uint64_t)(esp_timer_get_time() - start) * rate_hz(rate) / 1000000u;
        const uint32_t *words;
        unsigned elapsed;
        if (!acquire(n, rate, &words, &elapsed)) {
            status = 1;
            break;
        }
        if (!merged) gain = words[0] >> 20;
        for (unsigned j = 0; j < n; j++) {
            int32_t i = ((int32_t)(words[j] << 22) >> 22), q = ((int32_t)(words[j] << 12) >> 22);
            fft_data[2 * j] = clamp16((i * window[j]) >> 9);
            fft_data[2 * j + 1] = clamp16((q * window[j]) >> 9);
        }
        spectrum_fft_poll(fft_data, n, dsps_fft_w_table_sc16, pump_spectrum, &out);
        spectrum_dc_apply(&dc, fft_data, n);
        for (unsigned j = 0; j < n; j++) {
            uint32_t p = spectrum_complex_power(fft_data[2 * j], fft_data[2 * j + 1]);
            if (det) {
                if (p > powers.maximum[j]) powers.maximum[j] = p;
            } else
                powers.mean[j] += (float)p;
        }
        ffts++;
        pairs += n;
        merged++;
        pump_spectrum(&out);
        if (out.pending && esp_timer_get_time() - out.progress > 3000000) {
            status = 7;
            break;
        }
        if (!out.pending) {
            if (stats) spectrum_stats_emit(&telemetry, n, rate_hz(rate), ffts, 0, 0, 0, 0, burst_serial_send);
            out.pending = pack_spectrum(n, log2n, det, merged, frames, index, gain);
            merged = 0;
            out.progress = esp_timer_get_time();
            pump_spectrum(&out);
        }
        telemetry.busy += (uint32_t)(esp_cpu_get_cycle_count() - busy_start);
        if (merged == 65535) break; /* bound the wire FFT count if the host stalls */
        if (esp_timer_get_time() - yield_at >= 20000) {
            vTaskDelay(1);
            yield_at = esp_timer_get_time();
        }
    } while (!ms || esp_timer_get_time() - start < (int64_t)ms * 1000);
    /* Drain in order; a partial final batch never contains an earlier frame. */
    if (out.pending) {
        if (burst_serial_send(frame + out.sent, out.pending - out.sent))
            frames++;
        else
            status = 7;
    }
    if (!status && merged) {
        size_t size = pack_spectrum(n, log2n, det, merged, frames, index, gain);
        if (burst_serial_send(frame, size))
            frames++;
        else
            status = 7;
    }
    snprintf(text, sizeof(text), "SPECEND %u 0 %u %" PRIu64 " %" PRIi64 " 0 0 %u 0 0 %u %u\n",
             (unsigned)status, (unsigned)ffts, pairs, esp_timer_get_time() - start, (unsigned)frames,
             (unsigned)ffts, stopped);
    send_text(text);
    return true;
}
