"""daemon.run's shape-mismatch logging.

Corrected 2026-09-23: a real shape mismatch (SMC sensor TH0F drifting into
a value the old A3 cutoff no longer caught) was logged EVERY TICK, ~86k
journal lines/day. A shape mismatch is now logged only when the mismatch
SET changes from the previous tick, and once more when it clears -- never
once per tick for an unchanged condition.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hwmon import backstop, daemon

from . import fakefs


def _build_valid_tree(tmp: Path) -> tuple[Path, Path]:
    sysfs = tmp / "sys"
    procfs = tmp / "proc"
    fakefs.add_coretemp(sysfs, index=3)
    fakefs.add_applesmc(sysfs, index=2)
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
    return sysfs, procfs


class DaemonShapeMismatchLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-daemon-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs, self.procfs = _build_valid_tree(self._tmp)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"
        # SHOULD 3 (review): pin XDG_CONFIG_HOME to a temp dir on every test
        # that calls daemon.run, so a real ~/.config/hwmon/config.json on
        # the machine running the suite can never leak into a test (belt
        # and suspenders alongside the explicit config_loader override below).
        env_patch = mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self._tmp / "xdg-config")})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run(self, error_sequence: list[list[str]]) -> str:
        stderr = io.StringIO()
        # Fix pass (deliverables SPEC.md §11 R3): this test is about
        # shape-mismatch logging only -- give the standing UPower divergence
        # check a fake that agrees with the fixture's sysfs capacity (81) so
        # it never itself logs anything and never shells out to a real
        # busctl in a unit test.
        upower_cache = backstop.UPowerCache(
            read_fn=lambda: {"pct": 81.0, "energy_full": 50.0, "energy_full_design": 50.0}
        )
        with mock.patch("hwmon.snapshot.validate_shape", side_effect=error_sequence):
            with contextlib.redirect_stderr(stderr):
                daemon.run(
                    state_dir=self.state_dir,
                    db_path=self.db_path,
                    sysfs_root=self.sysfs,
                    procfs_root=self.procfs,
                    interval=0.0,
                    iterations=len(error_sequence),
                    install_signal_handlers=False,
                    upower_cache=upower_cache,
                    config_loader=lambda: {"hibernate_backstop": False, "backstop_action_pct": None},
                    hibernate_fn=lambda: (_ for _ in ()).throw(AssertionError("must never call a real hibernate_fn")),
                    # Item 8 (review): every daemon.run() test must inject
                    # notify_fn too, or a real notify-send can fire.
                    backstop_notify_fn=lambda *a, **k: None,
                    triage_notify_fn=lambda *a, **k: None,
                )
        return stderr.getvalue()

    def test_unchanging_mismatch_logs_once_not_per_tick(self) -> None:
        # 5 ticks, same mismatch every time.
        out = self._run([["temps.TH0F: unexpected key"]] * 5)
        mismatch_lines = [line for line in out.splitlines() if "shape mismatch" in line]
        self.assertEqual(len(mismatch_lines), 1, msg=out)

    def test_clean_run_logs_nothing(self) -> None:
        out = self._run([[]] * 5)
        self.assertEqual(out, "")

    def test_appear_then_clear_logs_exactly_twice(self) -> None:
        out = self._run([[], [], ["temps.TH0F: unexpected key"], ["temps.TH0F: unexpected key"], []])
        lines = [line for line in out.splitlines() if "shape" in line.lower()]
        self.assertEqual(len(lines), 2, msg=out)
        self.assertIn("mismatch", lines[0])
        self.assertIn("clear", lines[1].lower())

    def test_a_changed_mismatch_set_logs_again(self) -> None:
        # Different mismatch each tick -> logs every time, since the SET changed.
        out = self._run(
            [
                ["temps.TH0F: unexpected key"],
                ["temps.TH0F: unexpected key", "temps.TH0R: unexpected key"],
                ["temps.TH0R: unexpected key"],
            ]
        )
        mismatch_lines = [line for line in out.splitlines() if "shape mismatch" in line]
        self.assertEqual(len(mismatch_lines), 3, msg=out)


if __name__ == "__main__":
    unittest.main()
