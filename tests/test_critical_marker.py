"""R-L1.4 -- the critical-battery journal marker + journalctl --sync,
fired once per boot (deliverables SPEC.md)."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hwmon import backstop, critical_marker, daemon, store


def _failing_hibernate_fn() -> bool:
    raise AssertionError("a test must never reach a real hibernate_fn")


class IsCriticalTests(unittest.TestCase):
    def test_discharging_at_or_below_threshold_is_critical(self) -> None:
        self.assertTrue(critical_marker.is_critical(5, "Discharging", critical_pct=5))
        self.assertTrue(critical_marker.is_critical(2, "Discharging", critical_pct=5))

    def test_discharging_above_threshold_is_not_critical(self) -> None:
        # Positive control for the boundary.
        self.assertFalse(critical_marker.is_critical(6, "Discharging", critical_pct=5))

    def test_not_discharging_is_never_critical_even_at_zero_pct(self) -> None:
        self.assertFalse(critical_marker.is_critical(0, "Charging", critical_pct=5))
        self.assertFalse(critical_marker.is_critical(0, "Full", critical_pct=5))

    def test_null_pct_is_never_critical(self) -> None:
        # feedback_absent_is_not_zero: a missing reading must not act like 0%.
        self.assertFalse(critical_marker.is_critical(None, "Discharging", critical_pct=5))

    def test_null_status_is_never_critical(self) -> None:
        self.assertFalse(critical_marker.is_critical(3, None, critical_pct=5))


class MarkerLineTests(unittest.TestCase):
    def test_verbatim_text_at_warning_priority(self) -> None:
        self.assertEqual(critical_marker.marker_line(4), "<4>hwmon: critical battery 4%")


class SyncJournalTests(unittest.TestCase):
    def test_success_returns_true(self) -> None:
        with mock.patch("hwmon.procutil.run", return_value=""):
            self.assertTrue(critical_marker.sync_journal())

    def test_failure_returns_false_not_an_exception(self) -> None:
        with mock.patch("hwmon.procutil.run", return_value=None):
            self.assertFalse(critical_marker.sync_journal())


def _valid_tree(tmp: Path) -> tuple[Path, Path]:
    from . import fakefs

    sysfs = tmp / "sys"
    procfs = tmp / "proc"
    fakefs.add_coretemp(sysfs, index=3)
    fakefs.add_applesmc(sysfs, index=2)
    fakefs.write_loadavg(procfs, 0.1, 0.1, 0.1)
    fakefs.write_stat(procfs, {"cpu": [1, 0, 1, 100, 0, 0, 0, 0]})
    fakefs.write_meminfo(procfs, {"MemTotal": 1000, "MemAvailable": 500, "SwapTotal": 0, "SwapFree": 0})
    fakefs.write_diskstats(procfs, "sda", sectors_read=0, sectors_written=0)
    fakefs.write_route(procfs, None)
    return sysfs, procfs


class DaemonWiringTests(unittest.TestCase):
    """End-to-end through `daemon.run` -- proves the DB dedupe, not just the
    pure `is_critical`/`marker_line` helpers."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-critical-marker-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs, self.procfs = _valid_tree(self._tmp)
        from . import fakefs

        fakefs.add_power_supply(
            self.sysfs, "BAT0",
            {"capacity": "4", "status": "Discharging", "voltage_now": "11000000", "current_now": "1000000"},
        )
        fakefs.add_power_supply(self.sysfs, "ADP1", {"online": "0"})
        fakefs.write_boot_id(self.procfs, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"
        # SHOULD 3 (review): pin XDG_CONFIG_HOME on every daemon.run test.
        env_patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self._tmp / "xdg-config")})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run(self, iterations: int, **kwargs) -> None:
        kwargs.setdefault("sync_journal_fn", lambda: True)
        kwargs.setdefault("config_loader", lambda: {"hibernate_backstop": False, "backstop_action_pct": None})
        kwargs.setdefault("hibernate_fn", _failing_hibernate_fn)
        # Never shell out to a real busctl for the §11 R3 standing check --
        # this file isn't testing that, and a real D-Bus call in a unit
        # test would be non-hermetic even where it happens to fail closed.
        kwargs.setdefault(
            "upower_cache",
            backstop.UPowerCache(read_fn=lambda: {"pct": None, "energy_full": None, "energy_full_design": None}),
        )
        # Item 8 (review): never let a real notify-send fire from a test.
        kwargs.setdefault("backstop_notify_fn", lambda *a, **k: None)
        kwargs.setdefault("triage_notify_fn", lambda *a, **k: None)
        daemon.run(
            state_dir=self.state_dir,
            db_path=self.db_path,
            sysfs_root=self.sysfs,
            procfs_root=self.procfs,
            interval=0.0,
            iterations=iterations,
            install_signal_handlers=False,
            check_events_at_start=False,
            **kwargs,
        )

    def test_marker_fires_once_across_many_ticks(self) -> None:
        sync_calls = []
        self._run(10, sync_journal_fn=lambda: sync_calls.append(1) or True)
        with store.Store(self.db_path) as st:
            rows = [r for r in st.list_events(days=1, now=time.time()) if r[2] == "critical_battery_marker"]
        self.assertEqual(len(rows), 1, msg=f"rows: {rows}")
        self.assertEqual(len(sync_calls), 1, "must sync exactly once, not once per tick")

    def test_marker_not_fired_when_not_critical(self) -> None:
        # Positive control: raise the threshold criteria the battery
        # satisfies (4% <= 5 by default) to something it can't reach.
        self._run(3, critical_pct=1)
        with store.Store(self.db_path) as st:
            rows = [r for r in st.list_events(days=1, now=time.time()) if r[2] == "critical_battery_marker"]
        self.assertEqual(rows, [])

    def test_restart_within_same_boot_does_not_refire(self) -> None:
        # A second daemon.run() (fresh in-process State) against the SAME db
        # and SAME boot id must see the DB record from the first run and
        # not fire (and not re-sync) again -- "once per boot", not merely
        # "once per process".
        self._run(1)
        sync_calls = []
        self._run(5, sync_journal_fn=lambda: sync_calls.append(1) or True)
        with store.Store(self.db_path) as st:
            rows = [r for r in st.list_events(days=1, now=time.time()) if r[2] == "critical_battery_marker"]
        self.assertEqual(len(rows), 1, "must not duplicate the marker across a restart in the same boot")
        self.assertEqual(sync_calls, [], "must not sync again once the DB already has this boot's marker")


if __name__ == "__main__":
    unittest.main()
