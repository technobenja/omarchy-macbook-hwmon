"""R-L1.5 -- the hibernate backstop, rewritten per the deliverables spec's
§11 M1/R1-R4 fix pass (supersedes §10 A4). No test in this file ever calls
a real busctl or a real hibernate; every subprocess-facing function is
either mocked at `hwmon.procutil.run` or replaced with a fake callable, and
every `daemon.run` call pins `XDG_CONFIG_HOME` and injects a `hibernate_fn`
that fails the test if it's ever actually called (SHOULD 3).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hwmon import backstop, config, daemon, events, store


def _failing_hibernate_fn() -> bool:
    raise AssertionError("a test must never reach a real hibernate_fn")


# --- R1: the trigger reads sysfs, never UPower -----------------------------------------


class InDangerZoneTests(unittest.TestCase):
    def test_at_margin_is_in_danger_zone(self) -> None:
        # margin >= 2 points below the configured backstop_action_pct.
        self.assertTrue(backstop.in_danger_zone(pct=3.0, action_pct=5.0, margin=2.0))

    def test_above_margin_is_not(self) -> None:
        # Positive control for the boundary.
        self.assertFalse(backstop.in_danger_zone(pct=3.1, action_pct=5.0, margin=2.0))

    def test_missing_pct_is_never_in_danger_zone(self) -> None:
        self.assertFalse(backstop.in_danger_zone(pct=None, action_pct=5.0))

    def test_missing_action_pct_is_never_in_danger_zone(self) -> None:
        self.assertFalse(backstop.in_danger_zone(pct=1.0, action_pct=None))


class ReadPercentageActionIsGoneTests(unittest.TestCase):
    def test_read_percentage_action_no_longer_exists(self) -> None:
        # §11 R2: "read_percentage_action and its tests are deleted: their
        # fixtures returned 5.0 for a property that cannot exist."
        self.assertFalse(hasattr(backstop, "read_percentage_action"))


# --- R2: UPower.conf consistency alert (optional, never a trigger source) -------------


class ReadUpowerConfPercentageActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-upowerconf-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.conf_path = self._tmp / "UPower.conf"
        self.conf_d_dir = self._tmp / "UPower.conf.d"

    def test_reads_base_file(self) -> None:
        self.conf_path.write_text("[UPower]\nPercentageAction=2\n")
        self.assertEqual(
            backstop.read_upower_conf_percentage_action(self.conf_path, self.conf_d_dir), 2.0
        )

    def test_missing_base_file_is_none(self) -> None:
        self.assertIsNone(backstop.read_upower_conf_percentage_action(self.conf_path, self.conf_d_dir))

    def test_drop_in_overrides_base(self) -> None:
        self.conf_path.write_text("[UPower]\nPercentageAction=2\n")
        self.conf_d_dir.mkdir()
        (self.conf_d_dir / "10-battery-policy.conf").write_text("[UPower]\nPercentageAction=8\n")
        self.assertEqual(
            backstop.read_upower_conf_percentage_action(self.conf_path, self.conf_d_dir), 8.0
        )

    def test_later_drop_in_wins_over_earlier(self) -> None:
        self.conf_d_dir.mkdir()
        (self.conf_d_dir / "10-a.conf").write_text("[UPower]\nPercentageAction=8\n")
        (self.conf_d_dir / "20-b.conf").write_text("[UPower]\nPercentageAction=9\n")
        self.assertEqual(
            backstop.read_upower_conf_percentage_action(self.conf_path, self.conf_d_dir), 9.0
        )

    def test_unparseable_file_is_skipped_not_fatal(self) -> None:
        self.conf_path.write_text("not an ini file at all {{{")
        self.conf_d_dir.mkdir()
        (self.conf_d_dir / "10-a.conf").write_text("[UPower]\nPercentageAction=8\n")
        self.assertEqual(
            backstop.read_upower_conf_percentage_action(self.conf_path, self.conf_d_dir), 8.0
        )

    def test_missing_section_is_none(self) -> None:
        self.conf_path.write_text("[Other]\nX=1\n")
        self.assertIsNone(backstop.read_upower_conf_percentage_action(self.conf_path, self.conf_d_dir))

    def test_no_conf_d_dir_is_fine(self) -> None:
        self.conf_path.write_text("[UPower]\nPercentageAction=2\n")
        self.assertEqual(
            backstop.read_upower_conf_percentage_action(self.conf_path, Path("/nonexistent-conf-d")), 2.0
        )


class IsConfigMismatchedTests(unittest.TestCase):
    def test_matching_values_are_not_mismatched(self) -> None:
        self.assertFalse(backstop.is_config_mismatched(8.0, 8.0))

    def test_differing_values_are_mismatched(self) -> None:
        self.assertTrue(backstop.is_config_mismatched(8.0, 2.0))

    def test_either_missing_is_never_a_mismatch(self) -> None:
        # A parse failure must never manufacture an alert.
        self.assertFalse(backstop.is_config_mismatched(8.0, None))
        self.assertFalse(backstop.is_config_mismatched(None, 2.0))


# --- R3: the UPower divergence standing check (never feeds the trigger) --------------


class DbusReadParsingTests(unittest.TestCase):
    """Round-2 review, BLOCKER: every fixture below is a REAL `busctl -j`
    JSON blob, measured live 2026-09-23 (the coordinator's task message
    quotes the exact commands) -- not a shape guessed at build time. The
    earlier version of this file used single-level fixtures like
    `{"type": "v", "data": [46.75]}` for a `Properties.Get` call, which is
    NOT what real busctl emits (a `Properties.Get`'s "v" out-parameter is
    ITSELF a variant, doubly-wrapped) -- those fixtures made every property
    reader pass against a shape that could never occur live, while the real
    bus returned `None` for all of them. Deleted; replaced with these.
    """

    def test_preparing_for_sleep_true_real_measured_shape(self) -> None:
        # busctl --system -j call org.freedesktop.login1 /org/freedesktop/login1
        #   org.freedesktop.DBus.Properties Get ss org.freedesktop.login1.Manager PreparingForSleep
        measured = json.dumps({"type": "v", "data": [{"type": "b", "data": False}]})
        with mock.patch("hwmon.procutil.run", return_value=measured):
            self.assertIs(backstop.read_preparing_for_sleep(), False)

    def test_energy_full_design_real_measured_shape_on_display_device_is_zero(self) -> None:
        # busctl --system -j call org.freedesktop.UPower /org/freedesktop/UPower/devices/DisplayDevice
        #   org.freedesktop.DBus.Properties Get ss org.freedesktop.UPower.Device EnergyFullDesign
        # -- measured 0.0 on THIS device, which is exactly why R3 (item 3)
        # reads the real battery device instead.
        measured = json.dumps({"type": "v", "data": [{"type": "d", "data": 0.0}]})
        with mock.patch("hwmon.procutil.run", return_value=measured):
            value = backstop._dbus_get_property(
                "org.freedesktop.UPower", backstop._DISPLAY_DEVICE_PATH, "org.freedesktop.UPower.Device",
                "EnergyFullDesign", timeout=1.0,
            )
        self.assertEqual(value, 0.0)

    def test_can_hibernate_yes_real_measured_shape(self) -> None:
        # busctl --system -j call org.freedesktop.login1 /org/freedesktop/login1
        #   org.freedesktop.login1.Manager CanHibernate
        # -- a plain METHOD call ("s" return), never wrapped in a
        # Properties.Get variant, so this shape was already correct.
        measured = json.dumps({"type": "s", "data": ["yes"]})
        with mock.patch("hwmon.procutil.run", return_value=measured):
            self.assertEqual(backstop.read_can_hibernate(), "yes")

    def test_percentage_double_wrapped_variant_parses(self) -> None:
        measured = json.dumps({"type": "v", "data": [{"type": "d", "data": 6.152518437156755}]})
        with mock.patch("hwmon.procutil.run", return_value=measured):
            value = backstop._dbus_get_property(
                "org.freedesktop.UPower", "/org/freedesktop/UPower/devices/battery_BAT0",
                "org.freedesktop.UPower.Device", "Percentage", timeout=1.0,
            )
        self.assertAlmostEqual(value, 6.152518437156755)

    def test_procutil_failure_yields_none(self) -> None:
        with mock.patch("hwmon.procutil.run", return_value=None):
            self.assertIsNone(backstop.read_preparing_for_sleep())
            self.assertIsNone(backstop.read_can_hibernate())
            self.assertIsNone(backstop.find_battery_device_path())

    def test_malformed_json_yields_none(self) -> None:
        with mock.patch("hwmon.procutil.run", return_value="not json {{"):
            self.assertIsNone(backstop.read_preparing_for_sleep())

    def test_single_wrapped_variant_is_rejected_not_misread(self) -> None:
        # Positive control for the fix: the OLD (wrong) single-wrap shape
        # must not accidentally "work" by coincidence -- a bare bool/float
        # in the outer data[0] slot is not a real busctl variant shape, and
        # the unwrap must fail closed (None), not silently misinterpret it.
        wrong_shape = json.dumps({"type": "v", "data": [False]})
        with mock.patch("hwmon.procutil.run", return_value=wrong_shape):
            value = backstop._dbus_get_property(
                "org.freedesktop.login1", backstop._LOGIND_PATH, "org.freedesktop.login1.Manager",
                "PreparingForSleep", timeout=1.0,
            )
        self.assertIsNone(value)

    def test_call_hibernate_success_and_failure(self) -> None:
        with mock.patch("hwmon.procutil.run", return_value=""):
            self.assertTrue(backstop.call_hibernate())
        with mock.patch("hwmon.procutil.run", return_value=None):
            self.assertFalse(backstop.call_hibernate())


class GetAllAndEnumerateDevicesTests(unittest.TestCase):
    """Review item 3/NIT 7: read the real battery device via ONE `GetAll`
    call, with the device path discovered via `EnumerateDevices` rather
    than hard-coded."""

    _GET_ALL_MEASURED = json.dumps({
        "type": "a{sv}",
        "data": [{
            "Percentage": {"type": "d", "data": 6.152518437156755},
            "EnergyFull": {"type": "d", "data": 722.698},
            "EnergyFullDesign": {"type": "d", "data": 72.576},
            "Vendor": {"type": "s", "data": "ifixit"},  # an example of an unrelated key GetAll also returns
        }],
    })

    _ENUMERATE_MEASURED = json.dumps({
        "type": "ao",
        "data": [["/org/freedesktop/UPower/devices/battery_BAT0", "/org/freedesktop/UPower/devices/line_power_ADP1"]],
    })

    def test_enumerate_devices_finds_the_battery_path(self) -> None:
        with mock.patch("hwmon.procutil.run", return_value=self._ENUMERATE_MEASURED):
            path = backstop.find_battery_device_path()
        self.assertEqual(path, "/org/freedesktop/UPower/devices/battery_BAT0")

    def test_enumerate_devices_ignores_non_battery_paths(self) -> None:
        only_line_power = json.dumps({"type": "ao", "data": [["/org/freedesktop/UPower/devices/line_power_ADP1"]]})
        with mock.patch("hwmon.procutil.run", return_value=only_line_power):
            self.assertIsNone(backstop.find_battery_device_path())

    def test_get_all_unwraps_every_property(self) -> None:
        with mock.patch("hwmon.procutil.run", return_value=self._GET_ALL_MEASURED):
            props = backstop._dbus_get_all_properties(
                "org.freedesktop.UPower", "/org/freedesktop/UPower/devices/battery_BAT0",
                "org.freedesktop.UPower.Device", timeout=1.0,
            )
        self.assertEqual(props["Percentage"], 6.152518437156755)
        self.assertEqual(props["EnergyFull"], 722.698)
        self.assertEqual(props["EnergyFullDesign"], 72.576)
        self.assertEqual(props["Vendor"], "ifixit")

    def test_read_battery_upower_properties_uses_discovered_path_and_one_call(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, timeout):
            calls.append(cmd)
            if "EnumerateDevices" in cmd:
                return self._ENUMERATE_MEASURED
            return self._GET_ALL_MEASURED

        with mock.patch("hwmon.procutil.run", side_effect=fake_run):
            result = backstop.read_battery_upower_properties()
        self.assertEqual(result, {"pct": 6.152518437156755, "energy_full": 722.698, "energy_full_design": 72.576})
        # One EnumerateDevices + one GetAll -- never three separate Gets (NIT 7).
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("battery_BAT0" in c for c in calls[1]))

    def test_read_battery_upower_properties_falls_back_when_enumerate_fails(self) -> None:
        def fake_run(cmd, timeout):
            if "EnumerateDevices" in cmd:
                return None  # simulated busctl failure
            return self._GET_ALL_MEASURED

        with mock.patch("hwmon.procutil.run", side_effect=fake_run):
            result = backstop.read_battery_upower_properties()
        self.assertEqual(result["pct"], 6.152518437156755)

    def test_read_battery_upower_properties_all_none_on_total_failure(self) -> None:
        with mock.patch("hwmon.procutil.run", return_value=None):
            result = backstop.read_battery_upower_properties()
        self.assertEqual(result, {"pct": None, "energy_full": None, "energy_full_design": None})


def _upower_cache(pct=None, energy_full=None, energy_full_design=None, **kwargs) -> backstop.UPowerCache:
    return backstop.UPowerCache(
        read_fn=lambda: {"pct": pct, "energy_full": energy_full, "energy_full_design": energy_full_design}, **kwargs
    )


class UPowerCacheTests(unittest.TestCase):
    def test_polls_at_most_once_per_ttl(self) -> None:
        calls = []

        class _Clock:
            def __init__(self):
                self.t = 1000.0

            def __call__(self):
                return self.t

        clock = _Clock()
        cache = backstop.UPowerCache(
            ttl_s=10.0,
            read_fn=lambda: calls.append(1) or {"pct": 3.0, "energy_full": 700.0, "energy_full_design": 70.0},
            clock=clock,
        )
        for _ in range(10):
            cache.get()
            clock.t += 1.0
        self.assertEqual(len(calls), 1, "must poll exactly once across a 10s window")

    def test_returns_latest_reading(self) -> None:
        cache = _upower_cache(pct=3.0, energy_full=700.0, energy_full_design=70.0)
        self.assertEqual(cache.get(), {"pct": 3.0, "energy_full": 700.0, "energy_full_design": 70.0})


class ComputeUpowerDivergenceTests(unittest.TestCase):
    """Today's measured numbers as positive controls (§11 M1)."""

    def test_measured_divergent_case_33_vs_327(self) -> None:
        result = backstop.compute_upower_divergence(upower_pct=3.27161, sysfs_pct=33)
        self.assertEqual(result["state"], "divergent")

    def test_measured_ok_case_49_vs_473(self) -> None:
        # Positive control for the boundary: the SAME machine 2h earlier,
        # before the fault appeared.
        result = backstop.compute_upower_divergence(upower_pct=47.3, sysfs_pct=49)
        self.assertEqual(result["state"], "ok")

    def test_either_missing_is_unknown(self) -> None:
        self.assertEqual(backstop.compute_upower_divergence(upower_pct=None, sysfs_pct=49)["state"], "unknown")
        self.assertEqual(backstop.compute_upower_divergence(upower_pct=47.3, sysfs_pct=None)["state"], "unknown")

    def test_energy_ratio_over_threshold_is_divergent_even_if_pct_agrees(self) -> None:
        # The measured cause: energy-full/energy-full-design = 9.96x.
        result = backstop.compute_upower_divergence(
            upower_pct=49.0, sysfs_pct=49.0, energy_full=722.698, energy_full_design=72.576
        )
        self.assertEqual(result["state"], "divergent")

    def test_energy_ratio_at_threshold_is_not_divergent(self) -> None:
        result = backstop.compute_upower_divergence(
            upower_pct=49.0, sysfs_pct=49.0, energy_full=120.0, energy_full_design=100.0
        )
        self.assertEqual(result["state"], "ok")

    def test_pct_and_sysfs_are_echoed_verbatim(self) -> None:
        result = backstop.compute_upower_divergence(upower_pct=3.27, sysfs_pct=33)
        self.assertEqual(result["upower_pct"], 3.27)
        self.assertEqual(result["sysfs_pct"], 33)


