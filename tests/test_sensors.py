from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hwmon import sensors

from . import fakefs


class TempRootTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs = self._tmp / "sys"
        self.procfs = self._tmp / "proc"
        # A14: a fake /run, so a test that sets fan_manual=1 never touches
        # this machine's REAL /run/mbpfan.pid -- reading real host state in
        # a unit test is exactly the nondeterminism this fixture exists to
        # avoid (this machine really does run mbpfan).
        self.run = self._tmp / "run"


class ReadTextTests(TempRootTestCase):
    """Some sysfs attributes fail not at open() but at read() time -- the
    driver returns -EIO/-ENODATA when it briefly can't reach the hardware
    (measured live: two of this machine's 31 SMC temp sensors, intermittently).
    That must be exactly as non-fatal as a missing file, never an exception."""

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(sensors.read_text(self.sysfs / "nope"))

    def test_normal_file_reads_and_strips(self) -> None:
        path = self._tmp / "f.txt"
        path.write_text("  hello  \n")
        self.assertEqual(sensors.read_text(path), "hello")

    def test_oserror_at_read_time_returns_none_not_exception(self) -> None:
        path = self._tmp / "f.txt"
        path.write_text("42\n")
        with mock.patch("os.read", side_effect=OSError("simulated -EIO")):
            self.assertIsNone(sensors.read_text(path))

    def test_read_int_also_survives_read_time_oserror(self) -> None:
        path = self._tmp / "f.txt"
        path.write_text("42\n")
        with mock.patch("os.read", side_effect=OSError("simulated -EIO")):
            self.assertIsNone(sensors.read_int(path))

    def test_fd_is_closed_even_when_read_raises(self) -> None:
        path = self._tmp / "f.txt"
        path.write_text("42\n")
        real_close = __import__("os").close
        closed_fds: list[int] = []
        with mock.patch("os.close", side_effect=lambda fd: (closed_fds.append(fd), real_close(fd))):
            with mock.patch("os.read", side_effect=OSError("simulated -EIO")):
                sensors.read_text(path)
        self.assertEqual(len(closed_fds), 1)


class HwmonDiscoveryTests(TempRootTestCase):
    def test_finds_coretemp_by_name_file_directly(self) -> None:
        fakefs.add_coretemp(self.sysfs, index=3)
        base = sensors.find_hwmon_by_name(self.sysfs, "coretemp")
        self.assertIsNotNone(base)
        self.assertTrue((base / "temp1_label").exists())

    def test_finds_applesmc_via_device_name_file(self) -> None:
        fakefs.add_applesmc(self.sysfs, index=2)
        base = sensors.find_hwmon_by_name(self.sysfs, "applesmc")
        self.assertIsNotNone(base)
        self.assertTrue((base / "fan1_input").exists())

    def test_hwmon_dir_with_no_name_file_is_skipped_not_raised(self) -> None:
        # A hwmon entry with neither hwmonN/name nor hwmonN/device/name
        # (measured: real hwmon2 looks exactly like this before you know
        # it's applesmc's class entry). Discovery must skip it silently.
        fakefs.add_bare_hwmon_no_name(self.sysfs, index=2)
        fakefs.add_coretemp(self.sysfs, index=3)
        # Should not raise, and should still find coretemp past the bare entry.
        self.assertIsNone(sensors.find_hwmon_by_name(self.sysfs, "applesmc"))
        base = sensors.find_hwmon_by_name(self.sysfs, "coretemp")
        self.assertIsNotNone(base)

    def test_unknown_name_returns_none(self) -> None:
        fakefs.add_coretemp(self.sysfs, index=3)
        self.assertIsNone(sensors.find_hwmon_by_name(self.sysfs, "nvme"))

    def test_missing_hwmon_class_dir_returns_none(self) -> None:
        self.assertIsNone(sensors.find_hwmon_by_name(self.sysfs, "coretemp"))


