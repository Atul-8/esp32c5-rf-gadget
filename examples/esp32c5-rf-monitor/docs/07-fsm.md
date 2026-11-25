# 07 · 设备端扫频状态机（方案 A）

> 分支 `feat/7-fsm`｜真板实测 2026-11-19（COM56）｜**判据一行都没搬**

## 为什么搬、搬的是什么

原来「谁掐表」在 PC 上：`host` 每扫一个窗都要 `FREQ <mhz>` → `SPEC <dwell> ... <bins>` →
收完帧再发下一个窗。射频时间省不了，但**每窗至少一个来回**是真的花在串口上。

方案 A 只把**调度**搬进设备：

| 搬进固件 | 留在主机（Python） |
| --- | --- |
| 计划表（哪些频点、每窗驻留多久、轮着扫） | 五个模块的判据（WiFi 体检 / BLE / 5.8G 守卫 / 抓档 / 分类器） |
| 状态机（idle → 逐窗 → 收尾） | 时间聚合、证据链、结论措辞 |
| 换频 + 出一帧 | 落库、GUI、告警 |

所以：**不新增判据、不改阈值、不动已验收的回放与场景矩阵**。真板标定照旧有效。

## 线上格式：一个字节都没改

每个窗口仍然是协议里已有的两段：

```
SPEC <bins> <fs_hz> <bins> <freq_mhz>\n     ← 与 SPEC 命令的启动回复同一个格式
SPC1 <28 B 头> <bins B 谱> <4 B CRC>        ← 与 SPEC 命令的帧同一个格式（header.frames 恒为 1）
```

主机的帧解析器**原样复用**，只是从「发一条 SPEC 等一串帧」变成「读一行头、读一帧、再读一行头」。

> 这是刻意选的：帧格式一动，`core/snapshot.py`、`fake_firmware`、回放、自检、真板标定全得重来。

## 新命令

| 命令 | 应答 | 说明 |
| --- | --- | --- |
| `FSMPLAN CLR` | `OK` | 清空计划 |
| `FSMPLAN ADD <mhz> <dwell_ms> [module]` | `OK` | 追加一个窗口（最多 128 个；`module` 仅回显） |
| `FSMPLAN?` / `FSM PLAN?` | `FSMPLAN <n>` + n×`W <i> <mhz> <dwell> <module>` + `FSMPLANEND` | 读回计划 |
| `FSM START <bins> <rate> <det> [repeats]` | `FSM START <plan_len> <bins> <rate> <det>` | `repeats=0` 一直转 |
| `FSM STOP` | `FSMEND 0` | 停止；已发出的帧不回收，主机要边排空边找 `FSMEND` |
| `FSM?` | `FSM idle\|sweeping <plan> <cursor> <freq> <frames> <errors> <dwell> <elapsed_ms>` | 状态 |
| `CAPS` | 增加 ` FSM` 标记 | 主机据此判断设备会不会自扫 |

错误码：`ERR fsm_unsupported`（该 family 没注册采集回调）、`ERR fsm_no_plan`、
`ERR fsm_args`、`ERR fsm_plan_full`、`ERR fsm_plan_args`。

## 实现落点（全是加法）

| 文件 | 改动 |
| --- | --- |
| `main/common/scan_fsm.{c,h}`（新） | 计划表 + 状态机 + `scan_fsm_idle()` 推进点；不碰射频细节，靠两个回调 `tune()` / `frame()` 与 family 解耦 |
| `main/common/spectrum.c/.h` | 新增 `spectrum_sweep_frame()`：单帧版采集（同窗口函数 / 同 DC / 同 `pack_spectrum`），与 `SPEC` 的循环体逐位可比 |
| `main/common/burst_serial.c/.h` | 新增 `burst_serial_serve_idle(handle, idle)`；`burst_serial_serve()` 变成它的薄包装（其它 family 行为不变） |
| `main/families/c5_c6_c61/receiver.c` | 注册 `fsm_tune`（= 主机 `FREQ` 那条路径）+ `fsm_frame`（= `SPEC` 那条路径）；`handle_command` 加一行分发；`CAPS` 加 `FSM` |
| `main/CMakeLists.txt` | sources 加 `common/scan_fsm.c` |