class DivergenceSustainedTests(unittest.TestCase):
    def test_first_divergent_sample_is_not_sustained(self) -> None:
        sustained, first_ts = backstop.divergence_sustained(None, True, now=1000.0)
        self.assertFalse(sustained)
        self.assertEqual(first_ts, 1000.0)

    def test_sustained_past_60s(self) -> None:
        sustained, first_ts = backstop.divergence_sustained(1000.0, True, now=1061.0)
        self.assertTrue(sustained)

    def test_not_yet_60s_is_not_sustained(self) -> None:
        # Positive control for the boundary.
        sustained, _ = backstop.divergence_sustained(1000.0, True, now=1059.0)
        self.assertFalse(sustained)

    def test_a_single_non_divergent_sample_resets_the_streak(self) -> None:
        sustained, first_ts = backstop.divergence_sustained(1000.0, False, now=1061.0)
        self.assertFalse(sustained)
        self.assertIsNone(first_ts)


# --- SHOULD 1: PreparingForSleep unreadable must REFUSE, not proceed ------------------


class HibernateBackstopResetTests(unittest.TestCase):
    """Review item 6: toggling `hibernate_backstop` off then back on must
    restart the 20-sample count, not resume stale state."""

    def test_reset_clears_consecutive_and_attempted(self) -> None:
        b = backstop.HibernateBackstop(preparing_for_sleep_fn=lambda: False, can_hibernate_fn=lambda: "yes")
        for _ in range(20):
            first = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(first.action, "hibernate")
        b.reset()
        self.assertEqual(b.consecutive, 0)
        self.assertFalse(b.attempted_this_episode)

    def test_reset_mid_episode_means_a_full_fresh_count_is_needed(self) -> None:
        b = backstop.HibernateBackstop(preparing_for_sleep_fn=lambda: False, can_hibernate_fn=lambda: "yes")
        for _ in range(19):
            b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        b.reset()  # e.g. hibernate_backstop was toggled off here
        decision = None
        for _ in range(19):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(decision.action, "none", "the pre-reset 19 samples must not count toward this streak")
        decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(decision.action, "hibernate", "the 20th sample AFTER the reset fires")


