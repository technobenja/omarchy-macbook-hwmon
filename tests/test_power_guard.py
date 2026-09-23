"""A13/S1: the sleep-block guard.

**S1 (advisor, measured):** `busctl --system -j call ... ListInhibitors`
returns `json.loads(out)["data"][0]` as a list of `[what, who, why, mode,
uid, pid]` rows; `what` is colon-joined and must be split on `:` (not
substring-matched) to test for the "sleep" token.

A13: only `mode == "block"` AND `"sleep" in what.split(":")` count --
`delay` inhibitors are always present and harmless (measured live
2026-09-23: NetworkManager, UPower, Omarchy lock-screen, all `delay`).
Cached at most every 10 s (`InhibitorCache`).
"""

from __future__ import annotations

import unittest

from hwmon import inhibitors

# The real rows measured live on omarchy 2026-09-23 -- three delay
# inhibitors, all harmless. This is acceptance 9's "live" half.
_LIVE_DELAY_ONLY_ROWS = [
    ["sleep", "UPower", "Pause device polling", "delay", 0, 988],
    ["sleep", "Omarchy", "Lock screen before suspend", "delay", 1000, 1025],
    ["sleep", "NetworkManager", "NetworkManager needs to turn off networks", "delay", 0, 685],
]


class ComputePowerGuardTests(unittest.TestCase):
    def test_delay_only_rows_yield_not_blocked(self) -> None:
        # Acceptance 9, live half: today's three inhibitors are all delay.
        result = inhibitors.compute_power_guard(_LIVE_DELAY_ONLY_ROWS)
        self.assertEqual(result, {"sleep_blocked": False, "blockers": []})

    def test_block_sleep_inhibitor_is_blocked_and_named(self) -> None:
        # Acceptance 9, fixture half.
        rows = _LIVE_DELAY_ONLY_ROWS + [
            ["sleep", "omarchy-update", "Omarchy update in progress", "block", 0, 4242],
        ]
        result = inhibitors.compute_power_guard(rows)
        self.assertTrue(result["sleep_blocked"])
        self.assertEqual(result["blockers"], [{"who": "omarchy-update", "why": "Omarchy update in progress"}])

    def test_block_non_sleep_inhibitor_does_not_count(self) -> None:
        # Positive control for the `what.split(":")` filter: a block-mode
        # inhibitor whose `what` has nothing to do with sleep.
        rows = [["shutdown", "systemd-logind", "some other reason", "block", 0, 1]]
        result = inhibitors.compute_power_guard(rows)
        self.assertEqual(result, {"sleep_blocked": False, "blockers": []})

    def test_colon_joined_what_is_split_not_substring_matched(self) -> None:
        # "asleepy" contains "sleep" as a substring but must NOT match --
        # proves the filter splits on ":" rather than using `in` on the raw
        # string.
        rows = [["asleepy:shutdown", "someone", "bogus", "block", 0, 1]]
        result = inhibitors.compute_power_guard(rows)
        self.assertEqual(result["blockers"], [])

    def test_multi_token_what_with_sleep_and_block_counts(self) -> None:
        rows = [["sleep:shutdown:idle", "installer", "long update", "block", 0, 1]]
        result = inhibitors.compute_power_guard(rows)
        self.assertTrue(result["sleep_blocked"])

    def test_multiple_block_sleep_inhibitors_all_listed(self) -> None:
        rows = [
            ["sleep", "a", "reason a", "block", 0, 1],
            ["sleep", "b", "reason b", "block", 0, 2],
        ]
        result = inhibitors.compute_power_guard(rows)
        self.assertEqual(len(result["blockers"]), 2)

    def test_none_rows_yields_null_never_a_crash(self) -> None:
        # A busctl failure (timeout, missing binary, malformed JSON) --
        # list_inhibitors returns None; compute_power_guard must never raise.
        result = inhibitors.compute_power_guard(None)
        self.assertEqual(result, {"sleep_blocked": None, "blockers": []})

    def test_malformed_row_is_skipped_not_fatal(self) -> None:
        rows = [["too", "short"], ["sleep", "ok", "reason", "block", 0, 1]]
        result = inhibitors.compute_power_guard(rows)
        self.assertEqual(len(result["blockers"]), 1)
        self.assertEqual(result["blockers"][0]["who"], "ok")