线程模型刻意保持**单任务**：扫频推进发生在串口空闲钩子里，和命令处理、射频操作同一条
主任务，因此不存在「两个任务抢射频」的问题。代价是主机发命令会插进两个窗口之间（真板
实测延迟 < 1 个窗口），收益是没有任何共享状态需要加锁。

## 真板实测（COM56，`testing/fsm_probe.py`）

```
[CAPS] FSM
[PLAN] FSMPLAN 3  存回 = [2405, 2410, 2415]
[START] FSM START 3 512 2 0
[FRAMES] 100 帧 / 频点 [2405, 2410, 2415] / bins [512]
[RATE] 16.89 窗/秒          ← 60 ms 驻留，理论 16.67 窗/秒：驻留被如实兑现
[STOP] FSMEND 0
[STATUS] FSM idle 3 0 2405 101 0 60 0
[probe] caps_has_fsm=ok plan_roundtrip=ok start_ack=ok frames_flowed=ok freq_rotated=ok
        stop_ack=ok idle_after_stop=ok no_frames_after_stop=ok
```

对照：主机逐窗驱动时约 **4 窗/秒**（每窗一次 `FREQ` + `SPEC` 往返）。设备自扫在同样
512 bin / 20 MS/s 下 **16.9 窗/秒**，快的是往返开销，不是射频。

固件体积：`esp_sdr.bin` 823,680 → **827,104 B**（+3.4 KB，其中计划表 128×6 B 静态内存）。

## 边界（必须随结论一起说）

1. **不省射频时间**：采样率、点数、驻留都没变；省的是串口往返。
2. **不改帧格式**，所以也不可能「更细的频谱」或「更多窗/秒」同时保持同样驻留。
3. `FSM STOP` 时串口里还压着已发出的帧（设备不因为主机不读就丢帧）：主机必须读到
   `FSMEND` 才算收工，直接 `readline` 只会读到二进制碎片。
4. 主机在自扫期间**不要**发 `GAIN` / `CAP20`：它们和扫频共用射频。要抓档或调增益，先
   `FSM STOP`，做完再 `FSM START`。
5. 目前 `repeats` 与 `module` 字段只是回显；模块归属由主机按频点自己判（主机手上有
   `info.plans`，不需要设备告诉它）。

---

## 主机侧怎么用（`engine=host` / `engine=device`）

固件会自扫之后，主机这边就多了一个选择：**谁掐表**。两个引擎共用同一套模块、同一套判据、
同一套下行 payload，区别只在「窗是谁驱动的」。

### 一条配置切换

`HostConfig.engine`（JSON 字段 `engine`，CLI `--engine`）取 `host`（默认）或 `device`：

```python
HostConfig.from_json(to_json({"port": "COM56", "engine": "device", "plan_dwell_ms": 20}))
```

```bash
python app.py --port COM56 --engine device          # 设备自扫
python app.py --port COM56 --engine host            # 主机逐窗（对照基线，老路径）
python testing/sweep_real.py --engine device --seconds 30
python testing/sweep_real.py --engine host  --mock --seconds 6   # 没板子也能验统计口径
```

设备不支持自扫（`CAPS` 里没有 `FSM`）时**自动退回 host**，并且把原因写进
`engine_note`、原样报到界面上——不静默降级。

### 两种引擎的口径差

| | `engine=host` | `engine=device` |
| --- | --- | --- |
| 每窗开销 | `FREQ` + `SPEC` 两个往返 | 0 个往返（设备自己轮） |
| 实测窗/秒 | **约 320**（本机逐窗驱动，模拟源） | **10.2**（bins=1024）/ **16.9**（bins=512）@60 ms 驻留 |
| 串口占用 | 命令与帧交替，谁也堵不住谁 | **帧是瓶颈**：设备吐帧受串口带宽限制 |
| 抓档 / 调增益 | 随时可插 | 必须 `FSM STOP` 让路（`SweepLinkFacade.pause_around`） |

