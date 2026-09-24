"""R-L3.1 -- post-crash triage, amended by A10c (quote journal lines, don't
parse them). The real `tests/fixtures/poweroff_2026-09-23/` fixture drives
the "info" scenario; a synthetic pacman.log drives the "action" scenario.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from hwmon import backstop, daemon, events, store, triage

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "poweroff_2026-09-23"


# --- pacman.log parsing ---------------------------------------------------------------


class ParsePacmanLogLineTests(unittest.TestCase):
    def test_parses_a_real_line(self) -> None:
        line = triage.parse_pacman_log_line("[2026-09-23T09:15:03-0700] [ALPM] transaction started")
        self.assertIsNotNone(line)
        self.assertEqual(line.message, "transaction started")

    def test_offset_is_converted_to_utc_epoch(self) -> None:
        # feedback_two_clocks: prove the OFFSET is actually applied, not
        # just accepted syntactically -- -0700 and +0000 for the same
        # clock-face time must differ by exactly 7 hours.
        local = triage.parse_pacman_log_line("[2026-09-23T09:15:03-0700] [ALPM] x")
        utc = triage.parse_pacman_log_line("[2026-09-23T09:15:03+0000] [ALPM] x")
        self.assertAlmostEqual(utc.ts - local.ts, -7 * 3600, delta=1.0)

    def test_non_alpm_line_is_none(self) -> None:
        self.assertIsNone(triage.parse_pacman_log_line("[2026-09-23T09:15:03-0700] some other log format"))

    def test_garbage_line_is_none_not_an_exception(self) -> None:
        self.assertIsNone(triage.parse_pacman_log_line("not a pacman log line at all"))

    def test_unparseable_timestamp_is_none(self) -> None:
        self.assertIsNone(triage.parse_pacman_log_line("[not-a-date] [ALPM] transaction started"))

    def test_parse_pacman_log_skips_bad_lines(self) -> None:
        text = (
            "[2026-09-23T09:15:00-0700] [ALPM] transaction started\n"
            "garbage line\n"
            "[2026-09-23T09:15:05-0700] [ALPM] transaction completed\n"
        )
        lines = triage.parse_pacman_log(text)
        self.assertEqual(len(lines), 2)


class FindInterruptedTransactionTests(unittest.TestCase):
    def test_started_without_completed_is_interrupted(self) -> None:
        lines = triage.parse_pacman_log("[2026-09-23T09:15:00-0700] [ALPM] transaction started\n")
        result = triage.find_interrupted_transaction(lines, window_start=0.0, window_end=1e12)
        self.assertIsNotNone(result)

    def test_started_then_completed_is_not_interrupted(self) -> None:
        # Positive control: the completion line clears the finding.
        lines = triage.parse_pacman_log(
            "[2026-09-23T09:15:00-0700] [ALPM] transaction started\n"
            "[2026-09-23T09:15:05-0700] [ALPM] transaction completed\n"
        )
        result = triage.find_interrupted_transaction(lines, window_start=0.0, window_end=1e12)
        self.assertIsNone(result)

    def test_started_line_outside_window_is_ignored(self) -> None:
        lines = triage.parse_pacman_log("[2026-01-01T00:00:00-0700] [ALPM] transaction started\n")
        result = triage.find_interrupted_transaction(lines, window_start=1_800_000_000.0, window_end=1_800_100_000.0)
        self.assertIsNone(result)

    def test_completion_after_the_window_still_clears_it(self) -> None:
        # A transaction that started just before the crash but finished
        # (barely) after the machine came back must not be "interrupted".
        lines = triage.parse_pacman_log(
            "[2026-09-23T09:15:00-0700] [ALPM] transaction started\n"
            "[2026-09-23T15:20:00-0700] [ALPM] transaction completed\n"
        )
        window_end = triage.parse_pacman_log_line("[2026-09-23T09:16:00-0700] [ALPM] transaction started").ts
        result = triage.find_interrupted_transaction(lines, window_start=0.0, window_end=window_end)
        self.assertIsNone(result)

    def test_picks_the_last_started_line_in_window(self) -> None:
        lines = triage.parse_pacman_log(
            "[2026-09-23T09:00:00-0700] [ALPM] transaction started\n"
            "[2026-09-23T09:00:05-0700] [ALPM] transaction completed\n"
            "[2026-09-23T09:10:00-0700] [ALPM] transaction started\n"
        )
        result = triage.find_interrupted_transaction(lines, window_start=0.0, window_end=1e12)
        self.assertIn("started", result.message)
        self.assertAlmostEqual(
            result.ts, triage.parse_pacman_log_line("[2026-09-23T09:10:00-0700] [ALPM] transaction started").ts
        )


# --- stale lock -------------------------------------------------------------------------


class IsLockStaleTests(unittest.TestCase):
    def test_lock_present_no_process_is_stale(self) -> None:
        self.assertTrue(triage.is_lock_stale(lock_exists=True, pacman_running=False))

    def test_lock_present_process_running_is_not_stale(self) -> None:
        # Positive control: pacman genuinely holding its own lock.
        self.assertFalse(triage.is_lock_stale(lock_exists=True, pacman_running=True))

    def test_no_lock_is_not_stale(self) -> None:
        self.assertFalse(triage.is_lock_stale(lock_exists=False, pacman_running=False))

    def test_unknown_process_state_is_could_not_check(self) -> None:
        self.assertIsNone(triage.is_lock_stale(lock_exists=True, pacman_running=None))


class PacmanProcessRunningTests(unittest.TestCase):
    def test_uses_pgrep_dash_x_never_dash_f_or_dash_a(self) -> None:
        # feedback_credential_in_argv's sibling trap.
        from unittest import mock

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1})()
            triage.pacman_process_running()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["pgrep", "-x", "pacman"])
        self.assertNotIn("-f", cmd)
        self.assertNotIn("-a", cmd)

    def test_exit_0_is_running(self) -> None:
        from unittest import mock

        with mock.patch("subprocess.run", return_value=type("R", (), {"returncode": 0})()):
            self.assertTrue(triage.pacman_process_running())

    def test_exit_1_is_not_running(self) -> None:
        from unittest import mock

        with mock.patch("subprocess.run", return_value=type("R", (), {"returncode": 1})()):
            self.assertFalse(triage.pacman_process_running())

    def test_other_exit_code_is_unknown(self) -> None:
        from unittest import mock

        with mock.patch("subprocess.run", return_value=type("R", (), {"returncode": 2})()):
            self.assertIsNone(triage.pacman_process_running())

    def test_timeout_is_unknown_not_an_exception(self) -> None:
        import subprocess
        from unittest import mock

        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=2.0)):
            self.assertIsNone(triage.pacman_process_running())


# --- journal queries --------------------------------------------------------------------


class QueryBtrfsWarningLinesTests(unittest.TestCase):
    def test_filters_to_btrfs_only(self) -> None:
        from unittest import mock

        raw = (
            '{"SYSLOG_IDENTIFIER": "kernel", "MESSAGE": "BTRFS warning (device dm-0): something"}\n'
            '{"SYSLOG_IDENTIFIER": "kernel", "MESSAGE": "unrelated kernel line"}\n'
        )
        with mock.patch("hwmon.procutil.run", return_value=raw) as mock_run:
            lines = triage.query_btrfs_warning_lines("boot-1")
        self.assertEqual(len(lines), 1)
        self.assertIn("BTRFS", lines[0]["MESSAGE"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("_BOOT_ID=boot-1", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("warning", cmd)

    def test_journal_failure_yields_none(self) -> None:
        from unittest import mock

        with mock.patch("hwmon.procutil.run", return_value=None):
            self.assertIsNone(triage.query_btrfs_warning_lines("boot-1"))


class QueryEspFsckLinesTests(unittest.TestCase):
    def test_lines_are_quoted_verbatim_not_pattern_matched(self) -> None:
        # A10c: this reader must return EVERY systemd-fsck line for the
        # boot, not just ones matching a specific phrase (unlike events.py).
        from unittest import mock

        raw = '{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "some other fsck message entirely"}\n'
        with mock.patch("hwmon.procutil.run", return_value=raw):
            lines = triage.query_esp_fsck_lines("boot-1")
        self.assertEqual(lines[0]["MESSAGE"], "some other fsck message entirely")


# --- snapshot pointer -----------------------------------------------------------------


class SnapshotPointerHintTests(unittest.TestCase):
    def test_default_is_generic_no_number_claimed(self) -> None:
        hint = triage.snapshot_pointer_hint()
        self.assertEqual(hint, triage.SNAPSHOT_HINT_GENERIC)
        self.assertNotIn("#", hint)

    def test_readable_snapshots_name_a_number(self) -> None:
        # Positive control: proves the "readable" branch works even though
        # it is inert live (no root in this session).
        hint = triage.snapshot_pointer_hint(lambda: [{"number": 12}, {"number": 15}])
        self.assertIn("#15", hint)

    def test_empty_list_is_generic(self) -> None:
        self.assertEqual(triage.snapshot_pointer_hint(lambda: []), triage.SNAPSHOT_HINT_GENERIC)


# --- find_boot_containing ---------------------------------------------------------------


class FindBootContainingTests(unittest.TestCase):
    def test_finds_the_boot_active_at_ts(self) -> None:
        boots = [
            events.BootInfo(index=-1, boot_id="a", first_entry_us=int(1000 * 1e6), last_entry_us=int(1999 * 1e6)),
            events.BootInfo(index=0, boot_id="b", first_entry_us=int(2000 * 1e6), last_entry_us=int(2999 * 1e6)),
        ]
        found = triage.find_boot_containing(1500.0, boots)
        self.assertEqual(found.boot_id, "a")

    def test_ts_before_any_known_boot_is_none(self) -> None:
        boots = [events.BootInfo(index=0, boot_id="b", first_entry_us=int(2000 * 1e6), last_entry_us=int(2999 * 1e6))]
        self.assertIsNone(triage.find_boot_containing(500.0, boots))


# --- build_report / severity ------------------------------------------------------------


class BuildReportSeverityTests(unittest.TestCase):
    def _base_kwargs(self, **overrides) -> dict:
        base = dict(
            pacman_interrupted=None,
            pacman_log_ok=True,
            pacman_lock_stale=False,
            esp_fsck_lines=[{"MESSAGE": "Dirty bit is set."}],
            btrfs_warning_lines=[],
            snapshot_hint="",
        )
        base.update(overrides)
        return base

    def test_clean_run_is_info(self) -> None:
        report = triage.build_report(**self._base_kwargs())
        self.assertEqual(report.severity, triage.SEVERITY_INFO)

    def test_interrupted_transaction_is_action(self) -> None:
        interrupted = triage.PacmanLogLine(ts=1.0, message="transaction started")
        report = triage.build_report(**self._base_kwargs(pacman_interrupted=interrupted))
        self.assertEqual(report.severity, triage.SEVERITY_ACTION)
        self.assertEqual(report.pacman_interrupted, "transaction started")

    def test_stale_lock_is_action(self) -> None:
        report = triage.build_report(**self._base_kwargs(pacman_lock_stale=True))
        self.assertEqual(report.severity, triage.SEVERITY_ACTION)

    def test_pacman_log_unreadable_is_unknown_not_clean(self) -> None:
        # feedback_absent_is_not_zero: could-not-answer, never silently "info".
        report = triage.build_report(**self._base_kwargs(pacman_log_ok=False))
        self.assertEqual(report.severity, triage.SEVERITY_UNKNOWN)

    def test_journal_unreadable_is_unknown(self) -> None:
        report = triage.build_report(**self._base_kwargs(esp_fsck_lines=None))
        self.assertEqual(report.severity, triage.SEVERITY_UNKNOWN)
        self.assertIsNone(report.esp_fsck_lines)

    def test_lock_check_itself_failed_is_unknown(self) -> None:
        report = triage.build_report(**self._base_kwargs(pacman_lock_stale=None))
        self.assertEqual(report.severity, triage.SEVERITY_UNKNOWN)

    def test_snapshot_hint_only_populated_when_interrupted(self) -> None:
        report = triage.build_report(**self._base_kwargs())
        self.assertEqual(report.snapshot_hint, "")
        interrupted = triage.PacmanLogLine(ts=1.0, message="transaction started")
        report2 = triage.build_report(**self._base_kwargs(pacman_interrupted=interrupted, snapshot_hint="X"))
        self.assertEqual(report2.snapshot_hint, "X")

    def test_as_dict_is_json_serializable(self) -> None:
        report = triage.build_report(**self._base_kwargs())
        json.dumps(report.as_dict())  # must not raise


# --- run_triage: real fixture (info) + synthetic pacman.log (action) -----------------


def _load_real_fixture() -> tuple[list[events.RawPoint], list[events.BootInfo]]:
    raw_rows = json.loads((FIXTURE_DIR / "raw_rows.json").read_text())
    points = [events.RawPoint(ts=r["ts"], pct=r["battery_pct"], status=r["status"]) for r in raw_rows]
    boot_rows = json.loads((FIXTURE_DIR / "boots.json").read_text())
    boots = [
        events.BootInfo(index=r["index"], boot_id=r["boot_id"], first_entry_us=r["first_entry"], last_entry_us=r["last_entry"])
        for r in boot_rows
    ]
    return points, boots


class RealFixtureInfoScenarioTests(unittest.TestCase):
    """"today's incident, replayed from the fixture" -- ESP dirty bit
    cleared (info), btrfs tree-log replay (info), no interrupted
    transaction, no stale lock -> severity info."""

    def setUp(self) -> None:
        self.points, self.boots = _load_real_fixture()
        self.boot0_id = "77091d3f90094084b893ba51d05dab0c"
        evidence = json.loads((FIXTURE_DIR / "boot0_evidence.json").read_text())
        self.fsck_lines = [e for e in evidence if e["SYSLOG_IDENTIFIER"] == "systemd-fsck"]
        # A real capture has no BTRFS kernel line in this fixture -- an
        # empty (not None) result is still a successful, informational check.
        self.btrfs_lines: list[dict] = []

    def _record(self) -> events.EventRecord:
        boots_by_id = {b.boot_id: b for b in self.boots}
        boot0 = boots_by_id[self.boot0_id]
        last_before_gap = max(p.ts for p in self.points if p.ts < 1790190000)
        return events.EventRecord(
            ts_start=last_before_gap, ts_end=boot0.first_entry_s, kind="hard_poweroff",
            last_pct=2, last_status="Discharging", detail="Dirty bit is set.", boot_id=self.boot0_id,
        )

    def test_severity_is_info(self) -> None:
        report = triage.run_triage(
            self._record(), self.boots,
            pacman_log_reader=lambda: "",  # readable, empty log -> no interrupted transaction
            lock_exists_fn=lambda: False,
            pacman_running_fn=lambda: False,
            fsck_query_fn=lambda boot_id: self.fsck_lines,
            btrfs_query_fn=lambda boot_id: self.btrfs_lines,
        )
        self.assertEqual(report.severity, triage.SEVERITY_INFO, msg=f"report: {report.as_dict()}")
        self.assertIsNone(report.pacman_interrupted)
        self.assertFalse(report.pacman_lock_stale)
        self.assertTrue(any("Dirty bit is set" in line for line in report.esp_fsck_lines))

    def test_daemon_wiring_records_a_triage_event_for_this_fixture(self) -> None:
        # End-to-end: daemon._run_triage_for_event's dedupe/DB-write path,
        # not just the pure triage.run_triage function.
        import tempfile
        import shutil as _shutil

        tmp = Path(tempfile.mkdtemp(prefix="hwmon-triage-fixture-test-"))
        self.addCleanup(_shutil.rmtree, tmp, ignore_errors=True)
        db_path = tmp / "hwmon.db"
        record = self._record()
        with store.Store(db_path) as st:
            daemon._run_triage_for_event(
                st, record, self.boots,
                triage_runner=lambda r, b: triage.run_triage(
                    r, b,
                    pacman_log_reader=lambda: "",
                    lock_exists_fn=lambda: False,
                    pacman_running_fn=lambda: False,
                    fsck_query_fn=lambda boot_id: self.fsck_lines,
                    btrfs_query_fn=lambda boot_id: self.btrfs_lines,
                ),
                notify_fn=lambda summary, body: None,
            )
            rows = [r for r in st.list_events(days=365, now=1.8e9) if r[2] == "triage"]
        self.assertEqual(len(rows), 1)
        report_json = json.loads(rows[0][5])
        self.assertEqual(report_json["severity"], "info")


class SyntheticInterruptedUpgradeScenarioTests(unittest.TestCase):
    """"interrupted upgrade (positive control)" -- a synthetic pacman.log
    whose last block has no "transaction completed" -> severity action,
    naming a snapshot to boot."""

    def setUp(self) -> None:
        # Real, large epoch numbers throughout (not small synthetic ints):
        # the pacman.log line's own parsed timestamp (feedback_two_clocks --
        # its offset is parsed, not assumed) must land inside the crashed
        # boot's window for the "interrupted" finding to trigger at all.
        crash_ts = triage.parse_pacman_log_line("[2026-01-12T05:33:10+0000] [ALPM] transaction started").ts
        self.boots = [
            events.BootInfo(index=-1, boot_id="crashed-boot", first_entry_us=int((crash_ts - 100_000) * 1e6), last_entry_us=int((crash_ts - 10) * 1e6)),
            events.BootInfo(index=0, boot_id="recovery-boot", first_entry_us=int((crash_ts + 100) * 1e6), last_entry_us=int((crash_ts + 100_000) * 1e6)),
        ]
        self.record = events.EventRecord(
            ts_start=crash_ts + 10.0, ts_end=crash_ts + 100.0, kind="hard_poweroff",
            last_pct=2, last_status="Discharging", detail="Dirty bit is set.", boot_id="recovery-boot",
        )
        # A transaction that started inside the crashed boot's window and
        # never completed.
        self.pacman_log = "[2026-01-12T05:33:10+0000] [ALPM] transaction started\n"

    def test_severity_is_action_and_names_a_snapshot_hint(self) -> None:
        report = triage.run_triage(
            self.record, self.boots,
            pacman_log_reader=lambda: self.pacman_log,
            lock_exists_fn=lambda: False,
            pacman_running_fn=lambda: False,
            fsck_query_fn=lambda boot_id: [],
            btrfs_query_fn=lambda boot_id: [],
        )
        self.assertEqual(report.severity, triage.SEVERITY_ACTION, msg=f"report: {report.as_dict()}")
        self.assertIn("started", report.pacman_interrupted)
        self.assertEqual(report.snapshot_hint, triage.SNAPSHOT_HINT_GENERIC)

    def test_transaction_outside_the_crashed_boot_window_is_not_flagged(self) -> None:
        # Positive control: a transaction from a much earlier, unrelated
        # boot must not be reported as THIS crash's interrupted upgrade.
        old_log = "[2020-01-01T00:00:00+0000] [ALPM] transaction started\n"
        report = triage.run_triage(
            self.record, self.boots,
            pacman_log_reader=lambda: old_log,
            lock_exists_fn=lambda: False,
            pacman_running_fn=lambda: False,
            fsck_query_fn=lambda boot_id: [],
            btrfs_query_fn=lambda boot_id: [],
        )
        self.assertIsNone(report.pacman_interrupted)


def _failing_hibernate_fn() -> bool:
    raise AssertionError("a test must never reach a real hibernate_fn")


#: SHOULD 3 / item 8 (review): every daemon.run() call in this module gets
#: these, so it never shells out to a real busctl, never could reach a real
#: hibernate, and never fires a real notify-send.
_SAFE_BACKSTOP_KWARGS = dict(
    config_loader=lambda: {"hibernate_backstop": False, "backstop_action_pct": None},
    hibernate_fn=_failing_hibernate_fn,
    # §11 R3 runs regardless of hibernate_backstop -- never a real busctl here.
    upower_cache=backstop.UPowerCache(
        read_fn=lambda: {"pct": None, "energy_full": None, "energy_full_design": None}
    ),
    backstop_notify_fn=lambda *a, **k: None,
    triage_notify_fn=lambda *a, **k: None,
)


class JournalUnavailableScenarioTests(unittest.TestCase):
    """"journal unavailable" -- after 3 consecutive `list_boots` failures,
    record `journal_unavailable`; triage then says "could not check", never
    "clean" (three states, feedback_absent_is_not_zero)."""

    def setUp(self) -> None:
        import os

        env_patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/nonexistent-xdg-config-for-tests"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _valid_tree(self, tmp: Path) -> tuple[Path, Path]:
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
        fakefs.write_boot_id(procfs, "aaaaaaaa-0000-0000-0000-000000000000")
        return sysfs, procfs

    def test_three_consecutive_list_boots_failures_records_journal_unavailable(self) -> None:
        import tempfile
        import shutil as _shutil

        tmp = Path(tempfile.mkdtemp(prefix="hwmon-journal-unavailable-test-"))
        self.addCleanup(_shutil.rmtree, tmp, ignore_errors=True)
        sysfs, procfs = self._valid_tree(tmp)
        state_dir = tmp / "state"
        db_path = tmp / "hwmon.db"

        with store.Store(db_path) as st:
            st.insert_raw({"ts": 1000.0, "cpu": {"package_c": 50.0, "usage_pct": 1.0}, "fan": {"rpm": 1300}, "battery": {"power_w": -1.0, "pct": 50, "temp_c": 30.0, "status": "Discharging"}})

        run_kwargs = dict(
            state_dir=state_dir, db_path=db_path, sysfs_root=sysfs, procfs_root=procfs,
            interval=0.0, iterations=1, install_signal_handlers=False,
            list_boots_fn=lambda: None,  # simulated journalctl failure, every run
            sync_journal_fn=lambda: True,
            **_SAFE_BACKSTOP_KWARGS,
        )
        for _ in range(2):
            daemon.run(**run_kwargs)
        with store.Store(db_path) as st:
            self.assertEqual([r for r in st.list_events(days=1, now=1e9) if r[2] == "journal_unavailable"], [])

        daemon.run(**run_kwargs)  # 3rd consecutive failure
        with store.Store(db_path) as st:
            rows = [r for r in st.list_events(days=1, now=1e9) if r[2] == "journal_unavailable"]
        self.assertEqual(len(rows), 1, msg=f"rows: {rows}")

    def test_success_resets_the_streak(self) -> None:
        import tempfile
        import shutil as _shutil

        tmp = Path(tempfile.mkdtemp(prefix="hwmon-journal-unavailable-reset-test-"))
        self.addCleanup(_shutil.rmtree, tmp, ignore_errors=True)
        sysfs, procfs = self._valid_tree(tmp)
        state_dir = tmp / "state"
        db_path = tmp / "hwmon.db"

        # Two failures, then a success, then two more failures -- must NOT
        # add up to 3 without the reset (i.e. must not fire after only 2
        # more).
        for _ in range(2):
            daemon.run(
                state_dir=state_dir, db_path=db_path, sysfs_root=sysfs, procfs_root=procfs,
                interval=0.0, iterations=1, install_signal_handlers=False,
                list_boots_fn=lambda: None, sync_journal_fn=lambda: True,
                **_SAFE_BACKSTOP_KWARGS,
            )
        daemon.run(
            state_dir=state_dir, db_path=db_path, sysfs_root=sysfs, procfs_root=procfs,
            interval=0.0, iterations=1, install_signal_handlers=False,
            list_boots_fn=lambda: [], sync_journal_fn=lambda: True,  # success resets the streak
            **_SAFE_BACKSTOP_KWARGS,
        )
        for _ in range(2):
            daemon.run(
                state_dir=state_dir, db_path=db_path, sysfs_root=sysfs, procfs_root=procfs,
                interval=0.0, iterations=1, install_signal_handlers=False,
                list_boots_fn=lambda: None, sync_journal_fn=lambda: True,
                **_SAFE_BACKSTOP_KWARGS,
            )
        with store.Store(db_path) as st:
            rows = [r for r in st.list_events(days=1, now=1e9) if r[2] == "journal_unavailable"]
        self.assertEqual(rows, [], "the success in between must have reset the streak")


if __name__ == "__main__":
    unittest.main()
