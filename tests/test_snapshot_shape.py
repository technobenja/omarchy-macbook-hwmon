"""Acceptance check 0: a written snapshot's key set and types equal
tests/fixtures/latest.example.json (shape assertion, run live and in
tests). Positive control: a snapshot with one key removed fails it.

Also guards against snapshot.py's embedded `_REFERENCE_SNAPSHOT` (used so
the live daemon can shape-check every sample with no file dependency)
drifting from the committed fixture -- the actual seam between the Python
and QML halves.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from hwmon import snapshot

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "latest.example.json"


def _load_fixture() -> dict:
    with FIXTURE_PATH.open() as f:
        return json.load(f)


class FixtureDriftGuardTests(unittest.TestCase):
    """If this test goes red, snapshot.py's embedded reference has drifted
    from the committed contract file -- fix the embedded copy, never the
    fixture."""

    def test_embedded_reference_equals_committed_fixture(self) -> None:
        self.assertEqual(snapshot._REFERENCE_SNAPSHOT, _load_fixture())


class ShapeValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _load_fixture()

    def test_fixture_validates_against_itself(self) -> None:
        self.assertEqual(snapshot.validate_shape(self.fixture, self.fixture), [])

    def test_fixture_validates_against_embedded_reference(self) -> None:
        self.assertEqual(snapshot.validate_shape(self.fixture), [])

    def test_positive_control_missing_top_level_key_fails(self) -> None:
        broken = copy.deepcopy(self.fixture)
        del broken["fan"]
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(errors)
        self.assertTrue(any("fan" in e for e in errors))

    def test_positive_control_missing_nested_key_fails(self) -> None:
        broken = copy.deepcopy(self.fixture)
        del broken["battery"]["cycles"]
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(errors)
        self.assertTrue(any("battery.cycles" in e for e in errors))

    def test_unexpected_extra_key_fails(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["battery"]["bogus"] = 1
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("battery.bogus" in e for e in errors))

    def test_wrong_type_fails(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["battery"]["pct"] = "not-a-number"
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("battery.pct" in e for e in errors))

    def test_null_leaf_is_allowed_where_nullable(self) -> None:
        # Every leaf is nullable except schema and ts (A2).
        broken = copy.deepcopy(self.fixture)
        broken["battery"]["pct"] = None
        broken["cpu"]["package_c"] = None
        broken["system"]["net"]["rx_bps"] = None
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [])

    def test_schema_and_ts_are_not_nullable(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["schema"] = None
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("schema" in e and "null" in e for e in errors))

        broken = copy.deepcopy(self.fixture)
        broken["ts"] = None
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("ts" in e and "null" in e for e in errors))

    def test_cpu_load_must_have_three_elements(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["cpu"]["load"] = [0.1, 0.2]
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("cpu.load" in e for e in errors))

    def test_per_core_elements_are_validated_by_template(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["cpu"]["per_core"][0]["usage_pct"] = "bad"
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("per_core[0].usage_pct" in e for e in errors))

    def test_power_guard_blockers_is_checked_per_element(self) -> None:
        # M8: "power_guard.blockers is a list checked per element". The
        # fixture holds exactly one blocker (S7) so this has something to
        # mutate against.
        broken = copy.deepcopy(self.fixture)
        broken["power_guard"]["blockers"][0]["why"] = 123  # should be a str
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("blockers[0].why" in e for e in errors), msg=f"shape errors: {errors}")

    def test_power_guard_blockers_extra_key_fails(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["power_guard"]["blockers"][0]["bogus"] = 1
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("blockers[0].bogus" in e for e in errors), msg=f"shape errors: {errors}")

    def test_power_guard_blockers_empty_list_is_valid(self) -> None:
        # S7: blockers is [] (never null) when nothing blocks.
        broken = copy.deepcopy(self.fixture)
        broken["power_guard"]["sleep_blocked"] = False
        broken["power_guard"]["blockers"] = []
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [])

    def test_cpu_throttle_exact_key_set_required(self) -> None:
        # M8: cpu.throttle is a fixed-schema dict (not a dynamic label map),
        # unlike temps/cores_c.
        broken = copy.deepcopy(self.fixture)
        broken["cpu"]["throttle"]["bogus"] = 1
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("throttle.bogus" in e for e in errors))

    def test_fan_control_and_target_rpm_are_nullable(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["fan"]["control"] = None
        broken["fan"]["target_rpm"] = None
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [])

    def test_recovery_missing_key_fails(self) -> None:
        # M-v4: schema 2 -> 3 adds `recovery` (R-L4.1, deliverables SPEC.md).
        broken = copy.deepcopy(self.fixture)
        del broken["recovery"]
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("recovery" in e for e in errors))

    def test_recovery_exact_key_set_required(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["recovery"]["bogus"] = 1
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("recovery.bogus" in e for e in errors))

    def test_recovery_home_snapshot_age_s_is_nullable(self) -> None:
        # `not_configured`/`unknown` states carry a null age (R-L4.1).
        broken = copy.deepcopy(self.fixture)
        broken["recovery"]["home_snapshot_state"] = "not_configured"
        broken["recovery"]["home_snapshot_age_s"] = None
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [])

    def test_recovery_home_snapshot_state_wrong_type_fails(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["recovery"]["home_snapshot_state"] = 123
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("recovery.home_snapshot_state" in e for e in errors))

    def test_schema_is_3(self) -> None:
        self.assertEqual(self.fixture["schema"], 3)

    def test_recovery_upower_missing_key_fails(self) -> None:
        # Fix pass (deliverables SPEC.md §11 R3): recovery.upower.
        broken = copy.deepcopy(self.fixture)
        del broken["recovery"]["upower"]
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("recovery.upower" in e for e in errors))

    def test_recovery_upower_exact_key_set_required(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["recovery"]["upower"]["bogus"] = 1
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("recovery.upower.bogus" in e for e in errors))

    def test_recovery_upower_pct_fields_are_nullable(self) -> None:
        # "unknown" state carries null upower_pct/sysfs_pct.
        broken = copy.deepcopy(self.fixture)
        broken["recovery"]["upower"] = {"state": "unknown", "upower_pct": None, "sysfs_pct": None}
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [])

    def test_recovery_upower_state_wrong_type_fails(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["recovery"]["upower"]["state"] = 123
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("recovery.upower.state" in e for e in errors))


class DynamicLabelMapStructuralCheckTests(unittest.TestCase):
    """Corrected 2026-09-23 (spec A3 dated note): `temps` and `cpu.cores_c`
    are label -> float maps whose KEY SET is data (which SMC sensors are
    currently valid), not schema -- unlike every other dict in the
    snapshot. Live symptom this fixes: TH0F drifting from invalid (-43C,
    caught by the old A3 cutoff) to a reading the (old, -40C) cutoff let
    through as "valid" produced a real, unpredictable key that isn't in the
    committed fixture's `temps`, and the exact-key check flagged it as
    "unexpected key" every single tick. Which labels are present is exactly
    what A3 already decides -- validate_shape must not re-decide it."""

    def setUp(self) -> None:
        self.fixture = _load_fixture()

    def test_temps_extra_label_not_in_fixture_does_not_fail(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["temps"]["TXXX"] = 55.0  # a label the fixture has never seen
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [], msg=f"shape errors: {errors}")

    def test_temps_missing_a_fixture_label_does_not_fail(self) -> None:
        broken = copy.deepcopy(self.fixture)
        del broken["temps"]["TC1C"]  # a sensor that just read invalid this tick
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [], msg=f"shape errors: {errors}")

    def test_temps_can_be_a_totally_different_label_set(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["temps"] = {"SOMETHING_ELSE": 12.5}
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [], msg=f"shape errors: {errors}")

    def test_temps_value_must_still_be_float_or_null(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["temps"]["TC1C"] = "hot"
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("temps.TC1C" in e for e in errors), msg=f"shape errors: {errors}")

    def test_temps_null_value_is_allowed(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["temps"]["TC1C"] = None
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [], msg=f"shape errors: {errors}")

    def test_cores_c_extra_label_does_not_fail(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["cpu"]["cores_c"]["Core 2"] = 61.0
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [], msg=f"shape errors: {errors}")

    def test_cores_c_missing_label_does_not_fail(self) -> None:
        broken = copy.deepcopy(self.fixture)
        del broken["cpu"]["cores_c"]["Core 1"]
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertEqual(errors, [], msg=f"shape errors: {errors}")

    def test_cores_c_value_must_still_be_float_or_null(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["cpu"]["cores_c"]["Core 0"] = "warm"
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("cores_c.Core 0" in e for e in errors), msg=f"shape errors: {errors}")

    def test_non_dynamic_dicts_still_require_the_exact_key_set(self) -> None:
        # The structural relaxation is scoped to temps/cores_c only -- every
        # other dict in the snapshot (fixed schema, not sensor labels) still
        # requires an exact key match. Positive control for the scoping.
        broken = copy.deepcopy(self.fixture)
        del broken["system"]["mem_used_bytes"]
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("system.mem_used_bytes" in e for e in errors))

        broken = copy.deepcopy(self.fixture)
        broken["battery"]["bogus"] = 1
        errors = snapshot.validate_shape(broken, self.fixture)
        self.assertTrue(any("battery.bogus" in e for e in errors))


class LiveSnapshotShapeTests(unittest.TestCase):
    """The same check the daemon runs on every sample it actually builds
    (not just on the fixture) -- against a fake sysfs tree.

    Deliberately uses only a SMALL subset of SMC labels, not the fixture's
    full 31-sensor set: since temps/cores_c are checked structurally
    (label -> float, not an exact key set -- see
    DynamicLabelMapStructuralCheckTests), a live snapshot whose valid/invalid
    SMC split differs from the fixture's must still pass. That's the
    behavior this class exists to prove; matching the fixture's set exactly
    would no longer prove anything the structural check doesn't already
    guarantee.
    """

    def setUp(self) -> None:
        import shutil
        import tempfile

        from . import fakefs

        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.fixture = _load_fixture()
        sysfs = self._tmp / "sys"
        procfs = self._tmp / "proc"

        fakefs.add_coretemp(sysfs, index=3)
        fakefs.add_applesmc(
            sysfs,
            index=2,
            temps_milli_c={
                "TA0P": 41250,
                "TB0T": 35250,
                "TH0F": -34750,  # the live drift case: invalid under the corrected 0C cutoff
            },
        )
        fakefs.add_power_supply(
            sysfs,
            "BAT0",
            {
                "capacity": "81",
                "status": "Discharging",
                "voltage_now": "11247000",
                "current_now": "1390000",
                "temp": "345",
                "cycle_count": "3",
                "charge_now": "5435000",
                "charge_full": "6710000",
                "charge_full_design": "6400000",
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

        self.sysfs = sysfs
        self.procfs = procfs

    def test_live_built_snapshot_passes_shape_check(self) -> None:
        snap, state = snapshot.build_snapshot(self.sysfs, self.procfs, None, now=1_000_000.0)
        errors = snapshot.validate_shape(snap)
        self.assertEqual(errors, [], msg=f"shape errors: {errors}")

    def test_live_second_sample_also_passes(self) -> None:
        snap1, state1 = snapshot.build_snapshot(self.sysfs, self.procfs, None, now=1_000_000.0)
        snap2, state2 = snapshot.build_snapshot(self.sysfs, self.procfs, state1, now=1_000_001.0)
        errors = snapshot.validate_shape(snap2)
        self.assertEqual(errors, [], msg=f"shape errors: {errors}")

    def test_positive_control_live_snapshot_missing_key_fails_shape_check(self) -> None:
        snap, _ = snapshot.build_snapshot(self.sysfs, self.procfs, None, now=1_000_000.0)
        del snap["fan"]
        errors = snapshot.validate_shape(snap)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
