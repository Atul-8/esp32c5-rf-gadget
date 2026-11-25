"""SQLite + CSV 双写。"""
import csv
import os
import tempfile
import unittest

from core.snapshot import Snapshot
from core.store import Store


def make_snap(ts: float, center: int = 2_442_000_000, peak_index: int = 100) -> Snapshot:
    bins = [12] * 512
    bins[peak_index] = 200
    return Snapshot(ts=ts, center_hz=center, bw_hz=80_000_000, bins_db=tuple(bins), meta={"seq": 1})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c5mon-store-")
        self.db = os.path.join(self.tmp, "monitor.db")
        self.csv_path = os.path.join(self.tmp, "monitor.csv")

    def test_insert_query_roundtrip(self):
        with Store(self.db, csv_path=self.csv_path) as store:
            store.add(make_snap(100.0), module="wifi_check")
            store.add(make_snap(101.0, center=5_745_000_000), module="guard_58g")
            self.assertEqual(store.count(), 2)
            self.assertEqual(store.count_by_module(), {"guard_58g": 1, "wifi_check": 1})
            rows = list(store.iter_between(99.0, 101.0))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].bin_count, 512)
            self.assertEqual(rows[0].bins_db[100], 200)
            self.assertEqual(rows[0].meta["module"], "wifi_check")
            filtered = list(store.iter_between(99.0, 101.0, center_hz=5_745_000_000))
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0].center_hz, 5_745_000_000)

    def test_csv_sidecar(self):
        with Store(self.db, csv_path=self.csv_path) as store:
            store.add(make_snap(100.0), module="probe")
            export_path = os.path.join(self.tmp, "export.csv")
            store.export_csv(export_path)
        with open(self.csv_path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["module"], "probe")
        self.assertEqual(rows[0]["bins"], "512")
        with open(export_path, newline="", encoding="utf-8") as handle:
            exported = list(csv.DictReader(handle))
        self.assertEqual(len(exported), 1)

    def test_reopen_keeps_rows_and_appends_csv(self):
        with Store(self.db, csv_path=self.csv_path) as store:
            store.add(make_snap(100.0))
        with Store(self.db, csv_path=self.csv_path) as store:
            store.add(make_snap(102.0))
            self.assertEqual(store.count(), 2)
        with open(self.csv_path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)  # 表头只写一次

    def test_events(self):
        with Store(self.db) as store:
            store.add_event("guard_58g", "alert", {"freq_hz": 5_740_000_000}, ts=500.0)
            events = store.events("guard_58g")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "alert")
        self.assertEqual(events[0].payload["freq_hz"], 5_740_000_000)

    def test_memory_store(self):
        with Store() as store:
            store.add(make_snap(1.0))
            self.assertEqual(store.count(), 1)
            self.assertIsNotNone(store.time_span())


if __name__ == "__main__":
    unittest.main()