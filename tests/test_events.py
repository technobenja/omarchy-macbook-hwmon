"""A17/B1/B2: power-loss events classification.

The main positive control is the REAL captured data in
`tests/fixtures/poweroff_2026-09-23/` (raw rows, boot list, and boot-0
journal evidence, captured before 24h retention deleted the live gap):
classifying it must reproduce `hard_poweroff`, `last_pct 2`,
`ts_start` == the 09:37:02 PDT row's ts, and `detail` == the fsck "Dirty
bit" line -- exactly acceptance 13's positive control.

B1's "never consult the journal for an intra-boot gap" guarantee is proven
with a journal fake that RECORDS every call it receives, so a test can
assert it was never invoked -- not just that the result happened to come
out right.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from hwmon import events

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "poweroff_2026-09-23"


def _load_real_fixture() -> tuple[list[events.RawPoint], list[events.BootInfo], list[dict]]:
    raw_rows = json.loads((FIXTURE_DIR / "raw_rows.json").read_text())
    points = [
        events.RawPoint(ts=r["ts"], pct=r["battery_pct"], status=r["status"]) for r in raw_rows
    ]
    boot_rows = json.loads((FIXTURE_DIR / "boots.json").read_text())
    boots = [
        events.BootInfo(
            index=r["index"],
            boot_id=r["boot_id"],
            first_entry_us=r["first_entry"],
            last_entry_us=r["last_entry"],
        )
        for r in boot_rows
    ]
    evidence = json.loads((FIXTURE_DIR / "boot0_evidence.json").read_text())
    return points, boots, evidence


class _RecordingJournalQuery:
    """A fake `journal_query` that records every boot_id it was asked
    about, so a test can PROVE it was never called (not merely that the
    final answer happened to be right) -- required for the
    off_or_stopped-without-consulting-the-journal acceptance check."""

    def __init__(self, evidence_by_boot: dict[str, list[dict]]) -> None:
        self.evidence_by_boot = evidence_by_boot
        self.calls: list[str] = []

    def __call__(self, boot_id: str) -> list[dict] | None:
        self.calls.append(boot_id)
        return self.evidence_by_boot.get(boot_id, [])


class RealPoweroffFixtureTests(unittest.TestCase):
    """Acceptance 13's positive control: the real 2026-09-23 hard power-off."""

    def setUp(self) -> None:
        self.points, self.boots, self.evidence = _load_real_fixture()
        self.boot0_id = "77091d3f90094084b893ba51d05dab0c"
        self.journal = _RecordingJournalQuery({self.boot0_id: self.evidence})

    def test_reproduces_hard_poweroff_from_real_data(self) -> None:
        records = events.scan_for_events(self.points, self.boots, self.journal)
        self.assertEqual(len(records), 1, msg=f"expected exactly one event, got {records}")
        record = records[0]
        self.assertEqual(record.kind, "hard_poweroff")
        self.assertEqual(record.last_pct, 2)
        self.assertAlmostEqual(record.ts_start, 1790181422.8908136, places=3)
        self.assertIn("Dirty bit is set", record.detail)

    def test_ts_start_is_the_093702_pdt_rows_ts_exactly(self) -> None:
        # The last raw row before the gap, verbatim -- not the journal's
        # own (older, boot-lost-the-last-30s) timestamp (B1).
        last_before_gap = max(p.ts for p in self.points if p.ts < 1790190000)
        records = events.scan_for_events(self.points, self.boots, self.journal)
        self.assertEqual(records[0].ts_start, last_before_gap)

    def test_boot_id_is_boot_zero(self) -> None:
        records = events.scan_for_events(self.points, self.boots, self.journal)
        self.assertEqual(records[0].boot_id, self.boot0_id)

    def test_journal_queried_exactly_once_for_the_one_gap(self) -> None:
        events.scan_for_events(self.points, self.boots, self.journal)
        self.assertEqual(self.journal.calls, [self.boot0_id])

    def test_rescan_with_existing_ts_start_finds_nothing_new(self) -> None:
        # B2 dedupe: once this gap's ts_start is "already recorded", a
        # rescan must not re-query the journal for it.
        first = events.scan_for_events(self.points, self.boots, self.journal)
        existing = {r.ts_start for r in first}
        journal2 = _RecordingJournalQuery({self.boot0_id: self.evidence})
        new_records = events.find_new_events(existing, self.points, self.boots, journal2)
        self.assertEqual(new_records, [])
        self.assertEqual(journal2.calls, [], msg="must not re-query an already-recorded gap")


