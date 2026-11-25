"""存储：SQLite 主存档 + CSV 旁路双写。

所有报告/瀑布图/告警都从这份存档读，不各自另存格式。
功率码以 BLOB 存原始字节（2048 bin 也只有 2 KB），meta 以 JSON 存。
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterator, Sequence

from .snapshot import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    iso_time    TEXT    NOT NULL,
    module      TEXT,
    center_hz   INTEGER NOT NULL,
    bw_hz       INTEGER NOT NULL,
    bins        INTEGER NOT NULL,
    codes       BLOB    NOT NULL,
    noise_db    REAL,
    mean_db     REAL,
    peak_db     REAL,
    peak_hz     REAL,
    meta        TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots (ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_freq ON snapshots (center_hz, ts);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    iso_time    TEXT NOT NULL,
    module      TEXT,
    kind        TEXT NOT NULL,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""

CSV_COLUMNS = [
    "ts",
    "iso_time",
    "module",
    "center_hz",
    "bw_hz",
    "bins",
    "noise_db",
    "mean_db",
    "peak_db",
    "peak_hz",
]


@dataclass
class Event:
    ts: float
    module: str
    kind: str
    payload: dict


class Store:
    """SQLite 存档；``csv_path`` 给出时额外追加一份人类可读的 CSV。"""

    def __init__(self, db_path: str = ":memory:", csv_path: str | None = None) -> None:
        self.db_path = db_path
        self.csv_path = csv_path
        if db_path != ":memory:":
            directory = os.path.dirname(os.path.abspath(db_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._csv_file = None
        self._csv_writer = None
        if csv_path:
            self._open_csv(csv_path, append=True)

    # ---- CSV -------------------------------------------------------------
    def _open_csv(self, csv_path: str, append: bool) -> None:
        directory = os.path.dirname(os.path.abspath(csv_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        exists = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
        self._csv_file = open(csv_path, "a" if append else "w", newline="", encoding="utf-8")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=CSV_COLUMNS)
        if not (append and exists):
            self._csv_writer.writeheader()
        self._csv_file.flush()

    # ---- 写入 ------------------------------------------------------------
    def add(self, snap: Snapshot, module: str | None = None) -> int:
        module = module or str(snap.meta.get("module", "")) or None
        peak_hz, peak_db = snap.peak()
        row = (
            snap.ts,
            snap.ts_iso,
            module,
            snap.center_hz,
            snap.bw_hz,
            snap.bin_count,
            bytes(int(min(255, max(0, round(v)))) for v in snap.bins_db),
            snap.noise_floor_db,
            snap.mean_db,
            peak_db,
            peak_hz,
            json.dumps(dict(snap.meta), ensure_ascii=False, default=str),
        )
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO snapshots (ts, iso_time, module, center_hz, bw_hz, bins, codes,"
                " noise_db, mean_db, peak_db, peak_hz, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        if self._csv_writer is not None:
            self._csv_writer.writerow(
                {
                    "ts": f"{snap.ts:.3f}",
                    "iso_time": snap.ts_iso,
                    "module": module or "",
                    "center_hz": snap.center_hz,
                    "bw_hz": snap.bw_hz,
                    "bins": snap.bin_count,
                    "noise_db": f"{snap.noise_floor_db:.1f}",
                    "mean_db": f"{snap.mean_db:.1f}",
                    "peak_db": f"{peak_db:.1f}",
                    "peak_hz": f"{peak_hz:.0f}",
                }
            )
            self._csv_file.flush()
        return int(cursor.lastrowid or 0)

    def add_many(self, snaps: Sequence[Snapshot], module: str | None = None) -> int:
        return sum(1 for snap in snaps if self.add(snap, module=module) >= 0)

    def add_event(self, module: str, kind: str, payload: dict, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
        with self._conn:
            self._conn.execute(
                "INSERT INTO events (ts, iso_time, module, kind, payload) VALUES (?,?,?,?,?)",
                (ts, iso, module, kind, json.dumps(payload, ensure_ascii=False, default=str)),
            )

    # ---- 查询 ------------------------------------------------------------
    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])

    def count_by_module(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT COALESCE(module, ''), COUNT(*) FROM snapshots GROUP BY 1 ORDER BY 1"
        ).fetchall()
        return {str(k): int(v) for k, v in rows}

    def time_span(self) -> tuple[float, float] | None:
        row = self._conn.execute("SELECT MIN(ts), MAX(ts) FROM snapshots WHERE 1").fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0]), float(row[1])

    def iter_between(
        self,
        t0: float,
        t1: float,
        center_hz: int | None = None,
        module: str | None = None,
    ) -> Iterator[Snapshot]:
        query = (
            "SELECT ts, center_hz, bw_hz, bins, codes, meta, module FROM snapshots"
            " WHERE ts >= ? AND ts <= ?"
        )
        params: list[object] = [t0, t1]
        if center_hz is not None:
            query += " AND center_hz = ?"
            params.append(center_hz)
        if module is not None:
            query += " AND module = ?"
            params.append(module)
        query += " ORDER BY ts ASC, id ASC"
        for ts, center, bw, bins, codes, meta, module in self._conn.execute(query, params):
            stored = json.loads(meta) if meta else {}
            if module:
                stored.setdefault("module", module)
            yield Snapshot(
                ts=float(ts),
                center_hz=int(center),
                bw_hz=int(bw),
                bins_db=tuple(float(b) for b in bytes(codes)),
                meta=stored,
            )

    def iter_all(self) -> Iterator[Snapshot]:
        span = self.time_span()
        if span is None:
            return iter(())
        return self.iter_between(span[0] - 1e-6, span[1] + 1e-6)

    def events(self, module: str | None = None) -> list[Event]:
        query = "SELECT ts, module, kind, payload FROM events"
        params: list[object] = []
        if module is not None:
            query += " WHERE module = ?"
            params.append(module)
        query += " ORDER BY ts ASC, id ASC"
        return [
            Event(float(ts), str(mod), str(kind), json.loads(payload) if payload else {})
            for ts, mod, kind, payload in self._conn.execute(query, params)
        ]

    def export_csv(self, path: str) -> str:
        """把整库导出成一份 CSV（含噪声底/均值/峰值列）。"""
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for snap in self.iter_all():
                peak_hz, peak_db = snap.peak()
                writer.writerow(
                    {
                        "ts": f"{snap.ts:.3f}",
                        "iso_time": snap.ts_iso,
                        "module": snap.meta.get("module", ""),
                        "center_hz": snap.center_hz,
                        "bw_hz": snap.bw_hz,
                        "bins": snap.bin_count,
                        "noise_db": f"{snap.noise_floor_db:.1f}",
                        "mean_db": f"{snap.mean_db:.1f}",
                        "peak_db": f"{peak_db:.1f}",
                        "peak_hz": f"{peak_hz:.0f}",
                    }
                )
        return path

    def close(self) -> None:
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["CSV_COLUMNS", "Event", "Store"]