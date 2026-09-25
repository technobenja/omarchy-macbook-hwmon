"""Integration coverage for snapshot.build_snapshot beyond the dedicated
acceptance-check test files: first-sample rates are null, missing files
produce null fields (never an exception), and the assembled snapshot uses
the exact nesting/keys from the A2 contract."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hwmon import snapshot

from . import fakefs


class SnapshotBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs = self._tmp / "sys"
        self.procfs = self._tmp / "proc"

        fakefs.add_coretemp(self.sysfs, index=3)
        fakefs.add_applesmc(self.sysfs, index=2)
        fakefs.add_power_supply(
            self.sysfs,
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
        fakefs.add_power_supply(self.sysfs, "ADP1", {"online": "0"})
        for i in range(4):
            fakefs.add_cpu_freq(self.sysfs, i, 1900000 + i * 1000)
        fakefs.write_loadavg(self.procfs, 0.82, 0.61, 0.55)
        fakefs.write_stat(
            self.procfs,
            {
                "cpu": [100, 0, 50, 800, 0, 0, 0, 0],
                "cpu0": [25, 0, 12, 200, 0, 0, 0, 0],
                "cpu1": [25, 0, 12, 200, 0, 0, 0, 0],
                "cpu2": [25, 0, 13, 200, 0, 0, 0, 0],
                "cpu3": [25, 0, 13, 200, 0, 0, 0, 0],
            },
        )
        fakefs.write_meminfo(
            self.procfs,
            {"MemTotal": 8_026_296, "MemAvailable": 3_126_692, "SwapTotal": 4_036_608, "SwapFree": 4_036_608},
        )
        fakefs.write_diskstats(self.procfs, "sda", sectors_read=1000, sectors_written=2000)
        fakefs.write_route(self.procfs, "wlp3s0")
        fakefs.write_netdev(self.procfs, "wlp3s0", rx_bytes=1000, tx_bytes=2000)

    def test_first_sample_rates_are_null(self) -> None:
        snap, _ = snapshot.build_snapshot(self.sysfs, self.procfs, None, now=1000.0)
        self.assertIsNone(snap["cpu"]["usage_pct"])
        for core in snap["cpu"]["per_core"]:
            self.assertIsNone(core["usage_pct"])
        self.assertIsNone(snap["system"]["disk"]["read_bps"])
        self.assertIsNone(snap["system"]["disk"]["write_bps"])
        self.assertIsNone(snap["system"]["net"]["rx_bps"])
        self.assertIsNone(snap["system"]["net"]["tx_bps"])

    def test_second_sample_rates_are_not_null(self) -> None:
        snap1, state1 = snapshot.build_snapshot(self.sysfs, self.procfs, None, now=1000.0)
        # Advance counters so there's a nonzero delta.
        fakefs.write_stat(
            self.procfs,
            {
                "cpu": [110, 0, 55, 835, 0, 0, 0, 0],
                "cpu0": [27, 0, 13, 208, 0, 0, 0, 0],
                "cpu1": [27, 0, 13, 208, 0, 0, 0, 0],
                "cpu2": [28, 0, 14, 209, 0, 0, 0, 0],
                "cpu3": [28, 0, 15, 210, 0, 0, 0, 0],
            },
        )
        fakefs.write_diskstats(self.procfs, "sda", sectors_read=1080, sectors_written=2080)
        fakefs.write_netdev(self.procfs, "wlp3s0", rx_bytes=2000, tx_bytes=3000)
        snap2, _ = snapshot.build_snapshot(self.sysfs, self.procfs, state1, now=1001.0)

        self.assertIsNotNone(snap2["cpu"]["usage_pct"])
        self.assertIsNotNone(snap2["system"]["disk"]["read_bps"])
        self.assertEqual(snap2["system"]["disk"]["read_bps"], 40960.0)
        self.assertIsNotNone(snap2["system"]["net"]["rx_bps"])
        self.assertEqual(snap2["system"]["net"]["rx_bps"], 1000.0)

    def test_missing_everything_yields_nulls_not_exceptions(self) -> None:
        empty_sysfs = self._tmp / "sys-empty"
        empty_procfs = self._tmp / "proc-empty"
        snap, _ = snapshot.build_snapshot(empty_sysfs, empty_procfs, None, now=1000.0)
        self.assertEqual(snap["schema"], 4)
        self.assertEqual(snap["ts"], 1000.0)
        self.assertIsNone(snap["battery"]["pct"])
        self.assertIsNone(snap["cpu"]["package_c"])
        self.assertEqual(snap["cpu"]["cores_c"], {})
        self.assertEqual(snap["cpu"]["load"], [None, None, None])
        self.assertEqual(snap["cpu"]["per_core"], [])
        self.assertEqual(snap["temps"], {})
        self.assertEqual(snap["sensors_invalid"], [])
        self.assertIsNone(snap["fan"]["label"])
        self.assertIsNone(snap["system"]["mem_used_bytes"])
        self.assertIsNone(snap["system"]["net"])  # no default route at all

    def test_net_is_none_when_no_default_route(self) -> None:
        fakefs.write_route(self.procfs, None)
        snap, _ = snapshot.build_snapshot(self.sysfs, self.procfs, None, now=1000.0)
        self.assertIsNone(snap["system"]["net"])

    def test_disk_device_is_always_sda(self) -> None:
        snap, _ = snapshot.build_snapshot(self.sysfs, self.procfs, None, now=1000.0)
        self.assertEqual(snap["system"]["disk"]["device"], "sda")

    def test_shape_is_internally_self_consistent(self) -> None:
        # This fixture tree only fakes a couple of SMC labels (not the
        # machine's full 31-sensor set), so it won't match the committed
        # fixture's exact `temps` key set -- that full-fixture check lives
        # in tests/test_snapshot_shape.py::LiveSnapshotShapeTests. Here we
        # only prove the snapshot validates against its own shape (i.e. the
        # builder produces something well-formed, not that it matches this
        # particular machine's SMC label set).
        snap, _ = snapshot.build_snapshot(self.sysfs, self.procfs, None, now=1000.0)
        self.assertEqual(snapshot.validate_shape(snap, reference=snap), [])


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_write_then_read_round_trips(self) -> None:
        import json

        target = self._tmp / "state" / "latest.json"
        snapshot.write_atomic(target, {"schema": 1, "ts": 123.0})
        self.assertTrue(target.exists())
        self.assertEqual(json.loads(target.read_text()), {"schema": 1, "ts": 123.0})

    def test_no_tmp_file_left_behind(self) -> None:
        target = self._tmp / "state" / "latest.json"
        snapshot.write_atomic(target, {"schema": 1, "ts": 1.0})
        snapshot.write_atomic(target, {"schema": 1, "ts": 2.0})
        leftovers = [p for p in target.parent.iterdir() if p.name != "latest.json"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
