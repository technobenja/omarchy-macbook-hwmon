"""Acceptance check 8: rows older than 24h are aggregated then deleted --
tested with an injected clock on a throwaway DB, never by waiting.

`Store.aggregate_and_prune` takes `now` as an explicit argument (it never
calls `time.time()` itself), so every test here drives retention purely by
choosing timestamps -- no real sleeping anywhere in this file.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hwmon import store

DAY = 24 * 3600


def _snap(ts: float, *, package_c=50.0, fan_rpm=1300, power_w=-10.0, pct=80, temp_c=30.0, usage_pct=10.0) -> dict:
    return {
        "ts": ts,
        "cpu": {"package_c": package_c, "usage_pct": usage_pct},
        "fan": {"rpm": fan_rpm},
        "battery": {"power_w": power_w, "pct": pct, "temp_c": temp_c},
    }


class RetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-store-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.db_path = self._tmp / "throwaway.db"
        self.st = store.Store(self.db_path)
        self.addCleanup(self.st.close)
        # A fixed, arbitrary "now" -- never time.time().
        self.now = 2_000_000_000.0

    def test_recent_rows_survive_as_raw(self) -> None:
        self.st.insert_raw(_snap(self.now - 3600))  # 1h ago
        self.st.aggregate_and_prune(now=self.now)
        self.assertEqual(self.st.raw_row_count(), 1)

    def test_old_rows_are_aggregated_then_deleted(self) -> None:
        old_ts = self.now - (25 * 3600)  # 25h ago: past the 24h raw retention
        self.st.insert_raw(_snap(old_ts, package_c=42.0))
        self.st.aggregate_and_prune(now=self.now)

        # Deleted from raw...
        self.assertEqual(self.st.raw_row_count(), 0)

        # ...but the value was captured in the minute table BEFORE deletion,
        # not silently dropped.
        bucket = int(old_ts // 60) * 60
        row = self.st._conn.execute(
            "SELECT cpu_package_c_avg FROM minute WHERE ts_min = ?", (bucket,)
        ).fetchone()
        self.assertIsNotNone(row, "row should have been aggregated into `minute` before pruning")
        self.assertEqual(row[0], 42.0)

    def test_rows_older_than_30_days_do_not_persist_even_as_minute_aggregate(self) -> None:
        ancient_ts = self.now - (40 * DAY)  # older than both retention tiers
        self.st.insert_raw(_snap(ancient_ts, package_c=99.0))
        self.st.aggregate_and_prune(now=self.now)

        self.assertEqual(self.st.raw_row_count(), 0)
        self.assertEqual(self.st.minute_row_count(), 0)

    def test_minute_aggregate_min_avg_max(self) -> None:
        bucket_start = self.now - (25 * 3600)
        # Three samples in the same 60s bucket.
        self.st.insert_raw(_snap(bucket_start + 0, package_c=40.0))
        self.st.insert_raw(_snap(bucket_start + 10, package_c=60.0))
        self.st.insert_raw(_snap(bucket_start + 20, package_c=50.0))
        self.st.aggregate_and_prune(now=self.now)

        bucket = int(bucket_start // 60) * 60
        row = self.st._conn.execute(
            "SELECT cpu_package_c_min, cpu_package_c_avg, cpu_package_c_max FROM minute WHERE ts_min = ?",
            (bucket,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row, (40.0, 50.0, 60.0))

    def test_null_metric_values_do_not_poison_min_avg_max(self) -> None:
        bucket_start = self.now - (25 * 3600)
        self.st.insert_raw(_snap(bucket_start + 0, package_c=None))  # unreadable sample
        self.st.insert_raw(_snap(bucket_start + 10, package_c=55.0))
        self.st.aggregate_and_prune(now=self.now)

        bucket = int(bucket_start // 60) * 60
        row = self.st._conn.execute(
            "SELECT cpu_package_c_min, cpu_package_c_avg, cpu_package_c_max FROM minute WHERE ts_min = ?",
            (bucket,),
        ).fetchone()
        self.assertEqual(row, (55.0, 55.0, 55.0))  # NULLs skipped, not averaged in as 0

    def test_multiple_calls_are_idempotent_for_a_still_current_bucket(self) -> None:
        recent_ts = self.now - 10
        self.st.insert_raw(_snap(recent_ts, package_c=70.0))
        self.st.aggregate_and_prune(now=self.now)
        self.st.aggregate_and_prune(now=self.now)  # calling again must not error or duplicate
        self.assertEqual(self.st.raw_row_count(), 1)
        self.assertEqual(self.st.minute_row_count(), 1)

    def test_uses_the_injected_clock_not_wall_time(self) -> None:
        # A "now" far in the future relative to wall-clock time still prunes
        # correctly purely from the argument -- proves no time.time() call
        # is hiding inside aggregate_and_prune.
        far_future = 4_000_000_000.0  # year ~2096, long after any real "now"
        self.st.insert_raw(_snap(far_future - 3600))
        self.st.aggregate_and_prune(now=far_future)
        self.assertEqual(self.st.raw_row_count(), 1)


if __name__ == "__main__":
    unittest.main()