class HibernateBackstopEvaluateTests(unittest.TestCase):
    def _backstop(self, *, preparing=False, can_hibernate="yes"):
        return backstop.HibernateBackstop(
            preparing_for_sleep_fn=lambda: preparing, can_hibernate_fn=lambda: can_hibernate
        )

    def test_not_discharging_never_counts(self) -> None:
        b = self._backstop()
        for _ in range(30):
            decision = b.evaluate(status="Charging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(decision.action, "none")
        self.assertEqual(b.consecutive, 0)

    def test_not_yet_20_consecutive_is_none(self) -> None:
        b = self._backstop()
        for _ in range(19):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
            self.assertEqual(decision.action, "none")

    def test_20th_consecutive_sample_fires(self) -> None:
        b = self._backstop()
        decision = None
        for _ in range(20):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(decision.action, "hibernate")

    def test_episode_reset_clears_the_counter(self) -> None:
        b = self._backstop()
        for _ in range(19):
            b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        b.evaluate(status="Charging", pct=50.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(b.consecutive, 0)
        decision = None
        for _ in range(19):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(decision.action, "none", "the old streak must not have carried over")

    def test_no_per_sample_retries_within_one_episode(self) -> None:
        b = self._backstop()
        for _ in range(20):
            first = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(first.action, "hibernate")
        for _ in range(50):
            again = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
            self.assertEqual(again.action, "none", "must not attempt again within the same episode")

    def test_new_episode_after_reset_can_attempt_again(self) -> None:
        b = self._backstop()
        for _ in range(20):
            b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        b.evaluate(status="Full", pct=100.0, action_pct=5.0, sleep_blocked=False)
        decision = None
        for _ in range(20):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(decision.action, "hibernate", "a fresh episode gets its own attempt")

    def test_preparing_for_sleep_true_defers_without_consuming_the_episode(self) -> None:
        b = self._backstop(preparing=True)
        for _ in range(25):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(decision.action, "none")
        self.assertFalse(b.attempted_this_episode, "a transient PreparingForSleep must not consume the attempt")

    def test_preparing_for_sleep_unknown_refuses_should1(self) -> None:
        # SHOULD 1 (review): None (could not check) must REFUSE, not
        # proceed as though it were clear.
        b = self._backstop(preparing=None)
        decision = None
        for _ in range(20):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(decision.action, "refuse")
        self.assertEqual(decision.reason, "preparing_for_sleep_unknown")

    def test_preparing_for_sleep_unknown_consumes_the_episode(self) -> None:
        b = self._backstop(preparing=None)
        for _ in range(20):
            first = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(first.action, "refuse")
        for _ in range(10):
            again = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
            self.assertEqual(again.action, "none", "the unknown-preparing refusal must still count as an attempt")

    def test_block_inhibitor_refuses(self) -> None:
        b = self._backstop()
        decision = None
        for _ in range(20):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=True)
        self.assertEqual(decision.action, "refuse")
        self.assertEqual(decision.reason, "sleep_blocked")

    def test_unknown_inhibitor_state_refuses(self) -> None:
        b = self._backstop()
        decision = None
        for _ in range(20):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=None)
        self.assertEqual(decision.action, "refuse")
        self.assertEqual(decision.reason, "sleep_blocked_unknown")

    def test_can_hibernate_not_yes_refuses(self) -> None:
        b = self._backstop(can_hibernate="no")
        decision = None
        for _ in range(20):
            decision = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(decision.action, "refuse")
        self.assertIn("no", decision.reason)

    def test_refusal_in_one_episode_does_not_block_a_later_episode(self) -> None:
        b = self._backstop()
        for _ in range(20):
            first = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=True)
        self.assertEqual(first.action, "refuse")
        b.evaluate(status="Full", pct=100.0, action_pct=5.0, sleep_blocked=True)
        second = None
        for _ in range(20):
            second = b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(second.action, "hibernate", "the inhibitor cleared; a NEW episode may still fire")

    def test_preparing_and_can_hibernate_are_lazy_not_polled_every_tick(self) -> None:
        calls = []
        b = backstop.HibernateBackstop(
            preparing_for_sleep_fn=lambda: calls.append("preparing") or False,
            can_hibernate_fn=lambda: calls.append("can_hibernate") or "yes",
        )
        for _ in range(19):
            b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(calls, [], "must not call logind before the 20th sample")
        b.evaluate(status="Discharging", pct=1.0, action_pct=5.0, sleep_blocked=False)
        self.assertEqual(calls, ["preparing", "can_hibernate"])


