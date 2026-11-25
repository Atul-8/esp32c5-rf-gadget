# 09 · 串口自保：命令前排空 + 失效自愈

> 现象：上位机（尤其**周期瀑布图页**）跑一会儿顶栏变「未连接」，日志一条
> `[RF error] SerialLinkError: 连接已标记失效，请重新打开`，之后只能重启上位机。
> 本文记录根因链、修法、真板实测与边界。**`core/` 的协议解析与判据一个字没改。**

## 1. 现象与误判

人类最初判断是「换页状态机 / 调度把链路搞坏了」，建议瀑布页改用实时频谱页那套调度。
查证后**不是**：

| 查证点 | 结论 |
| --- | --- |
| 是否与换页/FSM 有关 | 默认引擎 `host` 下不跑固件 FSM（`scan_fsm.c` 只在 `--engine device` 生效）；60 s 真板自检换页 6 次、400+ 窗，`device.frames 144 / misses 0`，**没掉链** |
| 掉链入口有几个 | 全项目只有 2 处置 `failed = True`：`core/serial_link.py` 的「帧 magic 不匹配」与「帧解码失败」 |
| 为什么「只有瀑布页」 | 瀑布页是唯一让人愿意长时间盯着的页面（一轮 44 窗 ≈ 4.4 s，累积 240 行）。掉链概率约 1/400~1/600 个窗 —— 看得越久越容易撞上，与页面调度无关 |
| 撞上后为什么只能重启 | `EspSdrLink.open()` **不复位** `failed`，`scan()` 见 `failed` 直接抛；RF 循环连续 5 片失败 → 自动停机 → 界面「未连接」 |

## 2. 根因链

```
一窗 SPEC 的 SPECEND 之后设备仍可能吐收尾帧（drops > 0 时更明显）
      → 主机没把这些字节读干净
      → 下一条文本命令（FREQ / GAIN / SPEC）的 readline() 把二进制帧当"行"读掉一部分
      → 随后的 _read_exact(4) 落在帧中间
      → magic 不匹配 → failed = True（core 自此永不再试）
      → RF 循环连续失败 5 片 → 自动停机 → 界面「未连接」
```

## 3. 修法

| 位置 | 改了什么 |
| --- | --- |
| `host/link_guard.py`（新） | `ResilientLink(EspSdrLink)`：① 每条文本命令 `_write()` 前排空读缓冲残留；② `scan()` 发现 `failed` 先 `reopen()`（重开串口 + 重握手 + 复位失效标记），3 s 冷却防热循环；③ 暴露 `reopen_count` / `last_reopen_at` |
| `host/session.py` | 链路换成 `ResilientLink`；新增 `relink()`：停自扫 → 重开链路 → **重设增益** → 重建自扫；补 `relink_ok/failed/closed` 中英文案 |
| `host/app.py` | RF 循环「连续失败 → 自动停机」之前先 `relink()`：成功即清零续跑并打日志；冷却中歇 3 s 再试；真失败才停机（单次采集上限 20 次） |
| `core/sweep_source.py` | ① 修「计划外频点」的**双读者** bug（自扫期间直接读串口会和 drain 线程抢）→ `pause_around`；② drain 读到 `failed` 时自行 `reopen()` 并重发计划；③ `stats()` 增加 `reopens` |
| `host/l10n.py` | 补 `无覆盖 / 有覆盖 / 未采样`（真板安静频段会落到「无覆盖」档，假场景不触发） |

**为什么重开后必须重设增益**：真板重开串口会 DTR 复位设备，`GAIN?` 回到 `GAIN HARDWARE -1`。
`relink()` 里的 `self.gain.apply(link)` 就是为这一步补的。

## 4. 真板实测（COM56）

```
$ python testing/probe_resilient.py --windows 24
[link] C5SDR 6 burst 16380  caps=20  transport=USB
[1] 24 窗：1751 帧 / 2.9s / 失败 0
[2] failed=True 之后 scan：66 帧 / 重开次数 0 → 1 / failed=False / err=-
[3] 重同步后 GAIN? → GAIN HARDWARE -1 0 81 0
[3] 重同步后 5180MHz：72 帧 / 表头 5180000000 Hz / bins 512 / status 0 / err=-
[OK]
```

第 3 行是「重设增益」的直接证据：重开后档位已回落 AGC，靠 `relink()` 补设。

其余回归：

| 命令 | 结果 |
| --- | --- |
| `cd host && python tests/test_link_guard.py -v` | 6 tests OK |
| `cd host && python tests/test_session.py -v` | 29 tests OK（含 3 条 relink） |
| `python -m unittest discover -s tests -t .`（分析层） | 280 tests OK |
| `python app.py --selftest --real --port COM56 --gain-index 60 --seconds 12` | `EXIT=0`，52/52 全绿，`hits 0`，`device.frames 146 / misses 0` |
| `python app.py --selftest --mock --engine device --seconds 10` | `EXIT=0`，52/52 全绿 |
| **故障注入端到端**：`ESPSDR_FAULT_FAILED_AFTER_S=4 python app.py --selftest --real --port COM56 --gain-index 60 --seconds 12` | `EXIT=0`、`fault_injected:true`、52/52 全绿、`device.frames 260 / misses 0` |

**故障注入**（`ESPSDR_FAULT_FAILED_AFTER_S=<秒>`，只在自检/回归用）：跑满 N 秒后人为把
`link.failed` 置位，报告里以 `fault_injected` 留档。它就为证明「GUI 里这条恢复路径真的接上了」
而存在 —— 不接的话 RF 线程 5 片内自动停机，所有页面探针会全空。

## 5. 边界

1. **真拔线只能如实报错**：`reopen()` 失败保留 `failed`，RF 循环照常自动停机，不假装在采集；
2. **自愈会丢一窗**：重开要 1~3 s，那窗帧拿不回来（瀑布图缺一格，不补）；
3. **不改 `core/` 判据**：`serial_link.py` 的解析与错误分类未动；`sweep_source.py` 的改动是修实现 bug（双读者），不是判据；
4. `--engine device` 下自扫掉链靠 `reopen()` + 重发计划，真断线仍由上层「自动停机」兜底。