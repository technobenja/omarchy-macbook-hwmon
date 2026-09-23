"""Acceptance check 3: battery.power_w < 0 while discharging, > 0 while
charging (fixture both ways); 0 for any other status (Full, Not charging,
Unknown) per A4."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hwmon import sensors

from . import fakefs

_COMMON_FIELDS = {
    "capacity": "81",
    "voltage_now": "11247000",
    "current_now": "1390000",  # unsigned magnitude, same regardless of status
}


class BatteryPowerSignTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs = self._tmp / "sys"

    def _battery_with_status(self, status: str) -> dict:
        fields = dict(_COMMON_FIELDS)
        fields["status"] = status
        fakefs.add_power_supply(self.sysfs, "BAT0", fields)
        return sensors.read_battery(self.sysfs)

    def test_discharging_is_negative(self) -> None:
        battery = self._battery_with_status("Discharging")
        self.assertLess(battery["power_w"], 0)
        self.assertEqual(battery["power_w"], -15.63)

    def test_charging_is_positive(self) -> None:
        battery = self._battery_with_status("Charging")
        self.assertGreater(battery["power_w"], 0)
        self.assertEqual(battery["power_w"], 15.63)

    def test_full_is_zero(self) -> None:
        battery = self._battery_with_status("Full")
        self.assertEqual(battery["power_w"], 0.0)

    def test_not_charging_is_zero(self) -> None:
        battery = self._battery_with_status("Not charging")
        self.assertEqual(battery["power_w"], 0.0)

    def test_unknown_is_zero(self) -> None:
        battery = self._battery_with_status("Unknown")
        self.assertEqual(battery["power_w"], 0.0)

    def test_missing_status_is_null_not_zero(self) -> None:
        fields = dict(_COMMON_FIELDS)
        fakefs.add_power_supply(self.sysfs, "BAT0", fields)  # no "status" file
        battery = sensors.read_battery(self.sysfs)
        self.assertIsNone(battery["power_w"])


if __name__ == "__main__":
    unittest.main()