为什么设备自扫反而慢：射频时间没变，**串口把帧吐出来才是瓶颈**。`bins=1024` 时一帧约
2.7 KB，20 MS/s 采样下串口吃满也就 10 窗/秒左右；降到 512 bin 才 16.9 窗/秒。所以
`engine=device` 的价值不是「更快」，是「主机不在时也能扫」以及「命令往来不再挤在每窗里」。

### `plan_dwell_ms`：设备计划表的节流阀

固件按模块下发的计划表轮扫，每窗驻留取计划表里的 `dwell_ms`。主机的 `plan_dwell_ms`
（默认 20）给这个值**封顶**：

| 值 | 实测 |
| --- | --- |
| 60（模块原值） | 16.89 窗/秒（bins=512）——驻留被如实兑现 |
| 20 | **30.72 窗/秒**（30 s 真板实测） |

调小它不是「射频更快」，是把每窗实际采样时间压短、把更多时间让给轮扫；代价是单帧噪声
变大。**默认 20 是标定过的折中**，改它要重跑一次 `sweep_real.py` 看 `frames_per_s` 和
各模块表格行数有没有掉。

### 后台排空线程（为什么要它）

设备自扫时帧是「不请自来」的：`FSM START` 之后串口会一直吐 `SPEC` 头 + `SPC1` 帧。
如果只在主机想取帧时才去读，串口缓冲会积压、然后一次性灌进来一大坨过期数据。所以
`core/sweep_source.py` 起一个后台线程**持续排空**，按窗缓存最近一帧；主机的 `_fetch()`
只是从缓存里取。

由此带来两条语义，写清楚免得踩：

1. **缓存里的「陈旧窗」照样可用**：设备本来一轮才回到某个频点，`stale_s` 之内的帧算
   有效数据；`_fetch()` 取走也不弹出（下个模块可能还要同一个窗）。
2. **`pace` 要诚实**：`SweepSource(pace=True)` 取到帧后补 `sleep(dwell_ms)`，因为设备
   确实花了这段射频时间去采；`mock` 下 `pace=False`，否则跑测试要白等真实时间。

另外 `FSM STOP` 之后串口里还压着已发出的帧，所以停止流程是「边排空边找 `FSMEND`」，
不是发完命令就当停干净了。

### 真板实测（COM56，`testing/sweep_real.py`）

```
$ python testing/sweep_real.py --seconds 30
[engine] device — device sweep
{ "seconds": 30.0, "frames": 301, "misses": 0, "distinct_mhz": 90,
  "snapshots": 235, "frames_per_s": 9.75, ... }
[OK]

$ python testing/sweep_real.py --seconds 30 --plan-dwell 20
{ "frames_per_s": 30.72, ... }          # 驻留封顶后窗/秒上来了
```

`frames` 是设备自扫的计数（`FSM?` 的 `frames` 字段）；`engine=host` 时没有设备自扫源，
同一个报表改用**喂给模块的快照数**并标注 `frames_src: "host-snapshots"`——数字来自哪条
口径一定写在报表里，不许两种口径混着比。

### 回归口径

- `python -m unittest discover -s tests -t .` —— 含 `tests/test_sweep_source.py`（9 条，假固件）
- `cd host && python tests/test_sweep_session.py -v` —— 会话级（两引擎都跑）
- `python app.py --selftest --seconds 10 --mock [--engine device]` —— 两条引擎都要 `ok:true`
- `python testing/fsm_probe.py` —— 只有这一条碰真板（端口在脚本里固定 COM56）

**验证范围说明**：上面的窗/秒数字是一块 C5 板 + 一条 USB 线的实测值，换板/换线/换 USB
口会变；`engine=device` 的帧率是**串口带宽的函数**，不是固件能力的上限。