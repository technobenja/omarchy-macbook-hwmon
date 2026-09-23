"""A16/A17: the `fan_target_rpm` column migration, the `events` table
(insert/dedupe/list/prune), and `fancurve_raw`/`raw_points_for_events`.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hwmon import events, store

DAY = 24 * 3600


def _snap(ts: float, *, pct=80, status="Discharging", package_c=50.0, fan_rpm=1300, target_rpm=None) -> dict:
    return {
        "ts": ts,
        "cpu": {"package_c": package_c, "usage_pct": 10.0},
        "fan": {"rpm": fan_rpm, "target_rpm": target_rpm},
        "battery": {"power_w": -10.0, "pct": pct, "temp_c": 30.0, "status": status},
    }


class FanTargetRpmMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-store-events-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.db_path = self._tmp / "hwmon.db"

    def test_fresh_db_has_fan_target_rpm_column(self) -> None:
        st = store.Store(self.db_path)
        self.addCleanup(st.close)
        cols = {row[1] for row in st._conn.execute("PRAGMA table_info(raw)")}
        self.assertIn("fan_target_rpm", cols)

    def test_pre_v3_db_without_the_column_is_migrated_on_open(self) -> None:
        # Build a v2-shaped `raw` table by hand (no fan_target_rpm column),
        # matching what a real pre-v3 hwmon.db looks like.
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            CREATE TABLE raw (
                ts REAL PRIMARY KEY,
                cpu_package_c REAL,
                fan_rpm REAL,
                battery_power_w REAL,
                battery_pct REAL,
                battery_temp_c REAL,
                cpu_usage_pct REAL,
                snapshot TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO raw (ts, cpu_package_c, fan_rpm, battery_power_w, battery_pct, "
            "battery_temp_c, cpu_usage_pct, snapshot) VALUES (1000.0, 50.0, 1300, -10.0, 80, 30.0, 10.0, '{}')"
        )
        conn.commit()
        conn.close()

        st = store.Store(self.db_path)
        self.addCleanup(st.close)
        cols = {row[1] for row in st._conn.execute("PRAGMA table_info(raw)")}
        self.assertIn("fan_target_rpm", cols)
        # The old row survives the migration, with NULL in the new column.
        row = st._conn.execute("SELECT fan_target_rpm FROM raw WHERE ts = 1000.0").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0])

    def test_insert_raw_populates_fan_target_rpm(self) -> None:
        st = store.Store(self.db_path)
        self.addCleanup(st.close)
        st.insert_raw(_snap(1000.0, target_rpm=2272))
        row = st._conn.execute("SELECT fan_target_rpm FROM raw WHERE ts = 1000.0").fetchone()
        self.assertEqual(row[0], 2272.0)

    def test_insert_raw_target_rpm_null_when_absent(self) -> None:
        st = store.Store(self.db_path)
        self.addCleanup(st.close)
        # A pre-v3 snapshot: fan has no target_rpm key at all.
        st.insert_raw(
            {
                "ts": 1000.0,
                "cpu": {"package_c": 1.0, "usage_pct": 1.0},
                "fan": {"rpm": 1},
                "battery": {"power_w": -1.0, "pct": 80, "temp_c": 30.0},
            }
        )
        row = st._conn.execute("SELECT fan_target_rpm FROM raw WHERE ts = 1000.0").fetchone()
        self.assertIsNone(row[0])


class EventsTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-store-events-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.db_path = self._tmp / "hwmon.db"
        self.st = store.Store(self.db_path)
        self.addCleanup(self.st.close)

    def _record(self, ts_start, **overrides) -> events.EventRecord:
        base = dict(
            ts_start=ts_start,
            ts_end=ts_start + 100.0,
            kind="hard_poweroff",
            last_pct=2,
            last_status="Discharging",
            detail="Dirty bit is set.",
            boot_id="boot-x",
        )
        base.update(overrides)
        return events.EventRecord(**base)

    def test_insert_and_list(self) -> None:
        self.st.insert_event(self._record(1000.0))
        rows = self.st.list_events(days=1, now=1000.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 1000.0)
        self.assertEqual(rows[0][2], "hard_poweroff")

    def test_insert_returns_true_on_new_row(self) -> None:
        self.assertTrue(self.st.insert_event(self._record(1000.0)))

    def test_duplicate_ts_start_is_ignored_not_an_error(self) -> None:
        # UNIQUE(ts_start) -- B2's shared dedupe key.
        self.assertTrue(self.st.insert_event(self._record(1000.0, kind="hard_poweroff")))
        self.assertFalse(self.st.insert_event(self._record(1000.0, kind="unclean_shutdown")))
        rows = self.st.list_events(days=1, now=1000.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "hard_poweroff", "the first-inserted row must survive, not be overwritten")

    def test_existing_event_ts_starts(self) -> None:
        self.st.insert_event(self._record(1000.0))
        self.st.insert_event(self._record(2000.0))
        self.assertEqual(self.st.existing_event_ts_starts(), {1000.0, 2000.0})

    def test_list_events_respects_days_window(self) -> None:
        now = 2_000_000.0
        self.st.insert_event(self._record(now - 10 * DAY))  # inside 30d window
        self.st.insert_event(self._record(now - 40 * DAY))  # outside
        rows = self.st.list_events(days=30, now=now)
        self.assertEqual(len(rows), 1)

    def test_events_pruned_after_30_days_by_aggregate_and_prune(self) -> None:
        now = 2_000_000_000.0
        self.st.insert_event(self._record(now - 40 * DAY))
        self.st.aggregate_and_prune(now=now)
        self.assertEqual(self.st.list_events(days=365, now=now), [])

    def test_recent_events_survive_aggregate_and_prune(self) -> None:
        now = 2_000_000_000.0
        self.st.insert_event(self._record(now - 1 * DAY))
        self.st.aggregate_and_prune(now=now)
        self.assertEqual(len(self.st.list_events(days=30, now=now)), 1)

    def test_last_pct_stored_as_int(self) -> None:
        self.st.insert_event(self._record(1000.0, last_pct=2))
        row = self.st._conn.execute("SELECT last_pct FROM events WHERE ts_start = 1000.0").fetchone()
        self.assertEqual(row[0], 2)
        self.assertIsInstance(row[0], int)


class RawPointsForEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-store-events-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.db_path = self._tmp / "hwmon.db"
        self.st = store.Store(self.db_path)
        self.addCleanup(self.st.close)

    def test_extracts_ts_pct_status_from_snapshot(self) -> None:
        self.st.insert_raw(_snap(1000.0, pct=42, status="Charging"))
        points = self.st.raw_points_for_events()
        self.assertEqual(points, [(1000.0, 42, "Charging")])

    def test_ordered_oldest_first(self) -> None:
        self.st.insert_raw(_snap(2000.0, pct=1))
        self.st.insert_raw(_snap(1000.0, pct=2))
        points = self.st.raw_points_for_events()
        self.assertEqual([p[0] for p in points], [1000.0, 2000.0])

    def test_empty_db_yields_empty_list(self) -> None:
        self.assertEqual(self.st.raw_points_for_events(), [])

    def test_status_null_when_snapshot_has_no_battery_status(self) -> None:
        # json_extract on a missing path yields SQL NULL -> None, same as
        # the old json.loads(...).get(...) fallback did.
        self.st.insert_raw({"ts": 1000.0, "cpu": {"package_c": 1.0, "usage_pct": 1.0}, "fan": {"rpm": 1}, "battery": {"power_w": -1.0, "pct": 80, "temp_c": 30.0}})
        points = self.st.raw_points_for_events()
        self.assertEqual(points, [(1000.0, 80, None)])


class RawPointsForEventsBenchmarkTests(unittest.TestCase):
    """S2 (advisor): a synthetic ~86,400-row raw table (the full 24 h raw
    retention at 1 Hz) must scan in well under the 1 s warning threshold
    with the `json_extract`-based implementation -- the original
    per-row-`json.loads` form was measured at ~0.26 s @ 9k rows,
    extrapolating to ~2.5 s @ 86k."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-store-bench-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.db_path = self._tmp / "hwmon.db"

    def test_86400_rows_scans_well_under_one_second(self) -> None:
        import json as _json
        import time as _time

        st = store.Store(self.db_path)
        self.addCleanup(st.close)

        n = 86_400
        rows = []
        for i in range(n):
            ts = float(i)
            snap = _snap(ts, pct=80, status="Discharging", package_c=50.0, fan_rpm=1300)
            rows.append(
                (
                    ts,
                    50.0,
                    1300,
                    -10.0,
                    80,
                    30.0,
                    10.0,
                    None,
                    _json.dumps(snap),
                )
            )
        # Bulk insert directly (bypassing insert_raw's one-commit-per-row,
        # which would make SETTING UP an 86,400-row benchmark itself slow
        # for reasons unrelated to what this test measures).
        st._conn.executemany(
            "INSERT INTO raw (ts, cpu_package_c, fan_rpm, battery_power_w, battery_pct, "
            "battery_temp_c, cpu_usage_pct, fan_target_rpm, snapshot) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        st._conn.commit()
        self.assertEqual(st.raw_row_count(), n)

        start = _time.perf_counter()
        points = st.raw_points_for_events()
        elapsed = _time.perf_counter() - start

        self.assertEqual(len(points), n)
        self.assertLess(
            elapsed, 1.0, msg=f"raw_points_for_events took {elapsed:.3f}s for {n} rows (must stay < 1s)"
        )
        # Correctness, not just speed: status/pct come through correctly.
        self.assertEqual(points[0], (0.0, 80, "Discharging"))


class FancurveRawTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-store-events-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.db_path = self._tmp / "hwmon.db"
        self.st = store.Store(self.db_path)
        self.addCleanup(self.st.close)

    def test_returns_rows_since_cutoff(self) -> None:
        self.st.insert_raw(_snap(1000.0, package_c=60.0, fan_rpm=1300, target_rpm=1400))
        self.st.insert_raw(_snap(2000.0, package_c=70.0, fan_rpm=2000, target_rpm=2100))
        rows = self.st.fancurve_raw(since=1500.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 70.0)

    def test_rows_include_snapshot_json_for_control_derivation(self) -> None:
        self.st.insert_raw(_snap(1000.0))
        rows = self.st.fancurve_raw(since=0.0)
        import json

        snap = json.loads(rows[0][3])
        self.assertIn("fan", snap)


if __name__ == "__main__":
    unittest.main()
