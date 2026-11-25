/* Device-side sweep scheduler. See scan_fsm.h for the division of labour. */
#include "scan_fsm.h"

#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "burst_serial.h"
#include "spectrum.h"
#include "esp_timer.h"

static const scan_fsm_ops_t *ops;
static scan_fsm_ops_t ops_owned; /* 指向它的副本，不依赖调用方对象的生命周期 */

typedef struct {
    uint16_t freq_mhz;
    uint16_t dwell_ms;
    uint8_t module; /* 主机侧模块下标，仅回显用；0xff = 未标注 */
} scan_window_t;

static scan_window_t plan[SCAN_FSM_MAX_WINDOWS];
static unsigned plan_len;
static int64_t s_last_cmd_us; /* 最近一条主机 FSM 命令的时刻（自动恢复判据） */

static struct {
    bool running;
    unsigned bins, rate_code, detector;
    unsigned repeats; /* 0 = 一直转 */
    unsigned done_repeats;
    unsigned cursor;  /* 当前窗口下标 */
    unsigned frames;  /* 已发帧数 */
    unsigned errors;  /* 采集/换频失败次数 */
    int64_t started_us;
    int64_t next_due_us;
} fsm;

static void reply(const char *text) { burst_serial_send(text, strlen(text)); }

static void reply_fmt(const char *fmt, ...) {
    char text[128];
    va_list args;
    va_start(args, fmt);
    vsnprintf(text, sizeof(text), fmt, args);
    va_end(args);
    burst_serial_send(text, strlen(text));
}

void scan_fsm_register(const scan_fsm_ops_t *new_ops) {
    if (new_ops) {
        ops_owned = *new_ops;
        ops = &ops_owned;
    } else {
        ops = NULL;
    }
    plan_len = 0;
    memset(&fsm, 0, sizeof(fsm));
}

bool scan_fsm_running(void) { return fsm.running; }

/* 主机静默毫秒数：FSM 停止且距上一次主机 FSM 命令已过去多久。
 * 返回 -1 = FSM 在扫（不适用）。用途：上位机关闭/停止采集后会发
 * FSM STOP 然后走人（或不再发命令），板子据此后台自己恢复巡扫，
 * 保证离线产品的「屏幕永远自己转」——逻辑在单片机，不依赖主机。
 * 主机只要还在发命令（重建计划/校准/查询），计时就被刷新，不会误启。 */
int64_t scan_fsm_idle_ms(void) {
    if (fsm.running) return -1;
    return (esp_timer_get_time() - s_last_cmd_us) / 1000;
}

static void fsm_reset(void) {
    fsm.running = false;
    fsm.cursor = 0;
    fsm.done_repeats = 0;
    fsm.started_us = 0;
    fsm.next_due_us = 0;
}

static void status_line(void) {
    unsigned freq = 0, dwell = 0;
    if (plan_len) {
        unsigned at = fsm.cursor < plan_len ? fsm.cursor : 0;
        freq = plan[at].freq_mhz;
        dwell = plan[at].dwell_ms;
    }
    reply_fmt("FSM %s %u %u %u %u %u %u %" PRIi64 "\n", fsm.running ? "sweeping" : "idle", plan_len, fsm.cursor,
              freq, fsm.frames, fsm.errors, dwell,
              fsm.started_us ? (esp_timer_get_time() - fsm.started_us) / 1000 : 0);
}

static bool plan_command(char *line) {
    unsigned mhz, dwell, module = 0xff;
    char extra;
    if (!strcmp(line, "FSMPLAN CLR")) {
        plan_len = 0;
        reply("OK" "\n");
        return true;
    }
    if (sscanf(line, "FSMPLAN ADD %u %u %u %c", &mhz, &dwell, &module, &extra) >= 2) {
        if (plan_len >= SCAN_FSM_MAX_WINDOWS) {
            reply("ERR fsm_plan_full\n");
            return true;
        }
        if (!mhz || !dwell || dwell > 60000u) {
            reply("ERR fsm_plan_args\n");
            return true;
        }
        plan[plan_len].freq_mhz = (uint16_t)mhz;
        plan[plan_len].dwell_ms = (uint16_t)dwell;
        plan[plan_len].module = (uint8_t)module;
        plan_len++;
        reply("OK" "\n");
        return true;
    }
    if (!strcmp(line, "FSMPLAN?") || !strcmp(line, "FSM PLAN?")) {
        reply_fmt("FSMPLAN %u\n", plan_len);
        for (unsigned j = 0; j < plan_len; j++)
            reply_fmt("W %u %u %u %u\n", j, plan[j].freq_mhz, plan[j].dwell_ms, plan[j].module);
        reply("FSMPLANEND\n");
        return true;
    }
    return false;
}

