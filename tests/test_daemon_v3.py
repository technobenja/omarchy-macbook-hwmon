"""daemon.run's v3 wiring: the InhibitorCache is polled at most every 10s
across many ticks (not once per tick), and the A17 events check runs
exactly once at daemon start using whatever `list_boots_fn` /
`journal_query_fn` fakes are injected -- never a real subprocess in this
test file.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from hwmon import daemon, events, inhibitors, store

from . import fakefs

NOW = time.time()


def _build_valid_tree(tmp: Path) -> tuple[Path, Path]:
    sysfs = tmp / "sys"
    procfs = tmp / "proc"
    fakefs.add_coretemp(sysfs, index=3)
    fakefs.add_applesmc(sysfs, index=2)
    fakefs.add_power_supply(
        sysfs,
        "BAT0",
        {
            "capacity": "81", "status": "Discharging", "voltage_now": "11247000",
            "current_now": "1390000", "temp": "345", "cycle_count": "3",
            "charge_now": "5435000", "charge_full": "6710000", "charge_full_design": "6400000",
        },
    )
    fakefs.add_power_supply(sysfs, "ADP1", {"online": "0"})
    for i in range(4):
        fakefs.add_cpu_freq(sysfs, i, 1900000)
    fakefs.write_loadavg(procfs, 0.82, 0.61, 0.55)
    fakefs.write_stat(
        procfs,
        {
            "cpu": [100, 0, 50, 800, 0, 0, 0, 0],
            "cpu0": [25, 0, 12, 200, 0, 0, 0, 0],
            "cpu1": [25, 0, 12, 200, 0, 0, 0, 0],
            "cpu2": [25, 0, 13, 200, 0, 0, 0, 0],
            "cpu3": [25, 0, 13, 200, 0, 0, 0, 0],
        },
    )
    fakefs.write_meminfo(
        procfs, {"MemTotal": 8_026_296, "MemAvailable": 3_126_692, "SwapTotal": 4_036_608, "SwapFree": 4_036_608}
    )
    fakefs.write_diskstats(procfs, "sda", sectors_read=1000, sectors_written=2000)
    fakefs.write_route(procfs, "wlp3s0")
    fakefs.write_netdev(procfs, "wlp3s0", rx_bytes=1000, tx_bytes=2000)
    return sysfs, procfs


class InhibitorCacheWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-daemon-v3-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs, self.procfs = _build_valid_tree(self._tmp)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"

    def test_power_guard_is_written_into_latest_json(self) -> None:
        rows = [["sleep", "omarchy-update", "Omarchy update in progress", "block", 0, 1]]
        cache = inhibitors.InhibitorCache(poll_fn=lambda: rows)
        daemon.run(
            state_dir=self.state_dir,
            db_path=self.db_path,
            sysfs_root=self.sysfs,
            procfs_root=self.procfs,
            interval=0.0,
            iterations=1,
            install_signal_handlers=False,
            inhibitor_cache=cache,
            list_boots_fn=lambda: [],
            journal_query_fn=lambda b: [],
        )
        snap = json.loads((self.state_dir / "latest.json").read_text())
        self.assertTrue(snap["power_guard"]["sleep_blocked"])
        self.assertEqual(snap["power_guard"]["blockers"][0]["who"], "omarchy-update")

    def test_busctl_fake_polled_at_most_once_per_ttl_across_many_ticks(self) -> None:
        poll_calls = []

        def fake_poll():
            poll_calls.append(1)
            return []

        cache = inhibitors.InhibitorCache(poll_fn=fake_poll, ttl_s=10.0)
        daemon.run(
            state_dir=self.state_dir,
            db_path=self.db_path,
            sysfs_root=self.sysfs,
            procfs_root=self.procfs,
            interval=0.0,
            iterations=25,  # simulates 25 "seconds" of ticks (interval=0 -> no real sleep)
            install_signal_handlers=False,
            inhibitor_cache=cache,
            list_boots_fn=lambda: [],
            journal_query_fn=lambda b: [],
        )
        # 25 ticks at interval=0.0 all happen within a fraction of a second
        # of wall-clock time, so with a 10s TTL the cache must poll ONCE.
        self.assertEqual(len(poll_calls), 1)


class EventsCheckAtStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-daemon-v3-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs, self.procfs = _build_valid_tree(self._tmp)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"

    def test_gap_found_at_start_is_recorded_once(self) -> None:
        # Seed a raw table with an old, unrecorded gap. `gap_end` is only a
        # few seconds before the real "now" the daemon-start check uses, so
        # the check finds exactly this ONE gap -- not a second, spurious
        # trailing gap between gap_end and the real current time.
        gap_start = NOW - 7200
        gap_end = NOW - 5
        with store.Store(self.db_path) as st:
            st.insert_raw({"ts": gap_start, "cpu": {"package_c": 50.0, "usage_pct": 1.0}, "fan": {"rpm": 1300}, "battery": {"power_w": -1.0, "pct": 2, "temp_c": 30.0, "status": "Discharging"}})
            st.insert_raw({"ts": gap_end, "cpu": {"package_c": 50.0, "usage_pct": 1.0}, "fan": {"rpm": 1300}, "battery": {"power_w": 0.0, "pct": 100, "temp_c": 30.0, "status": "Full"}})

        boots = [
            events.BootInfo(
                index=0,
                boot_id="boot-x",
                first_entry_us=int((gap_start + 60) * 1_000_000),
                last_entry_us=int(NOW * 1_000_000),
            )
        ]
        evidence = [{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set."}]
        journal_calls = []

        def fake_journal(boot_id: str):
            journal_calls.append(boot_id)
            return evidence

        daemon.run(
            state_dir=self.state_dir,
            db_path=self.db_path,
            sysfs_root=self.sysfs,
            procfs_root=self.procfs,
            interval=0.0,
            iterations=1,
            install_signal_handlers=False,
            inhibitor_cache=inhibitors.InhibitorCache(poll_fn=lambda: []),
            list_boots_fn=lambda: boots,
            journal_query_fn=fake_journal,
        )

        with store.Store(self.db_path) as st:
            rows = st.list_events(days=365, now=time.time())
        self.assertEqual(len(rows), 1, msg=f"rows: {rows}")
        self.assertEqual(rows[0][2], "hard_poweroff")
        # Journal queried once for the gap at start -- not once per tick.
        self.assertEqual(journal_calls, ["boot-x"])

    def test_s4_second_start_is_idempotent_events_count_stays_1_journal_not_recalled(self) -> None:
        # S4 (advisor): a second daemon start against the SAME db (e.g. a
        # `systemctl --user restart`) must not duplicate the already
        # recorded event, and -- because B2's dedupe now happens BEFORE
        # classification (see events.find_new_events) -- must not
        # re-query the journal for it either.
        gap_start = NOW - 7200
        gap_end = NOW - 5
        with store.Store(self.db_path) as st:
            st.insert_raw({"ts": gap_start, "cpu": {"package_c": 50.0, "usage_pct": 1.0}, "fan": {"rpm": 1300}, "battery": {"power_w": -1.0, "pct": 2, "temp_c": 30.0, "status": "Discharging"}})
            st.insert_raw({"ts": gap_end, "cpu": {"package_c": 50.0, "usage_pct": 1.0}, "fan": {"rpm": 1300}, "battery": {"power_w": 0.0, "pct": 100, "temp_c": 30.0, "status": "Full"}})

        boots = [
            events.BootInfo(
                index=0,
                boot_id="boot-y",
                first_entry_us=int((gap_start + 60) * 1_000_000),
                last_entry_us=int(NOW * 1_000_000),
            )
        ]
        evidence = [{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set."}]
        journal_calls: list[str] = []

        def fake_journal(boot_id: str):
            journal_calls.append(boot_id)
            return evidence

        run_kwargs = dict(
            state_dir=self.state_dir,
            db_path=self.db_path,
            sysfs_root=self.sysfs,
            procfs_root=self.procfs,
            interval=0.0,
            iterations=1,
            install_signal_handlers=False,
            inhibitor_cache=inhibitors.InhibitorCache(poll_fn=lambda: []),
            list_boots_fn=lambda: boots,
            journal_query_fn=fake_journal,
        )

        # First "daemon start" -- backfills and records the event.
        daemon.run(**run_kwargs)
        with store.Store(self.db_path) as st:
            rows_after_first = st.list_events(days=365, now=time.time())
        self.assertEqual(len(rows_after_first), 1)
        self.assertEqual(journal_calls, ["boot-y"])

        # Second "daemon start" -- e.g. a restart. The raw gap is still
        # there (raw retention is 24h; this gap is only 2h old), and the
        # boot list still contains "boot-y", so a naive re-scan would find
        # the SAME gap all over again.
        daemon.run(**run_kwargs)
        with store.Store(self.db_path) as st:
            rows_after_second = st.list_events(days=365, now=time.time())
        self.assertEqual(len(rows_after_second), 1, msg=f"event count must stay at 1, got {rows_after_second}")
        self.assertEqual(
            journal_calls, ["boot-y"], msg="journal must not be re-queried on the second start for an already-recorded gap"
        )

    def test_check_disabled_by_flag_does_not_touch_events_table(self) -> None:
        # S4 (advisor): the original form of this test used a `boom()`
        # fake that RAISED to prove it was never called -- but
        # `_check_power_loss_events` wraps `list_boots_fn()` in a
        # `try/except Exception` (so a real subprocess-wrapper failure
        # can't crash daemon startup), which silently SWALLOWS that raise.
        # This test could never go red even if the `check_events_at_start`
        # flag were ignored entirely. Recording calls in a list and
        # asserting it's empty has no such blind spot.
        with store.Store(self.db_path) as st:
            st.insert_raw({"ts": NOW - 100, "cpu": {"package_c": 50.0, "usage_pct": 1.0}, "fan": {"rpm": 1300}, "battery": {"power_w": -1.0, "pct": 2, "temp_c": 30.0, "status": "Discharging"}})

        calls: list[None] = []

        def record_call():
            calls.append(None)
            return []

        daemon.run(
            state_dir=self.state_dir,
            db_path=self.db_path,
            sysfs_root=self.sysfs,
            procfs_root=self.procfs,
            interval=0.0,
            iterations=1,
            install_signal_handlers=False,
            inhibitor_cache=inhibitors.InhibitorCache(poll_fn=lambda: []),
            list_boots_fn=record_call,
            check_events_at_start=False,
        )
        self.assertEqual(calls, [], msg="list_boots_fn must not be called when check_events_at_start=False")

    def test_list_boots_failure_does_not_crash_daemon_start(self) -> None:
        with store.Store(self.db_path) as st:
            st.insert_raw({"ts": 1000.0, "cpu": {"package_c": 50.0, "usage_pct": 1.0}, "fan": {"rpm": 1300}, "battery": {"power_w": -1.0, "pct": 2, "temp_c": 30.0, "status": "Discharging"}})

        n = daemon.run(
            state_dir=self.state_dir,
            db_path=self.db_path,
            sysfs_root=self.sysfs,
            procfs_root=self.procfs,
            interval=0.0,
            iterations=1,
            install_signal_handlers=False,
            inhibitor_cache=inhibitors.InhibitorCache(poll_fn=lambda: []),
            list_boots_fn=lambda: None,  # simulated journalctl failure
        )
        self.assertEqual(n, 1, "the collector loop must still run a tick")


if __name__ == "__main__":
    unittest.main()
