# hwmon

Hardware telemetry for **omarchy**, a 2013 MacBook Pro 11,1 running Arch
Linux + Omarchy. It adds a compact reading to the omarchy-shell bar, next to
the battery, and provides an `hwmon` command for the terminal.

It was built for a machine that has just had a new iFixit battery and fan
fitted: it keeps the low-level health numbers (fan, SMC temperatures, battery
power and wear) one glance away, and keeps a history.

> **Status (2026-09-23):** spec v2 approved after an advisor review; the build
> is in progress. Sections marked *(planned)* describe the spec, not shipped
> code. See [`specs/spec.md`](specs/spec.md).

## What it shows

**In the bar** — CPU package temperature and fan speed, e.g. `59° 1.3k`,
coloured when they cross a threshold. If the collector stops, the label turns
to `hwmon —` instead of showing an old number as current.

**Click for detail**, on two pages:

- **Hardware** — battery (%, status, signed power in W, voltage, current,
  temperature, cycle count, health vs design capacity), AC, the fan against its
  min/max, CPU package and cores, and every working SMC temperature sensor by
  label. Sensors the SMC reports as absent (`-127 °C` and similar) are counted,
  not hidden.
- **System** — load, per-core usage and frequency, RAM/swap, SSD read/write,
  network throughput.

## How it works

```
 hwmon daemon  (systemd user service, Python standard library only)
   │ every 1 s: reads /sys and /proc
   ├─► $XDG_RUNTIME_DIR/hwmon/latest.json   → bar widget (watches the file)
   └─► ~/.local/share/hwmon/hwmon.db        → `hwmon` CLI history
```

- No root, no network listener, no network egress, no extra packages.
- History: every second for 24 h; per-minute min/avg/max for 30 days for the
  six headline metrics.
- The file contract between the collector and the widget is
  [`tests/fixtures/latest.example.json`](tests/fixtures/latest.example.json);
  both halves are tested against it.

## Install *(planned)*

```bash
git clone http://gitea.lab:3000/techno/hwmon.git ~/dev/hwmon
~/dev/hwmon/install.sh
```

`install.sh` validates the plugin, installs the CLI and the user service, and
places the widget before the battery with `omarchy plugin enable` (it backs up
`~/.config/omarchy/shell.json` first). `install.sh --uninstall` reverses it and
keeps the history database; add `--purge` to delete that too.

## CLI *(planned)*

```bash
hwmon                     # current readings, hardware first
hwmon --json              # the raw snapshot
hwmon history [metric] [--minutes N]
hwmon peaks               # highest values since boot and in the last 24 h
```

## Scope

Monitoring only. It never writes to the fan or any other hardware control,
never needs root, and targets this machine; other Macs with `applesmc` may
work but are untested.

## Layout

```
specs/spec.md                      the spec (v2) — the source of truth
tests/fixtures/latest.example.json the collector ↔ widget contract
hwmon/                             collector + CLI (planned)
plugin/techno.hwmon/               omarchy-shell bar widget (in progress)
systemd/hwmon.service              user service (planned)
install.sh                         install / uninstall (planned)
```