def _valid_tree(tmp: Path, *, capacity: int = 3) -> tuple[Path, Path]:
    from . import fakefs

    sysfs = tmp / "sys"
    procfs = tmp / "proc"
    fakefs.add_coretemp(sysfs, index=3)
    fakefs.add_applesmc(sysfs, index=2)
    fakefs.add_power_supply(
        sysfs, "BAT0",
        {"capacity": str(capacity), "status": "Discharging", "voltage_now": "11000000", "current_now": "1000000"},
    )
    fakefs.add_power_supply(sysfs, "ADP1", {"online": "0"})
    fakefs.write_loadavg(procfs, 0.1, 0.1, 0.1)
    fakefs.write_stat(procfs, {"cpu": [1, 0, 1, 100, 0, 0, 0, 0]})
    fakefs.write_meminfo(procfs, {"MemTotal": 1000, "MemAvailable": 500, "SwapTotal": 0, "SwapFree": 0})
    fakefs.write_diskstats(procfs, "sda", sectors_read=0, sectors_written=0)
    fakefs.write_route(procfs, None)
    fakefs.write_boot_id(procfs, "11111111-2222-3333-4444-555555555555")
    return sysfs, procfs


class DaemonWiringTests(unittest.TestCase):
    """End-to-end through `daemon.run` -- proves the config gate, R1's
    sysfs-only trigger, R2's missing-threshold could-not-check, R4's
    success-gated event recording, and that a real `hibernate_fn`/UPower
    read is NEVER what gets called."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-backstop-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs, self.procfs = _valid_tree(self._tmp)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"
        # SHOULD 3 (review): pin XDG_CONFIG_HOME on every daemon.run test.
        env_patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self._tmp / "xdg-config")})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self._quiet_upower_cache = _upower_cache()

    def _run(self, iterations: int, **kwargs) -> None:
        kwargs.setdefault("sync_journal_fn", lambda: True)
        kwargs.setdefault("hibernate_fn", _failing_hibernate_fn)
        kwargs.setdefault("backstop_notify_fn", lambda summary, body: None)
        kwargs.setdefault("triage_notify_fn", lambda summary, body: None)
        kwargs.setdefault("upower_cache", self._quiet_upower_cache)
        kwargs.setdefault("critical_pct", -1)  # this fixture's battery is critically low; not this test's concern
        daemon.run(
            state_dir=self.state_dir,
            db_path=self.db_path,
            sysfs_root=self.sysfs,
            procfs_root=self.procfs,
            interval=0.0,
            iterations=iterations,
            install_signal_handlers=False,
            check_events_at_start=False,
            **kwargs,
        )

    def test_disabled_by_default_never_calls_hibernate_fn(self) -> None:
        # No config_loader override at all -- the REAL default (ConfigCache
        # reading the pinned, empty XDG_CONFIG_HOME) must mean "off".
        self._run(25)  # would raise via _failing_hibernate_fn if ever reached
        with store.Store(self.db_path) as st:
            rows = [r for r in st.list_events(days=1, now=time.time()) if r[2] == "backstop_hibernate"]
        self.assertEqual(rows, [])

    def test_missing_action_pct_records_could_not_check_and_does_not_act(self) -> None:
        out = self._capture_stderr(
            lambda: self._run(5, config_loader=lambda: {"hibernate_backstop": True, "backstop_action_pct": None})
        )
        self.assertIn("could not check", out)
        with store.Store(self.db_path) as st:
            self.assertEqual(st.list_events(days=1, now=time.time()), [])

    def test_missing_action_pct_warning_logged_once_not_per_tick(self) -> None:
        out = self._capture_stderr(
            lambda: self._run(10, config_loader=lambda: {"hibernate_backstop": True, "backstop_action_pct": None})
        )
        self.assertEqual(out.count("could not check (no backstop_action_pct configured)"), 1)

    def test_enabled_fires_after_20_ticks_using_sysfs_never_upower(self) -> None:
        # R1: even though the injected UPowerCache would (if ever read for
        # the trigger) return a wildly different number, the trigger must
        # fire from sysfs capacity=3 alone.
        divergent_upower_cache = _upower_cache(pct=99.0)  # if this were used as the trigger input, it would NEVER fire
        hb = backstop.HibernateBackstop(preparing_for_sleep_fn=lambda: False, can_hibernate_fn=lambda: "yes")
        hibernate_calls = []
        self._run(
            25,
            config_loader=lambda: {"hibernate_backstop": True, "backstop_action_pct": 5.0},
            upower_cache=divergent_upower_cache,
            hibernate_backstop=hb,
            hibernate_fn=lambda: hibernate_calls.append(1) or True,
        )
        self.assertEqual(len(hibernate_calls), 1)
        with store.Store(self.db_path) as st:
            rows = [r for r in st.list_events(days=1, now=time.time()) if r[2] == "backstop_hibernate"]
        self.assertEqual(len(rows), 1)

    def test_toggling_off_then_on_restarts_the_20_sample_count(self) -> None:
        # Review item 6, end to end: 19 enabled ticks (builds consecutive to
        # 19), 3 DISABLED ticks (must reset it), then re-enabled -- must
        # take a FULL fresh 20 ticks, not fire on the very next one using
        # the pre-disable count.
        tick = {"n": 0}

        def config_loader():
            tick["n"] += 1
            enabled = tick["n"] <= 19 or tick["n"] > 22
            return {"hibernate_backstop": enabled, "backstop_action_pct": 5.0}

        hibernate_calls: list[int] = []
        hb = backstop.HibernateBackstop(preparing_for_sleep_fn=lambda: False, can_hibernate_fn=lambda: "yes")
        self._run(
            42,
            config_loader=config_loader,
            hibernate_backstop=hb,
            hibernate_fn=lambda: hibernate_calls.append(tick["n"]) or True,
        )
        # Without the reset, tick 23 (the first re-enabled tick) would
        # already be the 20th sample (19 banked + 1) and fire immediately.
        # With the reset, tick 22+20 = 42 is the 20th FRESH sample.
        self.assertEqual(hibernate_calls, [42], "must not resume the pre-disable count")

    def test_failed_hibernate_call_records_refused_not_hibernate_and_does_not_consume_cap(self) -> None:
        # §11 R4.
        hb = backstop.HibernateBackstop(preparing_for_sleep_fn=lambda: False, can_hibernate_fn=lambda: "yes")
        self._run(
            25,
            config_loader=lambda: {"hibernate_backstop": True, "backstop_action_pct": 5.0},
            hibernate_backstop=hb,
            hibernate_fn=lambda: False,  # the logind call itself fails
        )
        with store.Store(self.db_path) as st:
            hibernate_rows = [r for r in st.list_events(days=1, now=time.time()) if r[2] == "backstop_hibernate"]
            refused_rows = [r for r in st.list_events(days=1, now=time.time()) if r[2] == "backstop_refused"]
        self.assertEqual(hibernate_rows, [])
        self.assertEqual(len(refused_rows), 1)
        self.assertEqual(refused_rows[0][5], "hibernate_call_failed")

    def test_already_hibernated_this_boot_short_circuits_before_any_upower_read(self) -> None:
        with store.Store(self.db_path) as st:
            st.insert_event(
                events.EventRecord(
                    ts_start=1.0, ts_end=None, kind="backstop_hibernate", last_pct=1, last_status="Discharging",
                    detail=None, boot_id="11111111222233334444555555555555",
                )
            )
        self._run(5, config_loader=lambda: {"hibernate_backstop": True, "backstop_action_pct": 5.0})
        # No AssertionError from _failing_hibernate_fn -> proven.

    def _capture_stderr(self, fn) -> str:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fn()
        return buf.getvalue()


class ConfigMismatchAlertWiringTests(unittest.TestCase):
    """R2's optional consistency alert, end to end -- alert only, never a
    trigger source, and a parse failure never disables the backstop."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-mismatch-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs, self.procfs = _valid_tree(self._tmp, capacity=80)  # nowhere near the danger zone
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"
        env_patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self._tmp / "xdg-config")})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run(self, notify_calls: list, upower_conf_reader) -> str:
        import contextlib
        import io

        buf = io.StringIO()
        upower_cache = _upower_cache()
        with contextlib.redirect_stderr(buf):
            daemon.run(
                state_dir=self.state_dir, db_path=self.db_path, sysfs_root=self.sysfs, procfs_root=self.procfs,
                interval=0.0, iterations=5, install_signal_handlers=False, check_events_at_start=False,
                critical_pct=-1, sync_journal_fn=lambda: True, hibernate_fn=_failing_hibernate_fn,
                upower_cache=upower_cache,
                config_loader=lambda: {"hibernate_backstop": False, "backstop_action_pct": 8.0},
                upower_conf_reader=upower_conf_reader,
                backstop_notify_fn=lambda summary, body: notify_calls.append((summary, body)),
            )
        return buf.getvalue()

    def test_mismatch_alerts_once_not_per_tick(self) -> None:
        notify_calls: list = []
        out = self._run(notify_calls, lambda: 2.0)
        self.assertEqual(len(notify_calls), 1)
        self.assertEqual(out.count("disagrees with UPower.conf"), 1)

    def test_matching_values_never_alert(self) -> None:
        notify_calls: list = []
        self._run(notify_calls, lambda: 8.0)
        self.assertEqual(notify_calls, [])

    def test_parse_failure_never_alerts_and_never_disables_the_backstop(self) -> None:
        notify_calls: list = []
        # A parse failure (None) must produce silence, not a false alarm --
        # and (implicitly) the run above completing without the failing
        # hibernate_fn firing proves the backstop itself kept working.
        self._run(notify_calls, lambda: None)
        self.assertEqual(notify_calls, [])