bool scan_fsm_command(char *line) {
    if (strncmp(line, "FSM", 3)) return false;
    /* 主机命令打点：自动恢复自扫的「静默」判据（见 scan_fsm_idle_ms）。
     * 注意：FSM STOP 本身也是一条命令，所以静默从 STOP 时刻起算。 */
    s_last_cmd_us = esp_timer_get_time();
    if (plan_command(line)) return true;
    if (!strcmp(line, "FSM?")) {
        status_line();
        return true;
    }
    if (!strcmp(line, "FSM STREAM ON") || !strcmp(line, "FSM STREAM OFF")) {
        bool on = !strcmp(line, "FSM STREAM ON");
        spectrum_set_emit(on);
        reply(on ? "OK STREAM ON\n" : "OK STREAM OFF\n");
        return true;
    }
    if (!strcmp(line, "FSM STREAM") || !strcmp(line, "FSM STREAM?")) {
        reply(spectrum_emit_enabled() ? "FSM stream=1\n" : "FSM stream=0\n");
        return true;
    }
    if (!strcmp(line, "FSM STOP")) {
        unsigned was = fsm.running ? 1u : 0u;
        fsm_reset();
        /* 总是应答：主机靠它判断「已经停稳、可以发 GAIN / CAP 了」。
         * 参数表示之前到底在不在扫，便于主机区分「停了个空」与「真停了」。 */
        reply_fmt("FSMEND %u\n", was);
        return true;
    }
    unsigned bins, rate, det, repeats = 0;
    char extra;
    int fields = sscanf(line, "FSM START %u %u %u %u %c", &bins, &rate, &det, &repeats, &extra);
    if (fields == 3 || fields == 4) {
        if (!ops) {
            reply("ERR fsm_unsupported\n");
            return true;
        }
        if (!plan_len) {
            reply("ERR fsm_no_plan\n");
            return true;
        }
        if (bins < 256 || bins > 2048 || (bins & (bins - 1)) || !rate || rate > 5 || det > 1 || repeats > 100000u) {
            reply("ERR fsm_args\n");
            return true;
        }
        fsm.bins = bins;
        fsm.rate_code = rate;
        fsm.detector = det;
        fsm.repeats = repeats;
        fsm.done_repeats = 0;
        fsm.cursor = 0;
        fsm.frames = 0;
        fsm.errors = 0;
        fsm.started_us = esp_timer_get_time();
        fsm.next_due_us = fsm.started_us;
        fsm.running = true;
        reply_fmt("FSM START %u %u %u %u\n", plan_len, bins, rate, det);
        return true;
    }
    if (!strcmp(line, "FSM START")) {
        reply("ERR fsm_args\n");
        return true;
    }
    return false;
}

/* 空转时的推进点：一次只出一个窗口，出错不停机（把错误计数记下来继续扫），
 * 因为一次采集失败在真板上是真会偶发的（见 META/实测：失败后会空转重试）。 */
bool scan_fsm_idle(void) {
    if (!fsm.running || !ops) return false;
    int64_t now = esp_timer_get_time();
    if (now < fsm.next_due_us) return false;
    if (fsm.cursor >= plan_len) {
        fsm.cursor = 0;
        fsm.done_repeats++;
        if (fsm.repeats && fsm.done_repeats >= fsm.repeats) {
            fsm_reset();
            reply("FSMEND 1\n");
            return true;
        }
    }
    scan_window_t window = plan[fsm.cursor];
    bool ok = ops->tune(window.freq_mhz) &&
              ops->frame(fsm.bins, fsm.rate_code, fsm.detector, window.freq_mhz);
    if (ok)
        fsm.frames++;
    else
        fsm.errors++;
    fsm.cursor++;
    /* 下一窗的时间点按「上一窗计划时刻 + 驻留」推进，避免累计漂移；
     * 落后太多（>1 s，例如主机在发别的命令）就直接对齐到现在，别补帧。 */
    fsm.next_due_us += (int64_t)window.dwell_ms * 1000;
    if (fsm.next_due_us < now - 1000000) fsm.next_due_us = now;
    return true;
}