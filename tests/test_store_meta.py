"""Deliverables SPEC.md additions to store.py: the generic `meta` table
(the journal-failure streak, R-L3.1) and `has_event_kind_for_boot` (the
per-boot dedupe shared by R-L1.4's critical-battery marker and R-L1.5's
hibernate backstop)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hwmon import events, store


class MetaTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-store-meta-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.st = store.Store(self._tmp / "hwmon.db")
        self.addCleanup(self.st.close)

    def test_missing_key_returns_default(self) -> None:
        self.assertEqual(self.st.get_meta_int("nope", default=0), 0)
        self.assertEqual(self.st.get_meta_int("nope", default=7), 7)

    def test_set_then_get_round_trips(self) -> None:
        self.st.set_meta_int("streak", 2)
        self.assertEqual(self.st.get_meta_int("streak"), 2)

    def test_set_overwrites_not_duplicates(self) -> None:
        self.st.set_meta_int("streak", 1)
        self.st.set_meta_int("streak", 2)
        self.assertEqual(self.st.get_meta_int("streak"), 2)
        rows = self.st._conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'streak'").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_survives_reopen(self) -> None:
        self.st.set_meta_int("streak", 3)
        self.st.close()
        st2 = store.Store(self._tmp / "hwmon.db")
        self.addCleanup(st2.close)
        self.assertEqual(st2.get_meta_int("streak"), 3)


class HasEventKindForBootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-store-meta-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.st = store.Store(self._tmp / "hwmon.db")
        self.addCleanup(self.st.close)

    def _record(self, **overrides) -> events.EventRecord:
        base = dict(
            ts_start=1000.0, ts_end=None, kind="critical_battery_marker",
            last_pct=4, last_status="Discharging", detail=None, boot_id="boot-a",
        )
        base.update(overrides)
        return events.EventRecord(**base)

    def test_absent_is_false(self) -> None:
        self.assertFalse(self.st.has_event_kind_for_boot("critical_battery_marker", "boot-a"))

    def test_present_is_true(self) -> None:
        self.st.insert_event(self._record())
        self.assertTrue(self.st.has_event_kind_for_boot("critical_battery_marker", "boot-a"))

    def test_wrong_kind_same_boot_is_false(self) -> None:
        # Positive control: the kind filter is real, not a boot-only check.
        self.st.insert_event(self._record())
        self.assertFalse(self.st.has_event_kind_for_boot("backstop_hibernate", "boot-a"))

    def test_right_kind_different_boot_is_false(self) -> None:
        self.st.insert_event(self._record())
        self.assertFalse(self.st.has_event_kind_for_boot("critical_battery_marker", "boot-b"))


if __name__ == "__main__":
    unittest.main()
