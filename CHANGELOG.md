# Changelog

Semver. The version lives in three places that must agree:
`hwmon/__init__.py` (`__version__`), `plugin/techno.hwmon/manifest.json`
(`version`), and this file.

## Unreleased

- Public release on GitHub (`technobenja/omarchy-macbook-hwmon`): MIT
  license, README install/requirements for GitHub, mbpfan note; lab-specific
  references and the fixture's machine-id removed. No code changes.

## 1.1.0 — 2026-09-23

Spec v3 delta (items 1–6) as amended by two Fable advisor reviews (spec
review, then a pre-deploy review of the battery pieces: SHIP-WITH-FIXES, all
six fixes applied). Deployed and verified live on omarchy — see the spec's
*v3 AS EXECUTED* note. 224 Python tests + 555 widget tests.

- **Snapshot schema 1 → 2**: adds `power_guard`, `cpu.throttle`,
  `fan.target_rpm`, `fan.control`. The embedded shape reference
  (`snapshot._REFERENCE_SNAPSHOT`) and the shape check (`power_guard.blockers`
  checked per element; `cpu.throttle` exact-key; `temps`/`cores_c` unchanged
  as dynamic label maps) were updated together with the fixture.
- **A13 sleep-block guard**: `power_guard` from logind's `ListInhibitors`
  over `busctl --system -j` (`hwmon/inhibitors.py`), only `mode=="block"`
  entries whose colon-split `what` contains `"sleep"`; cached at most every
  10 s across ticks (`InhibitorCache`), `null`/`[]` on any busctl failure.
- **A14 fan control + target**: `fan.target_rpm` (`fan1_output`) and
  `fan.control` (`smc` / `mbpfan` / `manual`, the latter two disambiguated
  by `/run/mbpfan.pid` + a live `/proc/<pid>/comm` check).
- **A15 throttle tracking**: `cpu.throttle` (`core_count`, `package_count`,
  `recent`) aggregated per S3 — `package_count` is a **max** across logical
  CPUs (each carries a copy of the one package counter) and `core_count` is
  a **sum of per-core maxes** (each core's sibling threads carry a copy of
  that core's counter), not a naive sum of all four counters. `recent` is
  `null` on the first sample, else true if either count rose in the
  trailing 60 s.
- **A16 `hwmon fancurve`**: fan RPM vs CPU-package-temperature bins
  (`--hours <= 24`, `--bin`, default 5 °C), grouped by `fan.control` — a
  pre-v3 row with no `control` key is derived from `fan.manual`. `raw` gets
  a `fan_target_rpm` column, migrated with `ALTER TABLE ... ADD COLUMN`
  onto an existing DB.
- **A17/B1/B2 power-loss events**: new `events` table
  (`UNIQUE(ts_start)`, kept 30 days). `hwmon/events.py` classifies a
  `raw`-table gap as `hard_poweroff` / `unclean_shutdown` / `off_or_stopped`
  by whether it crosses a boot boundary (B1: compared against the boot list
  from `journalctl --list-boots`, never the running process's own `-b 0`)
  and, only for a boundary-crossing gap, that boot's `systemd-fsck` /
  `systemd-journald` evidence. Runs once at daemon start (covering both a
  fresh gap and any older not-yet-backfilled one) and again, read-only, in
  `hwmon events`. Reproduces the real 2026-09-23 hard power-off
  (`tests/fixtures/poweroff_2026-09-23/`) exactly: `hard_poweroff`,
  `last_pct 2`, `ts_start` = the 09:37:02 PDT row, `detail` = the fsck
  "Dirty bit" line.
- **M7/S5 cooling-saturated threshold**: `fan >= 5000 RPM` warn removed
  (mbpfan legitimately reaches ~5300 RPM); the sustained ≥60s
  `fan >= 0.95*max` check lives in the QML half (not this package).
- **B3 install ordering**: `install.sh` now always `restart`s
  `hwmon.service` after copying the package (`enable --now` alone doesn't
  restart an already-active unit) and waits up to 10 s for `latest.json` to
  report the new schema before staging the plugin.
- 8 new test files (`test_throttle`, `test_power_guard`, `test_events`,
  `test_store_events`, `test_cli_v3`, `test_daemon_v3`, `test_procutil`)
  plus 5 extended ones, 224 tests total (up from 105). Mutation-tested: the
  S3 aggregation, the A13 busctl block/delay filter, the B1
  btime/boot-crossing rule, and the B2 dedupe (both the Python filter and
  the SQL `UNIQUE` constraint).
- **Second advisor pass (SHIP-WITH-FIXES), applied:**
  - **B1 tightened**: `find_crossed_boot` now bounds the candidate boot by
    `ts_before < first_entry_s <= ts_after`, not just `> ts_before` — the
    original form could walk past a rotated-out intervening boot and
    borrow a LATER boot's fsck line for an unrelated crash.
  - **`raw_points_for_events` sped up**: `json_extract(snapshot,
    '$.battery.status')` in SQL instead of a per-row `json.loads` in
    Python (measured 0.19 s @ 86,400 rows, was ~2.5 s extrapolated); logs
    a line if the scan still takes > 1 s.
  - **`install.sh`**: wait extended 3 s → 10 s; the schema-probe's command
    substitution is now `|| true`-guarded so it can't abort the script
    mid-loop under `set -e`; the timeout message states the machine is
    half-upgraded and says to re-run.
  - **`hwmon events` dedupe** now checks every recorded event
    (`existing_event_ts_starts()`), not just the ones inside `--days` —
    a short display window could otherwise let an already-recorded gap
    resurface via the live re-scan.
  - **`InhibitorCache`** keys its TTL off `time.monotonic()` (injectable
    via a `clock` field), never the wall clock, and `DEFAULT_TIMEOUT_S`
    dropped 5.0 s → 1.0 s (busctl measured ~3 ms; 5 s equalled the bar
    widget's own staleness bound).

## 1.0.0 — 2026-09-23

First release, built to spec v2 and verified live on omarchy.

- Collector (`hwmon daemon`, systemd user unit): 1 s sysfs/procfs sampling,
  Python stdlib only; atomic `$XDG_RUNTIME_DIR/hwmon/latest.json`; SQLite
  history (24 h per-second, 30 d per-minute). Measured ~0.87 % of one core.
- Bar widget `techno.hwmon`, placed before the battery: CPU package °C + fan
  RPM, threshold colours, `hwmon —` when stale, two-page popup
  (Hardware / System), IPC `state` for checks.
- CLI: `hwmon`, `--json`, `history`, `peaks`, `daemon`.
- `install.sh` with `--uninstall` / `--purge`.
- Fixed during install: invalid-sensor cutoff −40 → 0 °C (SMC TH0F drifted to
  −34.75 °C and spammed the journal); shape check made structural for sensor
  maps and logged on change only; popup clipped at 16 of 24 sensors.
