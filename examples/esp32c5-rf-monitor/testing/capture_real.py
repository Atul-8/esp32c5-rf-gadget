"""真板抓档链路自证：证明「CAP20 → 解析 → 存盘 + sidecar」在硬件上真的走通。

    python testing/capture_real.py --port COM56 --gain-index 60

它**不**证明「能自己发现偷拍设备」——本房间没有可疑源，发现能力由
``tests/test_capture.py``（mock 场景）与回放注入保证。本脚本只回答一个问题：
固件 CAP20 回来的 I/Q 能不能被正确解析、落盘、做哈希、写进 sidecar。

做法：真板抓一次 20 MS/s / 20 bit / 16380 样本的中频突发，然后**强行**构造一个候选
（频点取刚才的抓档频点，参数用假的），走 ``CaptureMode.deliver()`` 的真实存盘路径。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bind_capture  # noqa: E402
from core.classifier import SignalFeatures, classify  # noqa: E402
from core.registry import Registry  # noqa: E402
from core.serial_link import EspSdrLink  # noqa: E402
from modes._legacy.capture import (  # noqa: E402
    CAPTURE_BITS,
    CAPTURE_RATE_CODE,
    FIRMWARE_MAX_SAMPLES,
    CaptureMode,
    CaptureRequest,
    Candidate,
    kind_of,
)
from output.format import ascii_table, fmt_hz_short  # noqa: E402

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data", "capture-real"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="真板抓档链路自证")
    parser.add_argument("--port", default="COM56")
    parser.add_argument("--baud", type=int, default=2_000_000)
    parser.add_argument("--freq-mhz", type=int, default=2437, help="抓档中心频率（固件只收整 MHz）")
    parser.add_argument("--samples", type=int, default=FIRMWARE_MAX_SAMPLES)
    parser.add_argument("--gain-index", type=int, default=60)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    if args.samples > FIRMWARE_MAX_SAMPLES:
        print(f"! samples 超过固件上限 {FIRMWARE_MAX_SAMPLES}", file=sys.stderr)
        return 2

    link = EspSdrLink(args.port, args.baud)
    registry = Registry()
    plugin = CaptureMode()
    registry.register(plugin)
    try:
        link.open()
        link.set_gain("MANUAL", args.gain_index)
        print(f"固件：{link.identity}")
        print(f"能力：{' '.join(link.caps) or '（未通告）'}")
        freq_hz = int(args.freq_mhz) * 1_000_000
        print(f"抓档：{fmt_hz_short(freq_hz)} / {args.samples} 样本 / {CAPTURE_BITS} bit / rate_code {CAPTURE_RATE_CODE}")

        capture = link.capture_iq(args.samples, CAPTURE_RATE_CODE, CAPTURE_BITS, freq_hz)
        print(
            f"固件回报：I {len(capture.samples_i)} / Q {len(capture.samples_q)} 对、"
            f"bit {capture.bits}、rate_code {capture.rate_code}、{capture.duration_us} µs、"
            f"名义采样率 {capture.sample_rate_hz/1e6:.1f} MS/s"
        )
        wire = capture.to_stream_bytes()
        per_pair = 2.5 if capture.bits == 20 else 2.0
        expect_wire = int(args.samples * per_pair)
        print(f"线上格式：{len(wire)} B（{capture.bits} bit → {per_pair} B/对，期望 {expect_wire} B）")
        if len(wire) != expect_wire:
            print("! 线上字节数与位宽不符", file=sys.stderr)
            return 1

        bind_capture(registry, lambda *a, **k: capture, out_dir=args.out)
        wide = (max(capture.samples_i) - min(capture.samples_i)) > 0
        features = SignalFeatures(bw_hz=200_000.0, duration_s=12.0, duty_cycle=0.9)
        candidate = Candidate(
            center_hz=float(freq_hz),
            width_hz=200_000.0,
            fill=0.9,
            frames=12,
            peak_excess_db=35.0,
            window_hz=float(freq_hz),
            kind=kind_of(200_000.0, 0.9),
            classification=classify(features),
            first_seen=0.0,
            last_seen=12.0,
        )
        request = CaptureRequest(
            freq_hz=freq_hz,
            samples=args.samples,
            rate_code=CAPTURE_RATE_CODE,
            bits=CAPTURE_BITS,
            candidate=candidate,
        )
        record = plugin.deliver(request, capture)
        if record is None:
            print("! 存盘失败（见 plugin.errors）", file=sys.stderr)
            for error in plugin.errors:
                print(f"  {error}", file=sys.stderr)
            return 1

        size = os.path.getsize(record.path)
        digest = hashlib.sha256(open(record.path, "rb").read()).hexdigest()
        sidecar = json.load(open(record.sidecar, encoding="utf-8"))
        print()
        print(
            ascii_table(
                ["项", "值"],
                [
                    ["文件", record.path],
                    ["字节", f"{size}（期望 {args.samples * 4}）"],
                    ["sha256 一致", str(digest == record.sha256)],
                    ["峰值 / RMS", f"{record.peak} / {record.rms:.1f}"],
                    ["峰峰值 > 0", str(wide)],
                    ["分类", f"{record.label} / {record.confidence:.2f}"],
                    ["sidecar context", sidecar.get("context_note", "(有 context)")],
                    ["sidecar 字段", ", ".join(sorted(sidecar.keys()))],
                ],
                title="抓档产出",
            )
        )
        # 存档统一 int16 LE 交错（每对 4 B）；线上位宽只影响上面的 to_stream_bytes()
        ok = size == args.samples * 4 and digest == record.sha256 and wide
        print("结论：" + ("抓档链路通（真机 I/Q → 存盘 → 哈希 → sidecar）" if ok else "抓档链路有异常"))
        return 0 if ok else 1
    finally:
        link.close()


if __name__ == "__main__":
    raise SystemExit(main())