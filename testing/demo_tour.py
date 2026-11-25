#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""圆屏自动演示开关：RD DEMO 开始 / RD DEMO OFF 停止。

编排（在固件里跑，断开电脑也继续）：
  页 1 扫描光标转 1.5 圈 -> 页 2 下翻一页、停 2.5 s、上翻回来
  -> 页 3 钉窗驻留 10 s -> 回页 1 循环

用法：
  python testing/demo_tour.py                # 开始演示（Ctrl+C 停止）
  python testing/demo_tour.py --stop         # 只发停止
  python testing/demo_tour.py --port COM56   # 换口

注意：打开串口会复位板子，脚本会先等固件就绪（RD? 有回包）再发命令。
"""
import argparse
import sys
import time

import serial


def main():
    ap = argparse.ArgumentParser(description="圆屏自动演示开关")
    ap.add_argument("--port", default="COM56")
    ap.add_argument("--baud", type=int, default=2000000)
    ap.add_argument("--stop", action="store_true", help="只发 RD DEMO OFF")
    a = ap.parse_args()

    ser = serial.Serial(a.port, a.baud, timeout=0.2)

    def send(cmd, wait=0.3):
        ser.reset_input_buffer()
        ser.write((cmd + "\n").encode("ascii"))
        time.sleep(wait)
        blob = ser.read(ser.in_waiting or 1).decode("ascii", "replace")
        return [l.strip() for l in blob.replace("\r", "").split("\n") if l.strip()]

    # 打开串口 = 复位板子，等固件真正起来再说话
    t0 = time.time()
    ok = False
    while time.time() - t0 < 12:
        if any(l.startswith("RD ") for l in send("RD?", 0.4)):
            ok = True
            break
    if not ok:
        print("板子 12 s 内没就绪（RD? 无回包），退出。")
        ser.close()
        sys.exit(1)

    cmd = "RD DEMO OFF" if a.stop else "RD DEMO"
    print(">> %s -> %s" % (cmd, send(cmd)))
    if a.stop:
        ser.close()
        return

    print("演示循环中：页1 光标1.5圈 -> 页2 翻页 -> 页3 停10s -> 回页1。Ctrl+C 停止。")
    try:
        while True:
            time.sleep(0.5)
            ser.read(ser.in_waiting or 1)   # 读掉串口尾巴，防缓冲涨满
    except KeyboardInterrupt:
        print()
    print(">> RD DEMO OFF -> %s" % send("RD DEMO OFF"))
    print(">> RD VIEW 0  -> %s" % send("RD VIEW 0"))
    ser.close()
    print("已停止并回到页 1。")


if __name__ == "__main__":
    main()
