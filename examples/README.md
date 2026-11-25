# examples/

本目录放**跑在电脑上的应用层**（host 侧 Python），与仓库主体（`main/` 的
ESP-IDF 固件）通过串口协议解耦。固件保持"哑射频前端"，业务逻辑都在这里。

| 例子 | 内容 |
| --- | --- |
| [`esp32c5-rf-monitor/`](esp32c5-rf-monitor/) | 基于 ESP32-C5 射频监测的五功能分治应用（WiFi 体检 / 瀑布图 / BLE 风险 / 5.8G 守卫 / 可疑抓拍） |

```sh
cd esp32c5-rf-monitor
python -m app.demo                 # 无硬件全链路演示
python -m unittest discover -s tests -t .
```

设计约束（详见各例子的 `README.md` / `docs/`）：

- 固件协议是唯一耦合面，解析全部按 `docs/protocol.md`、`docs/spectrum.md`。
- 功率码是相对 dB 码，不是校准过的 dBm；不许在输出里写 dBm。
- 分时快照，不是连续实时流。