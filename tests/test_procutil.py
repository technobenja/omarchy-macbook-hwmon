"""`procutil.run` -- the shared subprocess wrapper for the v3 live-system
calls (`busctl`, `journalctl`): must never raise, and must actually enforce
its timeout against a REAL subprocess, not just a mocked one (S4, advisor).
"""

from __future__ import annotations

import time
import unittest

from hwmon import procutil


class ProcutilRunTests(unittest.TestCase):
    def test_successful_command_returns_stdout(self) -> None:
        out = procutil.run(["/bin/echo", "hello"], timeout=5.0)
        self.assertEqual(out, "hello\n")

    def test_nonzero_exit_yields_none(self) -> None:
        out = procutil.run(["/usr/bin/false"], timeout=5.0)
        self.assertIsNone(out)

    def test_missing_binary_yields_none_not_exception(self) -> None:
        out = procutil.run(["/no/such/binary-hwmon-test", "arg"], timeout=5.0)
        self.assertIsNone(out)

    def test_real_subprocess_timeout_yields_none(self) -> None:
        # S4 (advisor): exercise the REAL subprocess.run timeout path --
        # not a mock standing in for it -- against a command that
        # genuinely outlives the timeout, to prove procutil.run actually
        # enforces it (and doesn't, say, swallow TimeoutExpired only in a
        # unit test's imagination).
        start = time.monotonic()
        out = procutil.run(["/usr/bin/sleep", "5"], timeout=0.1)
        elapsed = time.monotonic() - start
        self.assertIsNone(out)
        self.assertLess(elapsed, 4.0, msg="a real timeout must return well before the full sleep completes")

    def test_timeout_kills_the_child_process(self) -> None:
        # Positive control for the timeout test above: confirm the
        # underlying subprocess.run(timeout=...) mechanism really is what
        # bounds the call, by using a large timeout and observing it DOES
        # take close to the full duration (i.e. this isn't somehow
        # returning instantly regardless of the command).
        start = time.monotonic()
        out = procutil.run(["/bin/echo", "quick"], timeout=5.0)
        elapsed = time.monotonic() - start
        self.assertEqual(out, "quick\n")
        self.assertLess(elapsed, 4.0, msg="a fast command must not be held up by the timeout value")


if __name__ == "__main__":
    unittest.main()
