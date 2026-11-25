"""回放注入自检：拿**真板录下的**快照，凭空塞一个 5.8G 图传形状的信号进去，
看 ``modes/guard_58g.py`` 会不会按预期报警。

为什么要这个：手上没有真的 5.8G 图传发射器，直接「在空房间里跑」只能证明**不误报**，
证明不了「真来了能抓到」。回放注入用的是真板实测的噪声底、真时间戳、真前端形状，
只是在数据里加一份合成信号 —— 抓不到就说明判据有问题，不用等硬件也能自证。

用法（在 ``examples/esp32c5-rf-monitor`` 下）::

    python testing/guard_replay.py --db .data/live.db                    # 5795 MHz / 4 MHz 宽
    python testing/guard_replay.py --db .data/live.db --freq-mhz 5720 --width-mhz 8 --lift 20

退出码：0 = 按预期报警；1 = 没报警（判据太严 / 数据不够）；2 = 参数或数据有问题。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.snapshot import Snapshot  # noqa: E402
from modes._legacy.guard_58g import Guard58G, MIN_FRAMES, SCAN_BW_HZ  # noqa: E402


def load_snapshots(db_path: str, module: str = "guard_58g") -> list[Snapshot]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "select ts, center_hz, bw_hz, codes from snapshots where module=? order by ts",
            (module,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for ts, center, bw, codes in rows:
        values = list(codes) if isinstance(codes, (bytes, bytearray)) else list(codes)
        out.append(Snapshot(ts=ts, center_hz=center, bw_hz=bw, bins_db=values, meta={"replay": True}))
    return out


def inject(snap: Snapshot, freq_hz: float, width_hz: float, lift: float) -> Snapshot:
    """在频点附近把一段 ``width_hz`` 宽的区域抬高 ``lift`` 码（模拟图传的宽平顶）。"""
    bins = len(snap.bins_db)
    bin_hz = snap.bw_hz / bins
    lo = freq_hz - width_hz / 2
    hi = freq_hz + width_hz / 2
    values = list(snap.bins_db)
    touched = 0
    for index in range(bins):
        f = snap.center_hz - snap.bw_hz / 2 + (index + 0.5) * bin_hz
        if lo <= f < hi:
            values[index] = min(255, int(round(values[index] + lift)))
            touched += 1
    return Snapshot(
        ts=snap.ts,
        center_hz=snap.center_hz,
        bw_hz=snap.bw_hz,
        bins_db=values,
        meta=dict(snap.meta, replay=True, injected_bins=touched),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="5.8G 守卫回放注入自检")
    parser.add_argument("--db", required=True, help="真板录下的库（app.live --db 产出的）")
    parser.add_argument("--freq-mhz", type=float, default=5795.0, help="注入频点（默认 5795，实板这一带最干净）")
    parser.add_argument("--width-mhz", type=float, default=4.0, help="注入带宽（图传一般 2~8 MHz）")
    parser.add_argument("--lift", type=float, default=25.0, help="抬升码数（相对底噪）")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"!! 库不存在：{args.db}", file=sys.stderr)
        return 2
    snaps = load_snapshots(args.db)
    if not snaps:
        print("!! 库里没有 guard_58g 的快照（app.live 跑的时候模块必须启用）", file=sys.stderr)
        return 2
    freq_hz = args.freq_mhz * 1e6
    injected = [
        inject(snap, freq_hz, args.width_mhz * 1e6, args.lift)
        if abs(snap.center_hz - freq_hz) <= SCAN_BW_HZ / 2
        else snap
        for snap in snaps
    ]
    guard = Guard58G()
    for snap in injected:
        guard.on_snapshot(snap)
    guard.analyze()
    output = guard.produce()

    span = guard.last_ts - guard.started_at if guard.started_at else 0.0
    print(
        f"回放：{len(snaps)} 帧 / {span:.1f} s / {len(guard.windows)} 窗；"
        f"注入 {args.freq_mhz:.1f} MHz ±{args.width_mhz/2:.1f} MHz，抬升 {args.lift:.0f} 码"
    )
    print(output.render())
    hits = [a for a in guard.alerts if abs(a.center_hz / 1e6 - args.freq_mhz) <= args.width_mhz]
    if not hits:
        print("!! 没抓到注入的信号 —— 判据太严或该窗帧数不足（< MIN_FRAMES）", file=sys.stderr)
        return 1
    best = max(hits, key=lambda a: a.peak_excess_db)
    ok = (
        best.kind in ("持续窄带", "持续宽带")
        and abs(best.bw_hz - args.width_mhz * 1e6) <= 1.5e6
        and best.fill >= 0.75
        and best.frames >= MIN_FRAMES
        and best.duration_s > 0
    )
    print(
        f"结论：{'抓到' if ok else '抓到了但字段不符'} → {best.alert_id} {best.kind} "
        f"{best.center_hz/1e6:.1f} MHz / {best.bw_hz/1e6:.1f} MHz / 抬升 {best.peak_excess_db:.1f} / "
        f"占空比 {best.fill:.0%} / 持续 {best.duration_s:.1f} s / {best.frames} 帧"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())