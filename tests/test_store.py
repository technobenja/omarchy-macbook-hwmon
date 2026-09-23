from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hwmon import store


def _snap(ts: float, **overrides) -> dict:
    base = {
        "ts": ts,
        "cpu": {"package_c": 55.0, "usage_pct": 12.0},
        "fan": {"rpm": 1300},
        "battery": {"power_w": -5.0, "pct": 90, "temp_c": 31.0},
    }
    base.update(overrides)
    return base


class StoreBasicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-store-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.db_path = self._tmp / "hwmon.db"

    def test_creates_db_file_and_parent_dir(self) -> None:
        nested = self._tmp / "a" / "b" / "hwmon.db"
        st = store.Store(nested)
        self.addCleanup(st.close)
        self.assertTrue(nested.exists())

    def test_insert_raw_is_queryable(self) -> None:
        st = store.Store(self.db_path)
        self.addCleanup(st.close)
        st.insert_raw(_snap(1000.0))
        self.assertEqual(st.raw_row_count(), 1)

    def test_insert_raw_upserts_same_ts(self) -> None:
        st = store.Store(self.db_path)
        self.addCleanup(st.close)
        st.insert_raw(_snap(1000.0, cpu={"package_c": 1.0, "usage_pct": 1.0}))
        st.insert_raw(_snap(1000.0, cpu={"package_c": 2.0, "usage_pct": 2.0}))
        self.assertEqual(st.raw_row_count(), 1)

    def test_reopening_existing_db_does_not_lose_data(self) -> None:
        st1 = store.Store(self.db_path)
        st1.insert_raw(_snap(1000.0))
        st1.close()

        st2 = store.Store(self.db_path)
        self.addCleanup(st2.close)
        self.assertEqual(st2.raw_row_count(), 1)

    def test_history_and_peaks_on_empty_db(self) -> None:
        st = store.Store(self.db_path)
        self.addCleanup(st.close)
        self.assertEqual(st.history("cpu_package_c", 60, now=1000.0), [])
        peaks = st.peaks(now=1000.0)
        self.assertEqual(peaks["cpu_package_c"], {"last_24h": None, "all_time": None})

    def test_history_unknown_metric_raises(self) -> None:
        st = store.Store(self.db_path)
        self.addCleanup(st.close)
        with self.assertRaises(ValueError):
            st.history("not_a_real_metric", 60, now=1000.0)

    def test_context_manager_closes(self) -> None:
        with store.Store(self.db_path) as st:
            st.insert_raw(_snap(1000.0))
        # Connection is closed; using it again should raise.
        with self.assertRaises(Exception):
            st._conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
