"""A15/S3: cpu.throttle aggregation and the "recent" rolling window.

**S3 (advisor, measured):** naive summing across logical CPUs is wrong on
this machine -- every logical CPU carries a COPY of the one package
counter (measured: all four read the same value), and each physical core's
two sibling threads carry a copy of THAT core's counter (measured: cpu0,2
-> core 0; cpu1,3 -> core 1). So `package_count` is a MAX across CPUs, and
`core_count` is a SUM of per-core MAXes. All counters read 0 live on this
machine, so the only real test of the aggregation is a fixture with
unequal, non-zero values -- the positive control below is exactly the
spec's own worked example: package 3 on every CPU -> 3 (not 12); cores
2,2 / 1,1 -> 3 (not 6).
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hwmon import sensors

from . import fakefs


class ThrottleAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-throttle-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs = self._tmp / "sys"

    def test_package_count_is_max_not_sum(self) -> None:
        # S3's own worked example: package 3 on every one of 4 CPUs -> 3.
        for i in range(4):
            fakefs.add_cpu_throttle(self.sysfs, i, core_id=i // 2, package_throttle_count=3)
        result = sensors.read_cpu_throttle(self.sysfs)
        self.assertEqual(result["package_count"], 3)

    def test_naive_sum_would_have_given_12_positive_control(self) -> None:
        # Prove the aggregation is NOT a naive sum: 4 CPUs * 3 each would be
        # 12 under naive summing. If this ever regresses to a sum, this
        # assertion (not just the equality above) makes the failure obvious.
        for i in range(4):
            fakefs.add_cpu_throttle(self.sysfs, i, core_id=i // 2, package_throttle_count=3)
        result = sensors.read_cpu_throttle(self.sysfs)
        self.assertNotEqual(result["package_count"], 12)

    def test_core_count_sums_distinct_cores_max_of_siblings(self) -> None:
        # cpu0,2 -> core 0 (2,2 -> max 2); cpu1,3 -> core 1 (1,1 -> max 1).
        # Sum over cores = 2 + 1 = 3, not 6 (naive sum over 4 CPUs).
        fakefs.add_cpu_throttle(self.sysfs, 0, core_id=0, core_throttle_count=2)
        fakefs.add_cpu_throttle(self.sysfs, 1, core_id=1, core_throttle_count=1)
        fakefs.add_cpu_throttle(self.sysfs, 2, core_id=0, core_throttle_count=2)
        fakefs.add_cpu_throttle(self.sysfs, 3, core_id=1, core_throttle_count=1)
        result = sensors.read_cpu_throttle(self.sysfs)
        self.assertEqual(result["core_count"], 3)
        self.assertNotEqual(result["core_count"], 6)

    def test_measured_machine_shape_cpu02_core0_cpu13_core1(self) -> None:
        # This machine's real topology (measured 2026-09-23): cpu0,2 share
        # core_id 0; cpu1,3 share core_id 1. All-zero counts (live today).
        fakefs.add_cpu_throttle(self.sysfs, 0, core_id=0, core_throttle_count=0, package_throttle_count=0)
        fakefs.add_cpu_throttle(self.sysfs, 1, core_id=1, core_throttle_count=0, package_throttle_count=0)
        fakefs.add_cpu_throttle(self.sysfs, 2, core_id=0, core_throttle_count=0, package_throttle_count=0)
        fakefs.add_cpu_throttle(self.sysfs, 3, core_id=1, core_throttle_count=0, package_throttle_count=0)
        result = sensors.read_cpu_throttle(self.sysfs)
        self.assertEqual(result, {"core_count": 0, "package_count": 0})

    def test_no_cpu_dirs_yields_null_not_exception(self) -> None:
        result = sensors.read_cpu_throttle(self.sysfs)
        self.assertEqual(result, {"core_count": None, "package_count": None})

    def test_missing_throttle_files_on_one_cpu_does_not_poison_others(self) -> None:
        fakefs.add_cpu_throttle(self.sysfs, 0, core_id=0, core_throttle_count=5, package_throttle_count=1)
        # cpu1 exists (via a bare cpufreq dir) but has no throttle files at all.
        fakefs.add_cpu_freq(self.sysfs, 1, 1400000)
        result = sensors.read_cpu_throttle(self.sysfs)
        self.assertEqual(result["package_count"], 1)
        self.assertEqual(result["core_count"], 5)


class ThrottleRecentTests(unittest.TestCase):
    """S7: `recent` is null on the first (valid) sample; otherwise true iff
    either counter rose within the trailing 60 s."""

    def test_first_sample_is_null(self) -> None:
        recent, last_increase = sensors.compute_throttle_recent(
            None, None, None, cur_core_count=0, cur_package_count=0, now=1000.0
        )
        self.assertIsNone(recent)
        self.assertIsNone(last_increase)

    def test_unreadable_current_sample_is_null(self) -> None:
        recent, _ = sensors.compute_throttle_recent(
            0, 0, None, cur_core_count=None, cur_package_count=None, now=1000.0
        )
        self.assertIsNone(recent)

    def test_no_change_is_not_recent(self) -> None:
        recent, last_increase = sensors.compute_throttle_recent(
            0, 0, None, cur_core_count=0, cur_package_count=0, now=1000.0
        )
        self.assertIs(recent, False)
        self.assertIsNone(last_increase)

    def test_core_count_rising_is_recent(self) -> None:
        recent, last_increase = sensors.compute_throttle_recent(
            0, 0, None, cur_core_count=1, cur_package_count=0, now=1000.0
        )
        self.assertIs(recent, True)
        self.assertEqual(last_increase, 1000.0)

    def test_package_count_rising_is_recent(self) -> None:
        recent, last_increase = sensors.compute_throttle_recent(
            0, 0, None, cur_core_count=0, cur_package_count=1, now=1000.0
        )
        self.assertIs(recent, True)
        self.assertEqual(last_increase, 1000.0)

    def test_positive_control_naive_equality_check_would_miss_an_either_or_rise(self) -> None:
        # If the implementation only checked core_count (ignoring
        # package_count), this would wrongly report False.
        recent, _ = sensors.compute_throttle_recent(
            5, 2, None, cur_core_count=5, cur_package_count=3, now=1000.0
        )
        self.assertIs(recent, True)

    def test_stays_recent_within_60s_window(self) -> None:
        recent1, last_increase = sensors.compute_throttle_recent(
            0, 0, None, cur_core_count=1, cur_package_count=0, now=1000.0
        )
        self.assertIs(recent1, True)
        # 59s later, no further increase -- still "recent" (within window).
        recent2, last_increase2 = sensors.compute_throttle_recent(
            1, 0, last_increase, cur_core_count=1, cur_package_count=0, now=1059.0
        )
        self.assertIs(recent2, True)
        self.assertEqual(last_increase2, 1000.0)

    def test_false_61s_after_the_last_increase(self) -> None:
        _, last_increase = sensors.compute_throttle_recent(
            0, 0, None, cur_core_count=1, cur_package_count=0, now=1000.0
        )
        recent, _ = sensors.compute_throttle_recent(
            1, 0, last_increase, cur_core_count=1, cur_package_count=0, now=1061.0
        )
        self.assertIs(recent, False)


if __name__ == "__main__":
    unittest.main()
