"""R-L4.1 -- `/home` snapper snapshot age, the schema-3 `recovery` key
(deliverables SPEC.md). Three states, each with its own positive control:
`not_configured` (no config file), `unknown` (config exists but unreadable/
unparseable), and a real `fresh`/`stale` reading.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hwmon import recovery

NOW = 2_000_000.0


class ParseNewestSnapshotTsTests(unittest.TestCase):
    def test_single_row(self) -> None:
        csv_text = "number,date\n1,2026-09-23 12:00:00\n"
        ts = recovery.parse_newest_snapshot_ts(csv_text)
        self.assertIsNotNone(ts)

    def test_picks_the_newest_of_several_rows(self) -> None:
        csv_text = "number,date\n1,2026-09-23 08:00:00\n2,2026-09-23 14:00:00\n3,2026-09-23 10:00:00\n"
        newest = recovery.parse_newest_snapshot_ts(csv_text)
        earliest_only = recovery.parse_newest_snapshot_ts("number,date\n1,2026-09-23 08:00:00\n")
        self.assertGreater(newest, earliest_only)

    def test_no_data_rows_is_none(self) -> None:
        self.assertIsNone(recovery.parse_newest_snapshot_ts("number,date\n"))

    def test_empty_text_is_none(self) -> None:
        self.assertIsNone(recovery.parse_newest_snapshot_ts(""))

    def test_missing_date_column_is_none(self) -> None:
        self.assertIsNone(recovery.parse_newest_snapshot_ts("number,description\n1,hello\n"))

    def test_garbage_date_value_is_skipped_not_fatal(self) -> None:
        csv_text = "number,date\n1,not-a-date\n2,2026-09-23 12:00:00\n"
        self.assertIsNotNone(recovery.parse_newest_snapshot_ts(csv_text))

    def test_all_garbage_dates_is_none(self) -> None:
        self.assertIsNone(recovery.parse_newest_snapshot_ts("number,date\n1,not-a-date\n"))

    def test_column_order_does_not_matter(self) -> None:
        # `--columns number,date` is requested explicitly, but the parser
        # itself must not assume a fixed column index.
        csv_text = "date,number\n2026-09-23 12:00:00,1\n"
        self.assertIsNotNone(recovery.parse_newest_snapshot_ts(csv_text))


class ComputeRecoveryStateTests(unittest.TestCase):
    def test_no_config_file_is_not_configured_never_stale(self) -> None:
        # Task requirement: "before the home config exists this MUST be
        # state not_configured, not stale/red".
        result = recovery.compute_recovery_state(home_config_present=False, csv_text=None, now=NOW)
        self.assertEqual(result, {"home_snapshot_state": "not_configured", "home_snapshot_age_s": None})

    def test_config_present_but_command_failed_is_unknown(self) -> None:
        result = recovery.compute_recovery_state(home_config_present=True, csv_text=None, now=NOW)
        self.assertEqual(result["home_snapshot_state"], "unknown")
        self.assertIsNone(result["home_snapshot_age_s"])

    def test_config_present_but_unparseable_csv_is_unknown(self) -> None:
        result = recovery.compute_recovery_state(home_config_present=True, csv_text="garbage", now=NOW)
        self.assertEqual(result["home_snapshot_state"], "unknown")

    def test_fresh_within_two_hours(self) -> None:
        newest = NOW - 3600  # 1h old
        import datetime

        date_str = datetime.datetime.fromtimestamp(newest, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        csv_text = f"number,date\n1,{date_str}\n"
        result = recovery.compute_recovery_state(home_config_present=True, csv_text=csv_text, now=NOW)
        self.assertEqual(result["home_snapshot_state"], "fresh")
        self.assertAlmostEqual(result["home_snapshot_age_s"], 3600.0, delta=1.0)

    def test_stale_past_two_hours(self) -> None:
        # Positive control for the fresh/stale boundary.
        newest = NOW - 3 * 3600  # 3h old
        import datetime

        date_str = datetime.datetime.fromtimestamp(newest, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        csv_text = f"number,date\n1,{date_str}\n"
        result = recovery.compute_recovery_state(home_config_present=True, csv_text=csv_text, now=NOW)
        self.assertEqual(result["home_snapshot_state"], "stale")

    def test_exactly_two_hours_is_fresh_not_stale(self) -> None:
        import datetime

        newest = NOW - recovery.STALE_AFTER_S
        date_str = datetime.datetime.fromtimestamp(newest, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        csv_text = f"number,date\n1,{date_str}\n"
        result = recovery.compute_recovery_state(home_config_present=True, csv_text=csv_text, now=NOW)
        self.assertEqual(result["home_snapshot_state"], "fresh")


class HomeConfigExistsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-recovery-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_missing_file_is_false(self) -> None:
        self.assertFalse(recovery.home_config_exists(self._tmp / "does-not-exist"))

    def test_present_file_is_true(self) -> None:
        path = self._tmp / "home"
        path.write_text("ALLOW_USERS=someuser\n")
        self.assertTrue(recovery.home_config_exists(path))


class ListHomeSnapshotsTests(unittest.TestCase):
    def test_success_returns_stdout(self) -> None:
        with mock.patch("hwmon.procutil.run", return_value="number,date\n1,2026-09-23 12:00:00\n") as mock_run:
            out = recovery.list_home_snapshots()
        self.assertIn("number,date", out)
        # Requested columns/flags, per the module docstring.
        cmd = mock_run.call_args[0][0]
        self.assertIn("--utc", cmd)
        self.assertIn("--csvout", cmd)
        self.assertIn("number,date", cmd)

    def test_no_permissions_failure_yields_none(self) -> None:
        # Measured live 2026-09-23: "No permissions." on stderr, non-zero exit.
        with mock.patch("hwmon.procutil.run", return_value=None):
            self.assertIsNone(recovery.list_home_snapshots())


class RecoveryCacheTests(unittest.TestCase):
    def test_polls_at_most_once_per_ttl(self) -> None:
        calls = []

        class _Clock:
            def __init__(self):
                self.t = 1000.0

            def __call__(self):
                return self.t

        clock = _Clock()
        cache = recovery.RecoveryCache(
            ttl_s=60.0,
            home_config_path=Path("/nonexistent/home-config-for-test"),
            list_fn=lambda: calls.append(1) or None,
            clock=clock,
            now_fn=lambda: NOW,
        )
        for _ in range(60):
            cache.get()
            clock.t += 1.0
        self.assertEqual(len(calls), 0, "no config file present -> list_fn must never even be called")

    def test_polls_list_fn_at_most_once_per_ttl_when_config_present(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-recovery-cache-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        config_path = self._tmp / "home"
        config_path.write_text("ALLOW_USERS=someuser\n")

        calls = []

        class _Clock:
            def __init__(self):
                self.t = 1000.0

            def __call__(self):
                return self.t

        clock = _Clock()
        cache = recovery.RecoveryCache(
            ttl_s=60.0,
            home_config_path=config_path,
            list_fn=lambda: calls.append(1) or "number,date\n1,2026-09-23 12:00:00\n",
            clock=clock,
            now_fn=lambda: NOW,
        )
        for _ in range(60):
            cache.get()
            clock.t += 1.0
        self.assertEqual(len(calls), 1)

    def test_not_configured_is_the_null_default(self) -> None:
        cache = recovery.RecoveryCache(home_config_path=Path("/nonexistent/home-config-for-test"))
        self.assertEqual(cache.get(), {"home_snapshot_state": "not_configured", "home_snapshot_age_s": None})


if __name__ == "__main__":
    unittest.main()
