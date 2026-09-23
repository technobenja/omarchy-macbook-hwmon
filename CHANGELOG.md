# Changelog

Semver. The version lives in three places that must agree:
`hwmon/__init__.py` (`__version__`), `plugin/techno.hwmon/manifest.json`
(`version`), and this file.

## Unreleased

- Spec v3 delta (items 1–6): sleep-block guard on low battery, fan control
  mode + target RPM, throttle tracking, `hwmon fancurve`, power-loss events,
  retuned fan warning. Specced and advisor-reviewed; **not built yet**.

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