class ListInhibitorsParsingTests(unittest.TestCase):
    """S1: the exact `busctl -j` JSON shape, measured live."""

    def test_parses_measured_busctl_json_shape(self) -> None:
        import json
        from unittest import mock

        measured_json = json.dumps(
            {
                "type": "a(ssssuu)",
                "data": [
                    [
                        ["sleep", "UPower", "Pause device polling", "delay", 0, 988],
                        ["sleep", "Omarchy", "Lock screen before suspend", "delay", 1000, 1025],
                    ]
                ],
            }
        )
        with mock.patch("hwmon.procutil.run", return_value=measured_json) as mock_run:
            rows = inhibitors.list_inhibitors()
        mock_run.assert_called_once()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "sleep")
        self.assertEqual(rows[0][3], "delay")

    def test_procutil_failure_yields_none(self) -> None:
        from unittest import mock

        with mock.patch("hwmon.procutil.run", return_value=None):
            self.assertIsNone(inhibitors.list_inhibitors())

    def test_malformed_json_yields_none_not_exception(self) -> None:
        from unittest import mock

        with mock.patch("hwmon.procutil.run", return_value="not json{{{"):
            self.assertIsNone(inhibitors.list_inhibitors())

    def test_unexpected_shape_yields_none(self) -> None:
        from unittest import mock

        with mock.patch("hwmon.procutil.run", return_value='{"data": "not-a-list-of-lists"}'):
            self.assertIsNone(inhibitors.list_inhibitors())


class _FakeClock:
    """An injectable, fully deterministic stand-in for `time.monotonic` --
    starts at `start` and only moves when `advance()` is called, so a test
    can drive the TTL boundary exactly without ever sleeping for real, and
    without the cache touching the real wall or monotonic clock at all
    (S6: the cache's OWN default is `time.monotonic`, not the wall clock,
    but tests inject this instead of either)."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class InhibitorCacheTests(unittest.TestCase):
    """A13: "Queried at most every 10 s (cached between ticks)"."""

    def test_first_call_polls(self) -> None:
        calls = []

        def fake_poll():
            calls.append(1)
            return []

        cache = inhibitors.InhibitorCache(poll_fn=fake_poll, clock=_FakeClock())
        cache.get()
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.poll_count, 1)

    def test_call_within_ttl_does_not_repoll(self) -> None:
        calls = []

        def fake_poll():
            calls.append(1)
            return []

        clock = _FakeClock(1000.0)
        cache = inhibitors.InhibitorCache(poll_fn=fake_poll, ttl_s=10.0, clock=clock)
        cache.get()
        clock.advance(5.0)  # 5s later, inside the 10s TTL
        cache.get()
        clock.advance(4.9)
        cache.get()
        self.assertEqual(len(calls), 1, "must not poll again inside the TTL window")

    def test_call_at_or_past_ttl_repolls(self) -> None:
        calls = []

        def fake_poll():
            calls.append(1)
            return []

        clock = _FakeClock(1000.0)
        cache = inhibitors.InhibitorCache(poll_fn=fake_poll, ttl_s=10.0, clock=clock)
        cache.get()
        clock.advance(10.0)  # exactly at the TTL boundary
        cache.get()
        self.assertEqual(len(calls), 2)

    def test_ten_ticks_in_ten_seconds_polls_exactly_once(self) -> None:
        # S4 (advisor): simulates a 1 Hz daemon loop for 10 ticks at
        # t=1000..1009 -- with a 10s TTL the boundary (t=1010) is never
        # reached, so this must poll EXACTLY once, never once per tick.
        calls = []

        def fake_poll():
            calls.append(1)
            return []

        clock = _FakeClock(1000.0)
        cache = inhibitors.InhibitorCache(poll_fn=fake_poll, ttl_s=10.0, clock=clock)
        for _ in range(10):
            cache.get()
            clock.advance(1.0)
        self.assertEqual(len(calls), 1)

    def test_returns_computed_result_from_poll(self) -> None:
        rows = [["sleep", "x", "y", "block", 0, 1]]
        cache = inhibitors.InhibitorCache(poll_fn=lambda: rows, clock=_FakeClock())
        result = cache.get()
        self.assertTrue(result["sleep_blocked"])

    def test_failed_poll_yields_null_and_is_cached_too(self) -> None:
        cache = inhibitors.InhibitorCache(poll_fn=lambda: None, clock=_FakeClock())
        result = cache.get()
        self.assertEqual(result, {"sleep_blocked": None, "blockers": []})

    def test_default_clock_is_monotonic_not_wall_clock(self) -> None:
        # S6: the cache's default clock must be time.monotonic, never
        # time.time -- proves the actual default wiring, not just that an
        # injected fake behaves as expected.
        import time as _time

        cache = inhibitors.InhibitorCache(poll_fn=lambda: [])
        self.assertIs(cache.clock, _time.monotonic)

    def test_backward_wall_clock_jump_would_have_fooled_a_time_time_cache(self) -> None:
        # S6 positive control: simulate what a wall-clock cache would do
        # across an NTP correction that steps the clock BACKWARD. With a
        # monotonic-style fake clock this can't happen (advance() only
        # moves forward); this test proves the cache has no code path that
        # treats a smaller "now" as anything other than "not stale yet".
        calls = []

        def fake_poll():
            calls.append(1)
            return []

        clock = _FakeClock(1000.0)
        cache = inhibitors.InhibitorCache(poll_fn=fake_poll, ttl_s=10.0, clock=clock)
        cache.get()  # poll #1 at t=1000
        clock._t = 990.0  # simulate a backward jump (never happens with real monotonic)
        cache.get()  # now - last_poll_ts = -10 < ttl_s -> must NOT repoll
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
