"""hwmon's local config file: `hibernate_backstop` and `backstop_action_pct`
(backstop.py, R-L1.5, amended by §11 R2). Default-off/default-unset is
safety-critical here (the deliverables spec gates the backstop on a
supervised test that has not happened, and R2 is explicit that there is
"no default that enables anything" for the threshold either), so every
"can't read this" path is tested as its own positive control.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hwmon import config

DEFAULTS = {"hibernate_backstop": False, "backstop_action_pct": None}


class LoadConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-config-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.path = self._tmp / "config.json"

    def test_missing_file_yields_defaults(self) -> None:
        self.assertEqual(config.load_config(self.path), DEFAULTS)

    def test_hibernate_backstop_true_is_read(self) -> None:
        self.path.write_text('{"hibernate_backstop": true}')
        self.assertEqual(config.load_config(self.path), {**DEFAULTS, "hibernate_backstop": True})

    def test_hibernate_backstop_false_is_read(self) -> None:
        self.path.write_text('{"hibernate_backstop": false}')
        self.assertEqual(config.load_config(self.path), DEFAULTS)

    def test_invalid_json_yields_defaults_not_an_exception(self) -> None:
        self.path.write_text("not json {{{")
        self.assertEqual(config.load_config(self.path), DEFAULTS)

    def test_non_dict_json_yields_defaults(self) -> None:
        self.path.write_text("[1, 2, 3]")
        self.assertEqual(config.load_config(self.path), DEFAULTS)

    def test_wrong_type_for_known_key_yields_default_for_that_key(self) -> None:
        # A misconfigured file must fail SAFE (off), not raise and not
        # silently coerce a string/int into "on".
        self.path.write_text('{"hibernate_backstop": "yes"}')
        self.assertEqual(config.load_config(self.path), DEFAULTS)

    def test_unknown_keys_are_ignored(self) -> None:
        self.path.write_text('{"hibernate_backstop": true, "future_flag": 42}')
        self.assertEqual(config.load_config(self.path), {**DEFAULTS, "hibernate_backstop": True})

    def test_directory_instead_of_file_yields_defaults(self) -> None:
        # A read failure of any OTHER kind (not just "missing") must also
        # fail safe -- positive control beyond plain FileNotFoundError.
        a_dir = self._tmp / "config_is_a_dir.json"
        a_dir.mkdir()
        self.assertEqual(config.load_config(a_dir), DEFAULTS)

    def test_default_config_path_uses_xdg_config_home(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg-test"}):
            self.assertEqual(config.default_config_path(), Path("/tmp/xdg-test/hwmon/config.json"))

    def test_default_config_path_falls_back_to_home_dot_config(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(config.default_config_path(), Path.home() / ".config" / "hwmon" / "config.json")


class BackstopActionPctTests(unittest.TestCase):
    """§11 R2: "the threshold comes from hwmon config (`backstop_action_pct`)
    ... no default that enables anything"."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-config-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.path = self._tmp / "config.json"

    def test_absent_by_default(self) -> None:
        self.assertIsNone(config.load_config(self.path)["backstop_action_pct"])

    def test_int_value_is_read_as_float(self) -> None:
        self.path.write_text('{"backstop_action_pct": 8}')
        self.assertEqual(config.load_config(self.path)["backstop_action_pct"], 8.0)

    def test_float_value_is_read(self) -> None:
        self.path.write_text('{"backstop_action_pct": 7.5}')
        self.assertEqual(config.load_config(self.path)["backstop_action_pct"], 7.5)

    def test_bool_value_is_rejected_not_coerced_to_0_or_1(self) -> None:
        # bool is a subclass of int in Python -- a naive isinstance(x, (int,
        # float)) check would silently accept `true` as 1.0.
        self.path.write_text('{"backstop_action_pct": true}')
        self.assertIsNone(config.load_config(self.path)["backstop_action_pct"])

    def test_string_value_is_rejected(self) -> None:
        self.path.write_text('{"backstop_action_pct": "8"}')
        self.assertIsNone(config.load_config(self.path)["backstop_action_pct"])

    def test_infinity_is_rejected(self) -> None:
        # Round-2 review, BLOCKER: json.loads accepts bare "Infinity" (a
        # Python/simplejson extension) -- a threshold of Infinity would make
        # `in_danger_zone` permanently False, silently disabling the backstop.
        self.path.write_text('{"backstop_action_pct": Infinity}')
        self.assertIsNone(config.load_config(self.path)["backstop_action_pct"])

    def test_negative_infinity_is_rejected(self) -> None:
        self.path.write_text('{"backstop_action_pct": -Infinity}')
        self.assertIsNone(config.load_config(self.path)["backstop_action_pct"])

    def test_nan_is_rejected(self) -> None:
        # NaN compares unequal to everything, including itself -- every
        # `<=`/`>` comparison in in_danger_zone would silently be False.
        self.path.write_text('{"backstop_action_pct": NaN}')
        self.assertIsNone(config.load_config(self.path)["backstop_action_pct"])

    def test_over_100_is_rejected(self) -> None:
        self.path.write_text('{"backstop_action_pct": 200}')
        self.assertIsNone(config.load_config(self.path)["backstop_action_pct"])

    def test_exactly_100_is_accepted(self) -> None:
        # Positive control for the upper boundary.
        self.path.write_text('{"backstop_action_pct": 100}')
        self.assertEqual(config.load_config(self.path)["backstop_action_pct"], 100.0)

    def test_zero_is_rejected(self) -> None:
        # 0 <= sysfs capacity is always true -- a 0% threshold would fire
        # (once the margin is subtracted, at a negative danger-zone edge)
        # or otherwise mean something nonsensical; the spec requires a
        # strictly positive threshold.
        self.path.write_text('{"backstop_action_pct": 0}')
        self.assertIsNone(config.load_config(self.path)["backstop_action_pct"])

    def test_negative_is_rejected(self) -> None:
        self.path.write_text('{"backstop_action_pct": -1}')
        self.assertIsNone(config.load_config(self.path)["backstop_action_pct"])

    def test_small_positive_is_accepted(self) -> None:
        # Positive control for the lower boundary.
        self.path.write_text('{"backstop_action_pct": 0.5}')
        self.assertEqual(config.load_config(self.path)["backstop_action_pct"], 0.5)

    def test_rejection_logs_one_warning(self) -> None:
        import contextlib
        import io

        self.path.write_text('{"backstop_action_pct": 200}')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            config.load_config(self.path)
        self.assertIn("backstop_action_pct", buf.getvalue())
        self.assertEqual(len(buf.getvalue().splitlines()), 1)

    def test_both_keys_together(self) -> None:
        self.path.write_text('{"hibernate_backstop": true, "backstop_action_pct": 8}')
        result = config.load_config(self.path)
        self.assertEqual(result, {"hibernate_backstop": True, "backstop_action_pct": 8.0})