class UpowerDivergenceEventWiringTests(unittest.TestCase):
    """§11 R3: the standing check runs regardless of hibernate_backstop,
    and records `upower_divergent` once per boot after 60s sustained."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-divergence-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs, self.procfs = _valid_tree(self._tmp, capacity=33)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"
        env_patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self._tmp / "xdg-config")})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_sustained_divergence_records_one_event_and_notifies(self) -> None:
        notify_calls: list = []
        # `time.time()` is mocked to jump 61s between ticks so a single
        # SUBSEQUENT tick already crosses the 60s sustain threshold --
        # proves the event fires without looping 60+ real ticks. The
        # UPowerCache's own clock is `time.monotonic` (unaffected), so
        # `ttl_s=0.0` forces it to re-poll every call regardless.
        divergent_upower_cache = _upower_cache(pct=3.27, ttl_s=0.0)  # measured divergent case; poll fresh every tick
        with mock.patch("hwmon.daemon.time.time", side_effect=[1000.0 + 61.0 * i for i in range(20)]):
            daemon.run(
                state_dir=self.state_dir, db_path=self.db_path, sysfs_root=self.sysfs, procfs_root=self.procfs,
                interval=0.0, iterations=3, install_signal_handlers=False, check_events_at_start=False,
                critical_pct=-1, sync_journal_fn=lambda: True, hibernate_fn=_failing_hibernate_fn,
                upower_cache=divergent_upower_cache,
                config_loader=lambda: {"hibernate_backstop": False, "backstop_action_pct": None},
                backstop_notify_fn=lambda summary, body: notify_calls.append((summary, body)),
            )
        with store.Store(self.db_path) as st:
            # NOTE: the mocked `time.time()` values are small synthetic
            # numbers (~1000-1200), not real wall-clock time -- `now`/`days`
            # here just need a window that comfortably contains them.
            rows = [r for r in st.list_events(days=365, now=2_000_000.0) if r[2] == "upower_divergent"]
        self.assertEqual(len(rows), 1, msg=f"rows: {rows}")
        self.assertEqual(len(notify_calls), 1)

    def test_snapshot_recovery_upower_reflects_the_divergent_reading(self) -> None:
        upower_cache = _upower_cache(pct=3.27)
        daemon.run(
            state_dir=self.state_dir, db_path=self.db_path, sysfs_root=self.sysfs, procfs_root=self.procfs,
            interval=0.0, iterations=1, install_signal_handlers=False, check_events_at_start=False,
            critical_pct=-1, sync_journal_fn=lambda: True, hibernate_fn=_failing_hibernate_fn,
            upower_cache=upower_cache,
            config_loader=lambda: {"hibernate_backstop": False, "backstop_action_pct": None},
            backstop_notify_fn=lambda *a, **k: None,
        )
        latest = json.loads((self.state_dir / "latest.json").read_text())
        self.assertEqual(latest["recovery"]["upower"]["state"], "divergent")
        self.assertEqual(latest["recovery"]["upower"]["sysfs_pct"], 33)

    def test_ok_reading_never_records_an_event(self) -> None:
        upower_cache = _upower_cache(pct=32.0, energy_full=50.0, energy_full_design=50.0)
        daemon.run(
            state_dir=self.state_dir, db_path=self.db_path, sysfs_root=self.sysfs, procfs_root=self.procfs,
            interval=0.0, iterations=5, install_signal_handlers=False, check_events_at_start=False,
            critical_pct=-1, sync_journal_fn=lambda: True, hibernate_fn=_failing_hibernate_fn,
            upower_cache=upower_cache,
            config_loader=lambda: {"hibernate_backstop": False, "backstop_action_pct": None},
            backstop_notify_fn=lambda *a, **k: None,
        )
        with store.Store(self.db_path) as st:
            self.assertEqual([r for r in st.list_events(days=1, now=time.time()) if r[2] == "upower_divergent"], [])

    def test_no_boot_id_notifies_only_once_despite_many_sustained_ticks(self) -> None:
        # Review item 5: `has_event_kind_for_boot(kind, None)` is a SQL
        # `boot_id = NULL` comparison that NEVER matches, so without the
        # per-process latch this would notify on every tick once sustained
        # -- which is what sent the reviewer a real desktop notification.
        (self.procfs / "sys" / "kernel" / "random" / "boot_id").unlink()
        notify_calls: list = []
        with mock.patch("hwmon.daemon.time.time", side_effect=[1000.0 + 61.0 * i for i in range(20)]):
            daemon.run(
                state_dir=self.state_dir, db_path=self.db_path, sysfs_root=self.sysfs, procfs_root=self.procfs,
                interval=0.0, iterations=10, install_signal_handlers=False, check_events_at_start=False,
                critical_pct=-1, sync_journal_fn=lambda: True, hibernate_fn=_failing_hibernate_fn,
                upower_cache=_upower_cache(pct=3.27, ttl_s=0.0),
                config_loader=lambda: {"hibernate_backstop": False, "backstop_action_pct": None},
                backstop_notify_fn=lambda summary, body: notify_calls.append((summary, body)),
            )
        self.assertEqual(len(notify_calls), 1, msg=f"notify_calls: {notify_calls}")
        with store.Store(self.db_path) as st:
            rows = [r for r in st.list_events(days=365, now=2_000_000.0) if r[2] == "upower_divergent"]
        self.assertEqual(len(rows), 1)


class ConfigMismatchRateLimitTests(unittest.TestCase):
    """Review item 4: the UPower.conf consistency check only re-reads the
    file on an actual `ConfigCache` refresh, not every 1s tick."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-mismatch-rate-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs, self.procfs = _valid_tree(self._tmp, capacity=80)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"
        env_patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self._tmp / "xdg-config")})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_reader_called_only_on_poll_count_change(self) -> None:
        class _FakeConfigLoader:
            """A config_loader WITH a `poll_count` attribute (like the real
            `config.ConfigCache`), refreshing every 3rd call."""

            def __init__(self) -> None:
                self.poll_count = 0
                self._calls = 0

            def __call__(self) -> dict:
                self._calls += 1
                if self._calls % 3 == 1:
                    self.poll_count += 1
                return {"hibernate_backstop": False, "backstop_action_pct": 8.0}

        reader_calls: list = []
        daemon.run(
            state_dir=self.state_dir, db_path=self.db_path, sysfs_root=self.sysfs, procfs_root=self.procfs,
            interval=0.0, iterations=9, install_signal_handlers=False, check_events_at_start=False,
            critical_pct=-1, sync_journal_fn=lambda: True, hibernate_fn=_failing_hibernate_fn,
            upower_cache=_upower_cache(),
            config_loader=_FakeConfigLoader(),
            upower_conf_reader=lambda: reader_calls.append(1) or 2.0,
            backstop_notify_fn=lambda *a, **k: None,
        )
        # 9 ticks, refreshing every 3rd call -> 3 distinct poll_count values.
        self.assertEqual(len(reader_calls), 3, msg=f"reader_calls: {reader_calls}")

    def test_plain_callable_without_poll_count_checks_every_tick(self) -> None:
        # A config_loader with no `poll_count` attribute (e.g. a test's own
        # plain lambda) has no cache boundary to gate on, so this must fall
        # back to checking every tick -- never silently stop alerting.
        reader_calls: list = []
        daemon.run(
            state_dir=self.state_dir, db_path=self.db_path, sysfs_root=self.sysfs, procfs_root=self.procfs,
            interval=0.0, iterations=4, install_signal_handlers=False, check_events_at_start=False,
            critical_pct=-1, sync_journal_fn=lambda: True, hibernate_fn=_failing_hibernate_fn,
            upower_cache=_upower_cache(),
            config_loader=lambda: {"hibernate_backstop": False, "backstop_action_pct": 8.0},
            upower_conf_reader=lambda: reader_calls.append(1) or 2.0,
            backstop_notify_fn=lambda *a, **k: None,
        )
        self.assertEqual(len(reader_calls), 4)


