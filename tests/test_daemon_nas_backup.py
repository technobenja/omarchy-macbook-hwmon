"""daemon.run's v5 wiring (SPEC-nas-backup.md R-N7): the injected
`nas_backup_cache` reading lands verbatim under `recovery.nas_backup` in
`latest.json`, and it is polled at most once per its own TTL across many
ticks -- never a real read of the machine's actual status file in this
test file.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hwmon import backstop, daemon, nas_backup

from . import fakefs


def _quiet_upower_cache() -> backstop.UPowerCache:
    return backstop.UPowerCache(read_fn=lambda: {"pct": None, "energy_full": None, "energy_full_design": None})


_SAFE_BACKSTOP_KWARGS = dict(
    config_loader=lambda: {"hibernate_backstop": False, "backstop_action_pct": None},
    hibernate_fn=lambda: (_ for _ in ()).throw(AssertionError("must never call a real hibernate_fn")),
    backstop_notify_fn=lambda *a, **k: None,
    triage_notify_fn=lambda *a, **k: None,
)


def _build_valid_tree(tmp: Path) -> tuple[Path, Path]:
    sysfs = tmp / "sys"
    procfs = tmp / "proc"
    fakefs.add_coretemp(sysfs, index=3)
    fakefs.add_applesmc(sysfs, index=2)
    fakefs.add_power_supply(
        sysfs,
        "BAT0",
        {
            "capacity": "81", "status": "Discharging", "voltage_now": "11247000",
            "current_now": "1390000", "temp": "345", "cycle_count": "3",
            "charge_now": "5435000", "charge_full": "6710000", "charge_full_design": "6400000",
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


class NasBackupCacheWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-daemon-nas-backup-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sysfs, self.procfs = _build_valid_tree(self._tmp)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"
        env_patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self._tmp / "xdg-config")})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run(self, cache: nas_backup.NasBackupCache, *, iterations: int = 1) -> dict:
        daemon.run(
            state_dir=self.state_dir,
            db_path=self.db_path,
            sysfs_root=self.sysfs,
            procfs_root=self.procfs,
            interval=0.0,
            iterations=iterations,
            install_signal_handlers=False,
            nas_backup_cache=cache,
            list_boots_fn=lambda: [],
            journal_query_fn=lambda b: [],
            upower_cache=_quiet_upower_cache(),
            **_SAFE_BACKSTOP_KWARGS,
        )
        return json.loads((self.state_dir / "latest.json").read_text())

    def test_not_configured_reading_is_written_into_latest_json(self) -> None:
        cache = nas_backup.NasBackupCache(state_path=Path("/nonexistent/nas-backup-for-test.json"))
        snap = self._run(cache)
        self.assertEqual(
            snap["recovery"]["nas_backup"], {"state": "not_configured", "age_s": None, "reason": None}
        )

    def test_fresh_reading_is_written_into_latest_json(self) -> None:
        # `state_path` must point at a real file -- `is_file()` gates
        # whether `read_fn` is ever called at all (a missing file must
        # never even attempt a read).
        real_path = self._tmp / "nas-backup.json"
        real_path.write_text("{}")
        cache = nas_backup.NasBackupCache(
            state_path=real_path,
            read_fn=lambda p: json.dumps({"result": "ok", "ts": 1_000_000.0, "last_ok_ts": 1_000_000.0}),
            now_fn=lambda: 1_000_000.0 + 3600.0,
        )
        snap = self._run(cache)
        self.assertEqual(snap["recovery"]["nas_backup"]["state"], "fresh")
        self.assertAlmostEqual(snap["recovery"]["nas_backup"]["age_s"], 3600.0, delta=1.0)

    def test_failed_reading_with_reason_is_written_into_latest_json(self) -> None:
        real_path = self._tmp / "nas-backup.json"
        real_path.write_text("{}")
        cache = nas_backup.NasBackupCache(
            state_path=real_path,
            read_fn=lambda p: json.dumps({"result": "failed", "reason": "no-fresh-source", "ts": 1_000_000.0}),
            now_fn=lambda: 1_000_000.0,
        )
        snap = self._run(cache)
        self.assertEqual(snap["recovery"]["nas_backup"], {"state": "failed", "age_s": None, "reason": "no-fresh-source"})

    def test_cache_polled_at_most_once_per_ttl_across_many_ticks(self) -> None:
        real_path = self._tmp / "nas-backup.json"
        real_path.write_text(json.dumps({"result": "ok", "ts": 1.0, "last_ok_ts": 1.0}))
        calls = []
        cache = nas_backup.NasBackupCache(
            state_path=real_path,
            read_fn=lambda p: calls.append(1) or p.read_text(),
            ttl_s=10.0,
        )
        self._run(cache, iterations=25)
        # 25 ticks at interval=0.0 all happen within a fraction of a second
        # of wall-clock time, so with a 10s TTL the cache must poll ONCE.
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