class ConfigCacheTests(unittest.TestCase):
    """SHOULD 2 (review): cache `load_config` so it isn't re-read/parsed
    every 1 s tick."""

    def test_polls_at_most_once_per_ttl(self) -> None:
        calls = []

        class _Clock:
            def __init__(self):
                self.t = 1000.0

            def __call__(self):
                return self.t

        clock = _Clock()
        cache = config.ConfigCache(
            ttl_s=10.0, load_fn=lambda path: calls.append(1) or dict(DEFAULTS), clock=clock
        )
        for _ in range(10):
            cache.get()
            clock.t += 1.0
        self.assertEqual(len(calls), 1, "must poll exactly once across a 10s window")
        self.assertEqual(cache.poll_count, 1)

    def test_repolls_after_ttl_elapses(self) -> None:
        calls = []

        class _Clock:
            def __init__(self):
                self.t = 1000.0

            def __call__(self):
                return self.t

        clock = _Clock()
        cache = config.ConfigCache(ttl_s=10.0, load_fn=lambda path: calls.append(1) or dict(DEFAULTS), clock=clock)
        cache.get()
        clock.t += 10.0
        cache.get()
        self.assertEqual(len(calls), 2)

    def test_returns_the_loaded_dict(self) -> None:
        cache = config.ConfigCache(load_fn=lambda path: {"hibernate_backstop": True, "backstop_action_pct": 5.0})
        self.assertEqual(cache.get(), {"hibernate_backstop": True, "backstop_action_pct": 5.0})

    def test_callable_matches_get(self) -> None:
        # ConfigCache instances must be drop-in for daemon.run's plain
        # `Callable[[], dict]` config_loader parameter.
        cache = config.ConfigCache(load_fn=lambda path: dict(DEFAULTS))
        self.assertEqual(cache(), cache.get())

    def test_default_clock_is_monotonic(self) -> None:
        import time as _time

        cache = config.ConfigCache()
        self.assertIs(cache.clock, _time.monotonic)


if __name__ == "__main__":
    unittest.main()
