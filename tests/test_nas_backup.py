"""R-N7 -- NAS backup freshness, the schema-4 `recovery.nas_backup` key
(deliverables `SPEC-nas-backup.md`). Four states, each with its own
positive control: `not_configured` (no status file), `unknown`
(unreadable/invalid JSON/missing keys), `failed` (last recorded result),
and a real `fresh`/`stale` reading from `last_ok_ts`.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from hwmon import nas_backup

NOW = 2_000_000.0
DAY = 86400.0


class ComputeNasBackupStateTests(unittest.TestCase):
    def test_no_file_is_not_configured_never_stale(self) -> None:
        # Task requirement: "no file" MUST NOT read as stale/red.
        result = nas_backup.compute_nas_backup_state(file_present=False, raw_json=None, now=NOW)
        self.assertEqual(result, {"state": "not_configured", "age_s": None, "reason": None})

    def test_file_present_but_unreadable_is_unknown(self) -> None:
        # read_fn returning None (an OSError was swallowed) with the file present.
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=None, now=NOW)
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["age_s"])

    def test_invalid_json_is_unknown(self) -> None:
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json="not json at all {", now=NOW)
        self.assertEqual(result["state"], "unknown")

    def test_json_array_not_object_is_unknown(self) -> None:
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json="[1, 2, 3]", now=NOW)
        self.assertEqual(result["state"], "unknown")

    def test_missing_result_key_is_unknown(self) -> None:
        result = nas_backup.compute_nas_backup_state(
            file_present=True, raw_json=json.dumps({"ts": NOW}), now=NOW
        )
        self.assertEqual(result["state"], "unknown")

    def test_bogus_result_value_is_unknown(self) -> None:
        result = nas_backup.compute_nas_backup_state(
            file_present=True, raw_json=json.dumps({"result": "sideways", "ts": NOW}), now=NOW
        )
        self.assertEqual(result["state"], "unknown")

    def test_ok_result_recent_is_fresh(self) -> None:
        record = {"result": "ok", "ts": NOW - 3600, "last_ok_ts": NOW - 3600}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "fresh")
        self.assertAlmostEqual(result["age_s"], 3600.0, delta=1.0)

    def test_ok_result_without_last_ok_ts_falls_back_to_ts(self) -> None:
        # The writer's own "ok" row IS a last-ok event, even if it hasn't
        # echoed last_ok_ts back onto itself yet.
        record = {"result": "ok", "ts": NOW - 3600}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "fresh")
        self.assertAlmostEqual(result["age_s"], 3600.0, delta=1.0)

    def test_stale_past_72_hours(self) -> None:
        # Positive control for the fresh/stale boundary (task's back-dated-73h scenario).
        record = {"result": "ok", "ts": NOW - 73 * 3600, "last_ok_ts": NOW - 73 * 3600}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "stale")

    def test_exactly_72_hours_is_fresh_not_stale(self) -> None:
        record = {"result": "ok", "ts": NOW - nas_backup.STALE_AFTER_S, "last_ok_ts": NOW - nas_backup.STALE_AFTER_S}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "fresh")

    def test_skipped_alone_never_sets_failed(self) -> None:
        # Task requirement, verbatim: "skipped alone never sets failed; it
        # only lets age grow." A skipped run one hour after a two-day-old ok
        # must read stale (from age), never failed.
        record = {"result": "skipped", "reason": "nas-unreachable", "ts": NOW, "last_ok_ts": NOW - 4 * DAY}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "stale")
        self.assertNotEqual(result["state"], "failed")

    def test_skipped_recent_last_ok_is_fresh(self) -> None:
        record = {"result": "skipped", "reason": "on-battery", "ts": NOW, "last_ok_ts": NOW - 3600}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "fresh")

    def test_skipped_with_no_ok_ever_is_stale_not_fresh(self) -> None:
        record = {"result": "skipped", "reason": "nas-unreachable", "ts": NOW}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "stale")
        self.assertIsNone(result["age_s"])

    def test_failed_result_is_failed_with_reason(self) -> None:
        record = {"result": "failed", "reason": "no-fresh-source", "ts": NOW, "last_ok_ts": NOW - DAY}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "no-fresh-source")
        self.assertAlmostEqual(result["age_s"], DAY, delta=1.0)

    def test_failed_result_with_no_prior_ok_has_null_age(self) -> None:
        record = {"result": "failed", "reason": "no-password", "ts": NOW}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["age_s"])
        self.assertEqual(result["reason"], "no-password")

    def test_non_string_reason_is_dropped_not_fatal(self) -> None:
        record = {"result": "failed", "reason": 42, "ts": NOW}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["reason"])

    def test_non_numeric_timestamps_are_ignored_not_fatal(self) -> None:
        record = {"result": "ok", "ts": "not-a-number", "last_ok_ts": "also-not-a-number"}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        # No usable timestamp at all on an "ok" row -- treated the same as
        # "no ok ever": stale, not a guessed fresh.
        self.assertEqual(result["state"], "stale")
        self.assertIsNone(result["age_s"])

    def test_bool_is_not_mistaken_for_a_timestamp(self) -> None:
        # bool is an int subclass in Python -- must not be accepted as a ts.
        record = {"result": "ok", "ts": True, "last_ok_ts": True}
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "stale")
        self.assertIsNone(result["age_s"])

    def test_extra_contract_fields_are_ignored(self) -> None:
        # snapshot_id, bytes_added, files_new, files_changed, source_snapper,
        # duration_s, iso -- this module doesn't read them at all.
        record = {
            "ts": NOW,
            "iso": "2026-09-24T12:00:00Z",
            "result": "ok",
            "reason": None,
            "snapshot_id": "abc123",
            "bytes_added": 10485760,
            "files_new": 3,
            "files_changed": 1,
            "source_snapper": "/home/.snapshots/42/snapshot",
            "duration_s": 61.2,
            "last_ok_ts": NOW,
        }
        result = nas_backup.compute_nas_backup_state(file_present=True, raw_json=json.dumps(record), now=NOW)
        self.assertEqual(result["state"], "fresh")


class ReadStatusFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-nas-backup-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(nas_backup.read_status_file(self._tmp / "does-not-exist.json"))

    def test_present_file_returns_text(self) -> None:
        path = self._tmp / "nas-backup.json"
        path.write_text('{"result": "ok"}')
        self.assertEqual(nas_backup.read_status_file(path), '{"result": "ok"}')

    def test_directory_in_place_of_file_returns_none(self) -> None:
        path = self._tmp / "a-directory"
        path.mkdir()
        self.assertIsNone(nas_backup.read_status_file(path))


class DefaultStatePathTests(unittest.TestCase):
    def test_uses_xdg_state_home_when_set(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-state-test"}, clear=False):
            self.assertEqual(
                nas_backup.default_state_path(), Path("/tmp/xdg-state-test/omarchy-recovery/nas-backup.json")
            )

    def test_falls_back_to_home_local_state_when_unset(self) -> None:
        import os
        from unittest import mock

        env = dict(os.environ)
        env.pop("XDG_STATE_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            expected = Path.home() / ".local" / "state" / "omarchy-recovery" / "nas-backup.json"
            self.assertEqual(nas_backup.default_state_path(), expected)


class NasBackupCacheTests(unittest.TestCase):
    def test_polls_at_most_once_per_ttl_when_file_absent(self) -> None:
        calls = []

        class _Clock:
            def __init__(self):
                self.t = 1000.0

            def __call__(self):
                return self.t

        clock = _Clock()
        cache = nas_backup.NasBackupCache(
            ttl_s=60.0,
            state_path=Path("/nonexistent/nas-backup-for-test.json"),
            read_fn=lambda p: calls.append(1) or None,
            clock=clock,
            now_fn=lambda: NOW,
        )
        for _ in range(60):
            cache.get()
            clock.t += 1.0
        self.assertEqual(len(calls), 0, "absent file -> read_fn must never even be called")

    def test_polls_read_fn_at_most_once_per_ttl_when_file_present(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-nas-backup-cache-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        status_path = self._tmp / "nas-backup.json"
        status_path.write_text(json.dumps({"result": "ok", "ts": NOW, "last_ok_ts": NOW}))

        calls = []

        class _Clock:
            def __init__(self):
                self.t = 1000.0

            def __call__(self):
                return self.t

        clock = _Clock()
        cache = nas_backup.NasBackupCache(
            ttl_s=60.0,
            state_path=status_path,
            read_fn=lambda p: calls.append(1) or p.read_text(),
            clock=clock,
            now_fn=lambda: NOW,
        )
        for _ in range(60):
            cache.get()
            clock.t += 1.0
        self.assertEqual(len(calls), 1)

    def test_not_configured_is_the_null_default(self) -> None:
        cache = nas_backup.NasBackupCache(state_path=Path("/nonexistent/nas-backup-for-test.json"))
        self.assertEqual(cache.get(), {"state": "not_configured", "age_s": None, "reason": None})


if __name__ == "__main__":
    unittest.main()
