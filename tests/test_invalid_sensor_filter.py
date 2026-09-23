"""Acceptance check 2: a sensor reading <= 0C or >= 130C lands in
sensors_invalid and not in temps -- proven with a fixture, with a positive
control (a valid sensor in the SAME fixture appears in temps).

Threshold corrected 2026-09-23 (spec A3, dated note): -40C -> 0C after a
live junk sensor (TH0F) drifted from -43C to -34.75C -- still obvious junk
(nothing inside a running laptop is below freezing) but no longer caught by
the old -40C cutoff. See specs/spec.md A3.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hwmon import sensors

from . import fakefs


class InvalidSensorFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs = self._tmp / "sys"

    def test_negative_127_is_invalid_and_absent_from_temps(self) -> None:
        fakefs.add_applesmc(
            self.sysfs,
            index=2,
            temps_milli_c={
                "TH0C": -127000,  # invalid: <= 0C
                "TC0P": 58000,  # valid, positive control
            },
        )
        temps, invalid = sensors.read_smc_temps(self.sysfs)
        self.assertIn("TH0C", invalid)
        self.assertNotIn("TH0C", temps)
        # Positive control: a valid sensor in the SAME fixture appears in temps.
        self.assertIn("TC0P", temps)
        self.assertEqual(temps["TC0P"], 58.0)
        self.assertNotIn("TC0P", invalid)

    def test_th0f_drift_to_minus_34_75_is_invalid_under_the_corrected_cutoff(self) -> None:
        # Live 2026-09-23: TH0F drifted from -43C (caught by the old -40C
        # cutoff) to -34.75C (NOT caught by it -- still obvious junk, nothing
        # inside a running laptop is below freezing). Corrected cutoff: 0C.
        fakefs.add_applesmc(
            self.sysfs,
            index=2,
            temps_milli_c={
                "TH0F": -34750,  # invalid under the corrected <= 0C cutoff
                "TC0P": 40000,  # positive control: a valid 40.0 stays in temps
            },
        )
        temps, invalid = sensors.read_smc_temps(self.sysfs)
        self.assertIn("TH0F", invalid)
        self.assertNotIn("TH0F", temps)
        self.assertIn("TC0P", temps)
        self.assertEqual(temps["TC0P"], 40.0)
        self.assertNotIn("TC0P", invalid)

    def test_boundary_values(self) -> None:
        fakefs.add_applesmc(
            self.sysfs,
            index=2,
            temps_milli_c={
                "AT0": 0,  # exactly 0C -> invalid (<=)
                "AT0PLUS": 1000,  # just above 0C -> valid
                "AT130": 130000,  # exactly 130C -> invalid (>=)
                "AT129": 129999,  # just below 130C -> valid
            },
        )
        temps, invalid = sensors.read_smc_temps(self.sysfs)
        self.assertIn("AT0", invalid)
        self.assertIn("AT130", invalid)
        self.assertIn("AT0PLUS", temps)
        self.assertIn("AT129", temps)

    def test_measured_invalid_values_from_this_machine(self) -> None:
        # Measured 2026-09-23 (spec: "several read -127000, -43000, -42750").
        fakefs.add_applesmc(
            self.sysfs,
            index=2,
            temps_milli_c={
                "TH0C": -127000,
                "TH0F": -43000,
                "TH0R": -42750,
                "TC0P": 58000,
            },
        )
        temps, invalid = sensors.read_smc_temps(self.sysfs)
        self.assertEqual(sorted(invalid), ["TH0C", "TH0F", "TH0R"])
        self.assertEqual(set(temps), {"TC0P"})

    def test_invalid_list_is_sorted(self) -> None:
        fakefs.add_applesmc(
            self.sysfs,
            index=2,
            temps_milli_c={"TW0P": -127000, "TH0C": -127000, "TMLB": -127000},
        )
        _, invalid = sensors.read_smc_temps(self.sysfs)
        self.assertEqual(invalid, sorted(invalid))

    def test_mutation_a_broken_filter_is_caught_by_this_test(self) -> None:
        """Prove the test itself can fail: temporarily monkeypatch the
        threshold so an obviously-invalid reading is (wrongly) accepted,
        and confirm the assertion above would have failed. This is a
        self-contained mutation check, safe to leave in the suite -- it
        restores the real constant immediately via try/finally and never
        touches source files."""
        fakefs.add_applesmc(self.sysfs, index=2, temps_milli_c={"TH0C": -127000})
        original_min = sensors.INVALID_MIN_C
        try:
            sensors.INVALID_MIN_C = -1000.0  # nothing is invalid anymore
            temps, invalid = sensors.read_smc_temps(self.sysfs)
            # With the guard broken, the invalid reading is wrongly accepted.
            self.assertIn("TH0C", temps)
            self.assertNotIn("TH0C", invalid)
        finally:
            sensors.INVALID_MIN_C = original_min
        # And with the real constant restored, the filter is correct again.
        temps, invalid = sensors.read_smc_temps(self.sysfs)
        self.assertIn("TH0C", invalid)
        self.assertNotIn("TH0C", temps)


if __name__ == "__main__":
    unittest.main()
