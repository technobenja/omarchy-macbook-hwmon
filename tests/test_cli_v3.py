"""CLI coverage for the v3 commands: `hwmon fancurve` (A16) and
`hwmon events` (A17). `hwmon events` calls the real `events.list_boots` /
`events.query_boot_journal` by default (there's no CLI flag to inject a
fake, matching every other subprocess call in this codebase) -- these
tests patch those two functions so the suite never shells out to a real
`journalctl`.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hwmon import __main__ as cli
from hwmon import events, store

# Both `hwmon fancurve` (24h lookback) and `hwmon events` (30-day lookback)
# use the real wall clock internally (no injectable "now", unlike
# Store.aggregate_and_prune) -- so every timestamp in this file is anchored
# to the real `time.time()` at import/setup time, never an arbitrary small
# float like 1000.0 (which would fall outside both lookback windows).
NOW = time.time()


def _snap(
    ts: float,
    *,
    package_c: float,
    fan_rpm: int,
    target_rpm: int | None = None,
    manual: bool | None = False,
    control: str | None = None,
    pct: float = 80,
    status: str = "Discharging",
) -> dict:
    fan: dict = {"rpm": fan_rpm, "target_rpm": target_rpm, "manual": manual}
    if control is not None:
        fan["control"] = control
    return {
        "ts": ts,
        "cpu": {"package_c": package_c, "usage_pct": 10.0},
        "fan": fan,
        "battery": {"power_w": -10.0, "pct": pct, "temp_c": 30.0, "status": status},
    }


class CliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-cli-v3-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        full_argv = ["--state-dir", str(self.state_dir), "--db", str(self.db_path), *argv]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(full_argv)
        return code, out.getvalue(), err.getvalue()


class FancurveCliTests(CliBase):
    def test_empty_db_reports_no_rows(self) -> None:
        code, out, err = self._run(["fancurve"])
        self.assertEqual(code, 0)
        self.assertIn("no raw rows", out)

    def test_hours_over_24_is_rejected(self) -> None:
        code, out, err = self._run(["fancurve", "--hours", "25"])
        self.assertNotEqual(code, 0)
        self.assertIn("<= 24", err)

    def test_groups_by_control_and_bin(self) -> None:
        with store.Store(self.db_path) as st:
            for i in range(5):
                st.insert_raw(_snap(NOW - 600 + i, package_c=62.0, fan_rpm=1300, manual=False))
            for i in range(5):
                st.insert_raw(
                    _snap(NOW - 300 + i, package_c=72.0, fan_rpm=3000, target_rpm=3200, manual=True, control="mbpfan")
                )
        code, out, err = self._run(["fancurve", "--hours", "24"])
        self.assertEqual(code, 0)
        self.assertIn("smc", out)
        self.assertIn("mbpfan", out)
        self.assertIn("60", out)  # 62.0 bucketed to 60 (bin width 5)
        self.assertIn("70", out)

    def test_legacy_schema1_rows_derive_control_from_manual(self) -> None:
        with store.Store(self.db_path) as st:
            st.insert_raw(_snap(NOW - 100, package_c=65.0, fan_rpm=1300, manual=False))  # no "control" key
            st.insert_raw(_snap(NOW - 99, package_c=65.0, fan_rpm=4000, manual=True))  # no "control" key
        code, out, err = self._run(["fancurve"])
        self.assertEqual(code, 0)
        self.assertIn("smc", out)
        self.assertIn("mbpfan", out)


class EventsCliTests(CliBase):
    def test_empty_db_reports_no_events(self) -> None:
        with mock.patch("hwmon.events.list_boots", return_value=[]):
            code, out, err = self._run(["events"])
        self.assertEqual(code, 0)
        self.assertIn("no power-loss events", out)

    def test_recorded_event_is_listed(self) -> None:
        record = events.EventRecord(
            ts_start=NOW - 3600, ts_end=NOW - 3500, kind="hard_poweroff", last_pct=2,
            last_status="Discharging", detail="Dirty bit is set.", boot_id="boot-x",
        )
        with store.Store(self.db_path) as st:
            st.insert_event(record)
        with mock.patch("hwmon.events.list_boots", return_value=[]):
            code, out, err = self._run(["events"])
        self.assertEqual(code, 0)
        self.assertIn("hard_poweroff", out)
        self.assertIn("Dirty bit is set.", out)

    def test_live_rescan_finds_unrecorded_gap_without_writing(self) -> None:
        # Raw rows with an internal gap, nothing yet in `events`.
        gap_start = NOW - 7200
        gap_end = NOW - 3600
        with store.Store(self.db_path) as st:
            st.insert_raw(_snap(gap_start, package_c=50.0, fan_rpm=1300, pct=2, status="Discharging"))
            st.insert_raw(_snap(gap_end, package_c=50.0, fan_rpm=1300, pct=100, status="Charging"))

        boots = [
            events.BootInfo(
                index=0,
                boot_id="boot-live",
                first_entry_us=int((gap_start + 60) * 1_000_000),
                last_entry_us=int(NOW * 1_000_000),
            )
        ]
        evidence = [{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set."}]
        with mock.patch("hwmon.events.list_boots", return_value=boots), mock.patch(
            "hwmon.events.query_boot_journal", return_value=evidence
        ):
            code, out, err = self._run(["events"])
        self.assertEqual(code, 0)
        self.assertIn("hard_poweroff", out)

        # "without writing" (A17) -- the events table must still be empty.
        with store.Store(self.db_path) as st:
            self.assertEqual(st.list_events(days=365, now=NOW), [])

    def test_journalctl_unavailable_shows_recorded_only_with_warning(self) -> None:
        record = events.EventRecord(
            ts_start=NOW - 3600, ts_end=NOW - 3500, kind="off_or_stopped", last_pct=None,
            last_status=None, detail=None, boot_id=None,
        )
        with store.Store(self.db_path) as st:
            st.insert_event(record)
        with mock.patch("hwmon.events.list_boots", return_value=None):
            code, out, err = self._run(["events"])
        self.assertEqual(code, 0)
        self.assertIn("off_or_stopped", out)
        self.assertIn("journalctl", err)

    def test_s5_dedupe_uses_all_recorded_events_not_just_the_days_window(self) -> None:
        # S5 (advisor): a gap already recorded OUTSIDE the requested
        # --days display window must still be excluded from the live
        # re-scan -- deduping against list_events(days) instead of
        # existing_event_ts_starts() would let the CLI re-find (and
        # needlessly re-query the journal for) an already-recorded event
        # just because a short --days made it invisible to `recorded`.
        gap_start = NOW - 7200  # 2h ago
        gap_end = NOW - 7100
        with store.Store(self.db_path) as st:
            st.insert_raw(_snap(gap_start, package_c=50.0, fan_rpm=1300, pct=2, status="Discharging"))
            st.insert_raw(_snap(gap_end, package_c=50.0, fan_rpm=1300, pct=100, status="Charging"))
            # Already recorded, matching this exact gap's ts_start.
            st.insert_event(
                events.EventRecord(
                    ts_start=gap_start, ts_end=gap_end, kind="hard_poweroff", last_pct=2,
                    last_status="Discharging", detail="Dirty bit is set.", boot_id="boot-old",
                )
            )

        boots = [
            events.BootInfo(
                index=0,
                boot_id="boot-old",
                first_entry_us=int((gap_start + 60) * 1_000_000),
                last_entry_us=int(NOW * 1_000_000),
            )
        ]
        journal_calls = []

        def fake_journal(boot_id: str):
            journal_calls.append(boot_id)
            return [{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set."}]

        # A window far too short to include a 2h-old event by display rules.
        with mock.patch("hwmon.events.list_boots", return_value=boots), mock.patch(
            "hwmon.events.query_boot_journal", side_effect=fake_journal
        ):
            code, out, err = self._run(["events", "--days", "0.01"])

        self.assertEqual(code, 0)
        self.assertNotIn("hard_poweroff", out, msg=f"already-recorded event resurfaced outside its display window: {out!r}")
        self.assertEqual(journal_calls, [], msg="must not re-query the journal for an already-recorded gap")


if __name__ == "__main__":
    unittest.main()