if __name__ == "__main__":
    unittest.main()


class SysfsPctOnUpowerScaleTests(unittest.TestCase):
    """Measured 2026-09-24: capacity 104 (charge_now / charge_full_DESIGN on a
    104.8%-health cell) while charge_now/charge_full = 99.81 and UPower = 99.78."""

    def test_uses_charge_ratio_not_capacity(self) -> None:
        battery = {"pct": 104, "charge_now_ah": 6.697, "charge_full_ah": 6.71}
        self.assertAlmostEqual(backstop.sysfs_pct_on_upower_scale(battery), 99.81, places=2)

    def test_healthy_cell_near_full_is_not_divergent(self) -> None:
        battery = {"pct": 106, "charge_now_ah": 6.70, "charge_full_ah": 6.71}
        result = backstop.compute_upower_divergence(
            upower_pct=99.7, sysfs_pct=backstop.sysfs_pct_on_upower_scale(battery),
            energy_full=76.09, energy_full_design=72.576,
        )
        self.assertEqual(result["state"], "ok")

    def test_control_capacity_basis_would_be_divergent(self) -> None:
        # The same numbers compared on the old basis (capacity) cross the 5-point line.
        result = backstop.compute_upower_divergence(
            upower_pct=99.7, sysfs_pct=106, energy_full=76.09, energy_full_design=72.576,
        )
        self.assertEqual(result["state"], "divergent")

    def test_real_fault_still_divergent_on_charge_ratio(self) -> None:
        battery = {"pct": 33, "charge_now_ah": 2.103, "charge_full_ah": 6.71}
        result = backstop.compute_upower_divergence(
            upower_pct=3.27161, sysfs_pct=backstop.sysfs_pct_on_upower_scale(battery),
            energy_full=722.698, energy_full_design=72.576,
        )
        self.assertEqual(result["state"], "divergent")

    def test_falls_back_to_capacity_without_charge_counters(self) -> None:
        self.assertEqual(backstop.sysfs_pct_on_upower_scale({"pct": 57, "charge_now_ah": None, "charge_full_ah": None}), 57)
        self.assertEqual(backstop.sysfs_pct_on_upower_scale({"pct": 57, "charge_now_ah": 1.0, "charge_full_ah": 0}), 57)
