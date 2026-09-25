# hwmon

Hardware telemetry for **omarchy**, a 13-inch MacBook Pro 11,1 (Retina, Mid 2014) running Arch
Linux + Omarchy. It adds a compact reading to the omarchy-shell bar, next to
the battery, and provides an `hwmon` command for the terminal.

It was built for a machine that has just had a new iFixit battery and fan
fitted: it keeps the low-level health numbers (fan, SMC temperatures, battery
power and wear) one glance away, and keeps a history.

> **Status (2026-09-25): v1.3.1 installed and running on omarchy.** The
> System page groups **BACKUPS** (home snapshots, NAS backup); the **UPower
> check** moved into the Hardware page's battery section. v1.3.0 added the NAS
> backup row (schema 4) and an operator [`RUNBOOK.md`](RUNBOOK.md). 474 Python +
> 660 widget tests; verified live — see the spec's *v7 AS EXECUTED* note and
> [`CHANGELOG.md`](CHANGELOG.md).

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
  network throughput, and **BACKUPS**: home snapshots and NAS backup (see
  [`RUNBOOK.md`](RUNBOOK.md) for every state). The battery section on the
  Hardware page carries the **UPower check**.

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

## Install

**Requirements:** [Omarchy](https://omarchy.org/) (the omarchy-shell bar), a
Mac with the `applesmc` kernel module loaded, Python 3 (standard library
only), `systemd --user`. Built and tested on a MacBook Pro 11,1 (13-inch Retina,
Mid 2014); other `applesmc` Macs may work but are untested.

```bash
git clone https://github.com/technobenja/omarchy-macbook-hwmon.git ~/dev/hwmon
~/dev/hwmon/install.sh
```

`install.sh` (idempotent, no root, `set -euo pipefail`):

1. `omarchy plugin validate plugin/techno.hwmon` — aborts on failure.
2. Installs the CLI to `~/.local/bin/hwmon` and the Python package beside it,
   at `~/.local/share/hwmon/lib/hwmon/` (the launcher puts that `lib/` dir on
   `PYTHONPATH` and execs `python3 -m hwmon`, so the two must stay paired).
3. Stages the plugin as a real copy (`cp -rL`, never a symlink — the
   validator refuses symlinks) into a temp dir under
   `~/.config/omarchy/plugins/`, then one `mv` into place.
4. Installs `systemd/hwmon.service` and runs
   `systemctl --user enable --now hwmon.service`.
5. Takes a timestamped backup of `~/.config/omarchy/shell.json`, then uses
   the platform rather than hand-editing it: `omarchy-shell shell
   rescanPlugins` → `omarchy plugin enable techno.hwmon --section right
   --before omarchy.power` (leaves an already-placed widget where it is, so
   running install.sh again is a no-op there).

```bash
~/dev/hwmon/install.sh --uninstall           # keeps hwmon.db
~/dev/hwmon/install.sh --uninstall --purge   # also deletes hwmon.db
```

`--uninstall` disables the widget, stops and removes the systemd unit, and
removes the CLI and package. History (`hwmon.db`) is kept unless `--purge`
is also given.

> **After an update, run `omarchy restart shell`.** The shell hot-reloads the
> widget's entry file, but Quickshell keeps its cached copies of the popup's
> other QML files, so a changed popup keeps showing the old layout until the
> shell restarts (observed 2026-09-23). `install.sh` itself now restarts
> `hwmon.service` on every run and waits (up to 10 s) for it to report the
> current schema before staging the plugin — `enable --now` alone doesn't
> restart an already-running collector, which used to leave it on the old
> schema after an update. If the wait times out, the script dies rather
> than stage a plugin the collector can't back up yet.

## CLI

```bash
hwmon                                   # current readings, hardware first
hwmon --json                            # the raw snapshot
hwmon history [metric] [--minutes N]    # default: all six headline metrics, last 60 min
hwmon peaks                             # highest values since boot and in the last 24 h
hwmon fancurve [--hours N] [--bin C]    # fan RPM vs CPU temp, grouped by control mode; --hours <= 24 (raw retention)
hwmon events [--days N]                 # power-loss events (hard_poweroff / unclean_shutdown / off_or_stopped)
hwmon daemon                            # run the collector loop in the foreground (what the service runs)
```

`hwmon fancurve` reads only the per-second `raw` table (pairing a CPU temp
with a fan RPM needs the 1 s samples, which the per-minute history doesn't
keep), grouped by 5 °C bins (`--bin`) and by `fan.control` (`smc` / `mbpfan`
/ `manual`) — a pre-v3 row with no `control` key is derived from
`fan.manual` (`False` → `smc`, `True` → `mbpfan`).

`hwmon events` lists recorded power-loss events and also live-rescans the
retained `raw` table for any gap not yet recorded, without writing —
recording only happens once, at daemon start (backfill included).

Every command that reads `latest.json` exits non-zero with a clear message
if the snapshot is missing or more than 5 s old — it never prints a stale
number as if it were current.

`--state-dir DIR` / `--db PATH` (or the `HWMON_STATE_DIR` / `HWMON_DB`
environment variables) override where `latest.json` and `hwmon.db` live.
The defaults are `$XDG_RUNTIME_DIR/hwmon/latest.json` and
`~/.local/share/hwmon/hwmon.db` — what `install.sh` and `hwmon.service`
use; the overrides exist mainly for testing against a scratch directory
without touching real state.

## Fan control on Macs (recommended companion)

hwmon only *monitors*. On this MacBook the SMC's automatic mode never raised
the fan above ~1,300 RPM, even at 87 °C under Linux (`hwmon fancurve` shows
it). Installing [`mbpfan`](https://github.com/linux-on-mac/mbpfan) fixed it;
hwmon then shows `Control: mbpfan` and the target vs actual RPM.

## Scope

Monitoring only. It never writes to the fan or any other hardware control,
never needs root, and targets this machine; other Macs with `applesmc` may
work but are untested.

## Layout

```
specs/spec.md                      the spec (v2 → v6 deltas + AS EXECUTED) — the source of truth
tests/fixtures/latest.example.json the collector ↔ widget contract (schema 4)
tests/fixtures/poweroff_2026-09-23/ real captured data for the events classifier (acceptance 13)
hwmon/                             collector + CLI
tests/                             stdlib unittest suite (474 tests) + sysfs/procfs fixture builders
plugin/techno.hwmon/               omarchy-shell bar widget
systemd/hwmon.service              user service (built)
install.sh                         install / uninstall
bin/hwmon                          thin launcher installed to ~/.local/bin/hwmon
```

## License

MIT — see [`LICENSE`](LICENSE).