class FanTests(TempRootTestCase):
    def test_label_trailing_space_is_stripped(self) -> None:
        fakefs.add_applesmc(self.sysfs, index=2, fan_label="Right Side  ")
        fan = sensors.read_fan(self.sysfs, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(fan["label"], "Right Side")

    def test_fan_fields(self) -> None:
        fakefs.add_applesmc(
            self.sysfs, index=2, fan_rpm=1292, fan_min=1299, fan_max=6199, fan_manual=0
        )
        fan = sensors.read_fan(self.sysfs, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(fan["rpm"], 1292)
        self.assertEqual(fan["min_rpm"], 1299)
        self.assertEqual(fan["max_rpm"], 6199)
        self.assertIs(fan["manual"], False)

    def test_manual_true(self) -> None:
        # No mbpfan.pid in self.run -> "manual" (A14), never a real-host lookup.
        fakefs.add_applesmc(self.sysfs, index=2, fan_manual=1)
        fan = sensors.read_fan(self.sysfs, run_root=self.run, procfs_root=self.procfs)
        self.assertIs(fan["manual"], True)

    def test_target_rpm_from_fan1_output(self) -> None:
        fakefs.add_applesmc(self.sysfs, index=2, fan_manual=1, fan_output=2272)
        fan = sensors.read_fan(self.sysfs, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(fan["target_rpm"], 2272)

    def test_target_rpm_null_when_fan1_output_absent(self) -> None:
        fakefs.add_applesmc(self.sysfs, index=2, fan_manual=0)
        fan = sensors.read_fan(self.sysfs, run_root=self.run, procfs_root=self.procfs)
        self.assertIsNone(fan["target_rpm"])

    def test_no_applesmc_yields_all_none_not_exception(self) -> None:
        fan = sensors.read_fan(self.sysfs, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(
            fan,
            {
                "label": None,
                "rpm": None,
                "min_rpm": None,
                "max_rpm": None,
                "manual": None,
                "target_rpm": None,
                "control": None,
            },
        )


class FanControlTests(TempRootTestCase):
    """A14: `fan.control` -- "smc" if fan1_manual==0; "mbpfan" if
    fan1_manual==1 AND /run/mbpfan.pid names a LIVE process whose
    /proc/<pid>/comm is "mbpfan"; otherwise "manual"; null if manual itself
    is unreadable."""

    def test_manual_false_is_smc(self) -> None:
        control = sensors.read_fan_control(False, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(control, "smc")

    def test_manual_none_is_null(self) -> None:
        control = sensors.read_fan_control(None, run_root=self.run, procfs_root=self.procfs)
        self.assertIsNone(control)

    def test_manual_true_no_pidfile_is_manual(self) -> None:
        control = sensors.read_fan_control(True, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(control, "manual")

    def test_manual_true_with_live_mbpfan_is_mbpfan(self) -> None:
        fakefs.write_mbpfan_pid(self.run, pid=4242)
        fakefs.write_proc_comm(self.procfs, pid=4242, comm="mbpfan")
        control = sensors.read_fan_control(True, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(control, "mbpfan")

    def test_manual_true_with_stale_pidfile_wrong_comm_is_manual(self) -> None:
        # Positive control: the pid exists but belongs to something else
        # (a reused/stale pid) -- must NOT read as "mbpfan".
        fakefs.write_mbpfan_pid(self.run, pid=4242)
        fakefs.write_proc_comm(self.procfs, pid=4242, comm="bash")
        control = sensors.read_fan_control(True, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(control, "manual")

    def test_manual_true_with_pidfile_but_dead_process_is_manual(self) -> None:
        # pidfile exists but /proc/<pid>/comm doesn't (process is gone).
        fakefs.write_mbpfan_pid(self.run, pid=9999)
        control = sensors.read_fan_control(True, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(control, "manual")

    def test_manual_true_with_non_numeric_pidfile_is_manual(self) -> None:
        self.run.mkdir(parents=True, exist_ok=True)
        (self.run / "mbpfan.pid").write_text("not-a-pid\n")
        control = sensors.read_fan_control(True, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(control, "manual")

    def test_read_fan_wires_control_through(self) -> None:
        fakefs.add_applesmc(self.sysfs, index=2, fan_manual=1, fan_output=4000)
        fakefs.write_mbpfan_pid(self.run, pid=20237)
        fakefs.write_proc_comm(self.procfs, pid=20237, comm="mbpfan")
        fan = sensors.read_fan(self.sysfs, run_root=self.run, procfs_root=self.procfs)
        self.assertEqual(fan["control"], "mbpfan")
        self.assertEqual(fan["target_rpm"], 4000)


class BatteryTests(TempRootTestCase):
    def test_temp_is_deci_celsius(self) -> None:
        fakefs.add_power_supply(
            self.sysfs,
            "BAT0",
            {
                "capacity": "81",
                "status": "Discharging",
                "voltage_now": "11247000",
                "current_now": "1390000",
                "temp": "345",  # deci-C -> 34.5
                "cycle_count": "3",
                "charge_now": "5435000",
                "charge_full": "6710000",
                "charge_full_design": "6400000",
            },
        )
        battery = sensors.read_battery(self.sysfs)
        self.assertEqual(battery["temp_c"], 34.5)

    def test_health_pct_not_clamped_above_100(self) -> None:
        fakefs.add_power_supply(
            self.sysfs,
            "BAT0",
            {
                "status": "Full",
                "charge_full": "6710000",
                "charge_full_design": "6400000",
            },
        )
        battery = sensors.read_battery(self.sysfs)
        # 6710000 / 6400000 * 100 = 104.84375 -> measured 104.8-ish; must exceed 100, unclamped.
        self.assertGreater(battery["health_pct"], 100.0)
        self.assertAlmostEqual(battery["health_pct"], 104.84, places=2)

    def test_missing_battery_files_yield_none_not_exception(self) -> None:
        # BAT0 directory doesn't exist at all.
        battery = sensors.read_battery(self.sysfs)
        for key, value in battery.items():
            if key == "status":
                self.assertIsNone(value)
            else:
                self.assertIsNone(value, msg=f"{key} should be None")

    def test_current_now_is_unsigned_magnitude_regardless_of_status(self) -> None:
        # Driver reports current as an unsigned magnitude even while
        # discharging (measured on this machine) -- current_a must reflect
        # that raw magnitude, sign comes from power_w / status only.
        fakefs.add_power_supply(
            self.sysfs,
            "BAT0",
            {"status": "Discharging", "voltage_now": "11247000", "current_now": "1390000"},
        )
        battery = sensors.read_battery(self.sysfs)
        self.assertEqual(battery["current_a"], 1.39)
        self.assertLess(battery["power_w"], 0)


class ACTests(TempRootTestCase):
    def test_online_true(self) -> None:
        fakefs.add_power_supply(self.sysfs, "ADP1", {"online": "1"})
        self.assertEqual(sensors.read_ac(self.sysfs), {"online": True})

    def test_online_false(self) -> None:
        fakefs.add_power_supply(self.sysfs, "ADP1", {"online": "0"})
        self.assertEqual(sensors.read_ac(self.sysfs), {"online": False})

    def test_missing_adp1_yields_none(self) -> None:
        self.assertEqual(sensors.read_ac(self.sysfs), {"online": None})


class CpuTempTests(TempRootTestCase):
    def test_package_and_cores(self) -> None:
        fakefs.add_coretemp(
            self.sysfs, index=3, package_milli_c=59000, cores_milli_c={"Core 0": 58000, "Core 1": 59000}
        )
        package_c, cores_c = sensors.read_cpu_temps(self.sysfs)
        self.assertEqual(package_c, 59.0)
        self.assertEqual(cores_c, {"Core 0": 58.0, "Core 1": 59.0})

    def test_missing_coretemp_yields_none_and_empty(self) -> None:
        package_c, cores_c = sensors.read_cpu_temps(self.sysfs)
        self.assertIsNone(package_c)
        self.assertEqual(cores_c, {})


class LoadAvgTests(TempRootTestCase):
    def test_reads_three_floats(self) -> None:
        fakefs.write_loadavg(self.procfs, 0.82, 0.61, 0.55)
        self.assertEqual(sensors.read_loadavg(self.procfs), [0.82, 0.61, 0.55])

    def test_missing_file_yields_three_nulls(self) -> None:
        self.assertEqual(sensors.read_loadavg(self.procfs), [None, None, None])


class CpuUsageRateTests(TempRootTestCase):
    def test_first_sample_is_null(self) -> None:
        cur = sensors.CpuTimes(total=1000, idle=800)
        self.assertIsNone(sensors.cpu_usage_pct(None, cur))

    def test_usage_from_two_samples(self) -> None:
        prev = sensors.CpuTimes(total=1000, idle=800)
        cur = sensors.CpuTimes(total=1100, idle=850)
        # total delta 100, idle delta 50 -> 50% busy
        self.assertEqual(sensors.cpu_usage_pct(prev, cur), 50.0)

    def test_zero_or_negative_delta_is_null(self) -> None:
        same = sensors.CpuTimes(total=1000, idle=800)
        self.assertIsNone(sensors.cpu_usage_pct(same, same))


class MemTests(TempRootTestCase):
    def test_used_is_total_minus_available(self) -> None:
        fakefs.write_meminfo(
            self.procfs,
            {"MemTotal": 8_026_296, "MemAvailable": 3_126_692, "SwapTotal": 4_036_608, "SwapFree": 4_036_608},
        )
        mem = sensors.read_mem(self.procfs)
        self.assertEqual(mem["mem_total_bytes"], 8_026_296 * 1024)
        self.assertEqual(mem["mem_used_bytes"], (8_026_296 - 3_126_692) * 1024)
        self.assertEqual(mem["swap_used_bytes"], 0)

    def test_missing_meminfo_yields_all_none(self) -> None:
        mem = sensors.read_mem(self.procfs)
        self.assertEqual(
            mem,
            {
                "mem_used_bytes": None,
                "mem_total_bytes": None,
                "swap_used_bytes": None,
                "swap_total_bytes": None,
            },
        )


class DiskRateTests(TempRootTestCase):
    def test_first_sample_rate_is_null(self) -> None:
        fakefs.write_diskstats(self.procfs, "sda", sectors_read=1000, sectors_written=2000)
        raw = sensors.read_disk_raw(self.procfs, "sda")
        read_bps, write_bps = sensors.disk_rate(None, raw, dt=1.0)
        self.assertIsNone(read_bps)
        self.assertIsNone(write_bps)

    def test_rate_between_two_samples(self) -> None:
        prev = (1000, 2000)
        cur = (1080, 2080)  # +80 sectors each => 80*512 = 40960 bytes over 1s
        read_bps, write_bps = sensors.disk_rate(prev, cur, dt=1.0)
        self.assertEqual(read_bps, 40960.0)
        self.assertEqual(write_bps, 40960.0)

    def test_missing_device_returns_none(self) -> None:
        fakefs.write_diskstats(self.procfs, "sdb", sectors_read=1, sectors_written=1)
        self.assertIsNone(sensors.read_disk_raw(self.procfs, "sda"))


class NetTests(TempRootTestCase):
    def test_default_iface_from_route(self) -> None:
        fakefs.write_route(self.procfs, "wlp3s0")
        self.assertEqual(sensors.read_default_iface(self.procfs), "wlp3s0")

    def test_no_default_route_returns_none(self) -> None:
        fakefs.write_route(self.procfs, None)
        self.assertIsNone(sensors.read_default_iface(self.procfs))

    def test_netdev_raw_and_rate(self) -> None:
        fakefs.write_netdev(self.procfs, "wlp3s0", rx_bytes=1000, tx_bytes=2000)
        raw = sensors.read_netdev_raw(self.procfs, "wlp3s0")
        self.assertEqual(raw, (1000, 2000))
        rx_bps, tx_bps = sensors.net_rate(None, raw, dt=1.0)
        self.assertIsNone(rx_bps)
        self.assertIsNone(tx_bps)
        rx_bps, tx_bps = sensors.net_rate((0, 0), raw, dt=2.0)
        self.assertEqual(rx_bps, 500.0)
        self.assertEqual(tx_bps, 1000.0)


class CpuFreqTests(TempRootTestCase):
    def test_khz_converted_to_mhz(self) -> None:
        fakefs.add_cpu_freq(self.sysfs, 0, 1900000)
        self.assertEqual(sensors.read_core_freq_mhz(self.sysfs, 0), 1900.0)

    def test_missing_cpu_returns_none(self) -> None:
        self.assertIsNone(sensors.read_core_freq_mhz(self.sysfs, 7))


if __name__ == "__main__":
    unittest.main()