class IntraBootGapNeverConsultsJournalTests(unittest.TestCase):
    """"a fixture gap inside one boot (last raw ts > btime) -> off_or_stopped
    WITHOUT consulting the journal (prove the journal fake was not
    called)"."""

    def test_gap_after_boot_start_is_off_or_stopped_and_journal_not_called(self) -> None:
        boots = [
            events.BootInfo(index=0, boot_id="boot-current", first_entry_us=1_000_000_000_000, last_entry_us=1_000_100_000_000),
        ]
        # Both points are AFTER this boot's btime (1_000_000.0s) -- e.g. the
        # collector was `systemctl --user stop`ped and restarted within the
        # same boot.
        before = events.RawPoint(ts=1_000_050.0, pct=50, status="Discharging")
        after = events.RawPoint(ts=1_000_150.0, pct=48, status="Discharging")
        journal = _RecordingJournalQuery({})

        record = events.classify_gap(before, after, boots, journal)

        self.assertEqual(record.kind, "off_or_stopped")
        self.assertEqual(journal.calls, [], msg="journal must NOT be consulted for an intra-boot gap")
        self.assertIsNone(record.boot_id)
        self.assertIsNone(record.detail)

    def test_find_crossed_boot_returns_none_for_intra_boot_gap(self) -> None:
        boots = [events.BootInfo(index=0, boot_id="x", first_entry_us=1_000_000_000_000, last_entry_us=2_000_000_000_000)]
        self.assertIsNone(events.find_crossed_boot(ts_before=1_500_000.0, ts_after=1_600_000.0, boots=boots))

    def test_find_crossed_boot_finds_the_boot_that_started_during_the_gap(self) -> None:
        boots = [
            events.BootInfo(index=-1, boot_id="old", first_entry_us=900_000_000_000, last_entry_us=990_000_000_000),
            events.BootInfo(index=0, boot_id="new", first_entry_us=1_000_000_000_000, last_entry_us=1_100_000_000_000),
        ]
        crossed = events.find_crossed_boot(ts_before=995_000.0, ts_after=1_050_000.0, boots=boots)
        self.assertIsNotNone(crossed)
        self.assertEqual(crossed.boot_id, "new")

    def test_s1_rotated_out_intervening_boot_must_not_borrow_a_later_boots_evidence(self) -> None:
        # S1 (advisor): the boot that actually followed this gap has since
        # rotated out of `journalctl --list-boots` (retention). A LATER
        # boot still exists in the list -- the OLD (pre-S1) code, which
        # only checked `first_entry_s > ts_before` with no upper bound,
        # would incorrectly walk past the gap and pick that later boot,
        # borrowing ITS fsck/journald evidence for a crash it has nothing
        # to do with. Bounding by ts_after must yield None instead.
        gap_before = 1_000_000.0  # last sample before the crash
        gap_after = 1_000_060.0   # first sample once logging resumed (same boot the resumed row belongs to)
        # The boot that actually covers [gap_before, gap_after] is NOT in
        # this list (rotated out); only a boot from much later remains.
        boots = [
            events.BootInfo(
                index=0,
                boot_id="much-later-boot",
                first_entry_us=int(2_000_000.0 * 1_000_000),
                last_entry_us=int(2_100_000.0 * 1_000_000),
            ),
        ]
        crossed = events.find_crossed_boot(ts_before=gap_before, ts_after=gap_after, boots=boots)
        self.assertIsNone(crossed, "must not borrow a boot far past the gap window")

    def test_s1_classify_gap_with_rotated_out_boot_is_off_or_stopped_journal_not_called(self) -> None:
        gap_before = 1_000_000.0
        gap_after = 1_000_060.0
        boots = [
            events.BootInfo(
                index=0,
                boot_id="much-later-boot",
                first_entry_us=int(2_000_000.0 * 1_000_000),
                last_entry_us=int(2_100_000.0 * 1_000_000),
            ),
        ]
        before = events.RawPoint(ts=gap_before, pct=2, status="Discharging")
        after = events.RawPoint(ts=gap_after, pct=100, status="Charging")
        journal = _RecordingJournalQuery({"much-later-boot": [{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set."}]})

        record = events.classify_gap(before, after, boots, journal)

        self.assertEqual(record.kind, "off_or_stopped")
        self.assertEqual(journal.calls, [], msg="must not consult (or borrow evidence from) the later boot")


class CrossBootNoEvidenceTests(unittest.TestCase):
    """"a cross-boot gap with no evidence -> off_or_stopped"."""

    def test_boundary_crossing_gap_with_no_journal_evidence_is_off_or_stopped(self) -> None:
        boots = [events.BootInfo(index=0, boot_id="boot-b", first_entry_us=2_000_000_000_000, last_entry_us=2_100_000_000_000)]
        before = events.RawPoint(ts=1_999_000.0, pct=50, status="Discharging")
        after = events.RawPoint(ts=2_000_050.0, pct=100, status="Charging")
        journal = _RecordingJournalQuery({"boot-b": []})  # boot exists, no matching lines

        record = events.classify_gap(before, after, boots, journal)

        self.assertEqual(record.kind, "off_or_stopped")
        self.assertEqual(journal.calls, ["boot-b"], msg="the journal IS consulted for a boundary-crossing gap")
        self.assertEqual(record.boot_id, "boot-b")
        self.assertIsNone(record.detail)

    def test_journal_query_returning_none_is_also_no_evidence(self) -> None:
        boots = [events.BootInfo(index=0, boot_id="boot-c", first_entry_us=2_000_000_000_000, last_entry_us=2_100_000_000_000)]
        before = events.RawPoint(ts=1_999_000.0, pct=50, status="Discharging")
        after = events.RawPoint(ts=2_000_050.0, pct=100, status="Charging")

        def failing_query(boot_id: str) -> list[dict] | None:
            return None  # simulates a journalctl subprocess failure

        record = events.classify_gap(before, after, boots, failing_query)
        self.assertEqual(record.kind, "off_or_stopped")


class EvidenceWithoutLowBatteryTests(unittest.TestCase):
    """"evidence without low battery -> unclean_shutdown"."""

    def test_evidence_present_but_battery_was_fine_is_unclean_shutdown(self) -> None:
        boots = [events.BootInfo(index=0, boot_id="boot-d", first_entry_us=3_000_000_000_000, last_entry_us=3_100_000_000_000)]
        before = events.RawPoint(ts=2_999_000.0, pct=80, status="Discharging")
        after = events.RawPoint(ts=3_000_050.0, pct=79, status="Discharging")
        evidence_lines = [
            {"SYSLOG_IDENTIFIER": "systemd-journald", "MESSAGE": "File ... corrupted or uncleanly shut down, renaming and replacing."}
        ]
        journal = _RecordingJournalQuery({"boot-d": evidence_lines})

        record = events.classify_gap(before, after, boots, journal)

        self.assertEqual(record.kind, "unclean_shutdown")
        self.assertIsNotNone(record.detail)

    def test_evidence_with_low_battery_but_not_discharging_is_unclean_shutdown(self) -> None:
        # pct <= 5 alone isn't enough -- status must ALSO be Discharging.
        boots = [events.BootInfo(index=0, boot_id="boot-e", first_entry_us=3_000_000_000_000, last_entry_us=3_100_000_000_000)]
        before = events.RawPoint(ts=2_999_000.0, pct=3, status="Charging")
        after = events.RawPoint(ts=3_000_050.0, pct=10, status="Charging")
        evidence_lines = [{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set. Fs was not properly unmounted."}]
        journal = _RecordingJournalQuery({"boot-e": evidence_lines})

        record = events.classify_gap(before, after, boots, journal)

        self.assertEqual(record.kind, "unclean_shutdown")

    def test_evidence_with_low_battery_discharging_is_hard_poweroff(self) -> None:
        boots = [events.BootInfo(index=0, boot_id="boot-f", first_entry_us=3_000_000_000_000, last_entry_us=3_100_000_000_000)]
        before = events.RawPoint(ts=2_999_000.0, pct=2, status="Discharging")
        after = events.RawPoint(ts=3_000_050.0, pct=100, status="Full")
        evidence_lines = [{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set. Fs was not properly unmounted."}]
        journal = _RecordingJournalQuery({"boot-f": evidence_lines})

        record = events.classify_gap(before, after, boots, journal)

        self.assertEqual(record.kind, "hard_poweroff")

    def test_pct_boundary_5_is_low_battery_6_is_not(self) -> None:
        boots = [events.BootInfo(index=0, boot_id="boot-g", first_entry_us=3_000_000_000_000, last_entry_us=3_100_000_000_000)]
        evidence_lines = [{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set."}]
        journal = _RecordingJournalQuery({"boot-g": evidence_lines})

        at5 = events.classify_gap(
            events.RawPoint(ts=2_999_000.0, pct=5, status="Discharging"),
            events.RawPoint(ts=3_000_050.0, pct=100, status="Full"),
            boots,
            journal,
        )
        self.assertEqual(at5.kind, "hard_poweroff")

        at6 = events.classify_gap(
            events.RawPoint(ts=2_999_000.0, pct=6, status="Discharging"),
            events.RawPoint(ts=3_000_050.0, pct=100, status="Full"),
            boots,
            journal,
        )
        self.assertEqual(at6.kind, "unclean_shutdown")


class FindGapsTests(unittest.TestCase):
    def test_no_gap_under_threshold(self) -> None:
        points = [events.RawPoint(ts=float(i), pct=50, status="Discharging") for i in range(5)]
        self.assertEqual(events.find_gaps(points, threshold=30.0), [])

    def test_gap_over_threshold_found(self) -> None:
        points = [
            events.RawPoint(ts=0.0, pct=50, status="Discharging"),
            events.RawPoint(ts=100.0, pct=48, status="Discharging"),
        ]
        gaps = events.find_gaps(points, threshold=30.0)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][0].ts, 0.0)
        self.assertEqual(gaps[0][1].ts, 100.0)

    def test_points_are_sorted_before_gap_detection(self) -> None:
        points = [
            events.RawPoint(ts=100.0, pct=48, status="Discharging"),
            events.RawPoint(ts=0.0, pct=50, status="Discharging"),
        ]
        gaps = events.find_gaps(points, threshold=30.0)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][0].ts, 0.0)

    def test_scan_for_events_includes_trailing_now_gap(self) -> None:
        points = [events.RawPoint(ts=1000.0, pct=50, status="Discharging")]
        boots = [events.BootInfo(index=0, boot_id="boot-h", first_entry_us=int(1000.5 * 1_000_000), last_entry_us=int(2000 * 1_000_000))]
        journal = _RecordingJournalQuery({"boot-h": []})
        records = events.scan_for_events(points, boots, journal, now=2000.0, threshold=30.0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].ts_start, 1000.0)

    def test_scan_for_events_no_trailing_gap_when_now_is_recent(self) -> None:
        points = [events.RawPoint(ts=1000.0, pct=50, status="Discharging")]
        records = events.scan_for_events(points, [], lambda b: [], now=1005.0, threshold=30.0)
        self.assertEqual(records, [])


class EvidencePreferenceTests(unittest.TestCase):
    """`_find_evidence` prefers the fsck "Dirty bit" line over the journald
    "uncleanly shut down" line when both are present -- regardless of which
    order they appear in (S4, advisor: line ORDER must not matter, only
    identity)."""

    def test_fsck_wins_when_it_comes_first(self) -> None:
        lines = [
            {"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set. Fs was not properly unmounted."},
            {"SYSLOG_IDENTIFIER": "systemd-journald", "MESSAGE": "File ... corrupted or uncleanly shut down, renaming and replacing."},
        ]
        self.assertIn("Dirty bit is set", events._find_evidence(lines))

    def test_fsck_wins_when_journald_line_comes_first(self) -> None:
        # S4 (advisor): the real captured fixture and most other tests
        # happen to list fsck before journald -- this proves the
        # preference is by IDENTITY of the line, not by which one the
        # journal happened to emit (and this module happened to see) first.
        lines = [
            {"SYSLOG_IDENTIFIER": "systemd-journald", "MESSAGE": "File ... corrupted or uncleanly shut down, renaming and replacing."},
            {"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set. Fs was not properly unmounted."},
        ]
        detail = events._find_evidence(lines)
        self.assertIn("Dirty bit is set", detail)
        self.assertNotIn("uncleanly shut down", detail)

    def test_journald_only_is_still_evidence(self) -> None:
        lines = [{"SYSLOG_IDENTIFIER": "systemd-journald", "MESSAGE": "File ... corrupted or uncleanly shut down, renaming and replacing."}]
        self.assertIn("uncleanly shut down", events._find_evidence(lines))

    def test_no_matching_lines_is_none(self) -> None:
        lines = [{"SYSLOG_IDENTIFIER": "systemd-journald", "MESSAGE": "Journal started"}]
        self.assertIsNone(events._find_evidence(lines))

    def test_classify_gap_detail_is_fsck_line_even_when_journald_appears_first(self) -> None:
        # End-to-end version of the above, through classify_gap.
        boots = [events.BootInfo(index=0, boot_id="boot-order", first_entry_us=3_000_000_000_000, last_entry_us=3_100_000_000_000)]
        lines = [
            {"SYSLOG_IDENTIFIER": "systemd-journald", "MESSAGE": "File ... corrupted or uncleanly shut down, renaming and replacing."},
            {"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set. Fs was not properly unmounted."},
        ]
        journal = _RecordingJournalQuery({"boot-order": lines})
        before = events.RawPoint(ts=2_999_000.0, pct=2, status="Discharging")
        after = events.RawPoint(ts=3_000_050.0, pct=100, status="Full")

        record = events.classify_gap(before, after, boots, journal)

        self.assertEqual(record.kind, "hard_poweroff")
        self.assertIn("Dirty bit is set", record.detail)


class QueryBootJournalParsingTests(unittest.TestCase):
    """S4 (advisor): `query_boot_journal` must skip blank lines and
    unparseable garbage rather than choke on them -- journalctl's own
    output is JSON-lines, but this defends against a truncated read or a
    stray blank line at EOF."""

    def test_blank_lines_are_skipped(self) -> None:
        from unittest import mock

        raw = (
            '{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set."}\n'
            "\n"
            "   \n"
            '{"SYSLOG_IDENTIFIER": "systemd-journald", "MESSAGE": "Journal started"}\n'
        )
        with mock.patch("hwmon.procutil.run", return_value=raw):
            lines = events.query_boot_journal("some-boot-id")
        self.assertEqual(len(lines), 2)

    def test_garbage_line_is_skipped_not_fatal(self) -> None:
        from unittest import mock

        raw = (
            '{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set."}\n'
            "not valid json at all {{{\n"
            '{"SYSLOG_IDENTIFIER": "systemd-journald", "MESSAGE": "Journal started"}\n'
        )
        with mock.patch("hwmon.procutil.run", return_value=raw):
            lines = events.query_boot_journal("some-boot-id")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["SYSLOG_IDENTIFIER"], "systemd-fsck")
        self.assertEqual(lines[1]["SYSLOG_IDENTIFIER"], "systemd-journald")

    def test_non_object_json_line_is_skipped(self) -> None:
        # A line that parses as valid JSON but isn't a dict (e.g. a bare
        # number or list) must also be dropped, not passed through.
        from unittest import mock

        raw = '[1, 2, 3]\n{"SYSLOG_IDENTIFIER": "systemd-fsck", "MESSAGE": "Dirty bit is set."}\n'
        with mock.patch("hwmon.procutil.run", return_value=raw):
            lines = events.query_boot_journal("some-boot-id")
        self.assertEqual(len(lines), 1)

    def test_procutil_failure_yields_none(self) -> None:
        from unittest import mock

        with mock.patch("hwmon.procutil.run", return_value=None):
            self.assertIsNone(events.query_boot_journal("some-boot-id"))

    def test_all_blank_yields_empty_list_not_none(self) -> None:
        from unittest import mock

        with mock.patch("hwmon.procutil.run", return_value="\n\n   \n"):
            lines = events.query_boot_journal("some-boot-id")
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
