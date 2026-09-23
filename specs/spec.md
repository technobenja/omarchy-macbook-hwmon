# hwmon — Hardware Telemetry for omarchy (MacBook Pro 11,1)

**v2 — 2026-09-23** (Fable advisor review APPROVE-WITH-CHANGES; B1, B2, S1–S4 and nits folded in same day). Supersedes v1 (`36bd734`, 2026-09-22). v1 was written by a
local model in OpenCode and never built; its shape is kept, its platform
assumptions are corrected against the machine (measured 2026-09-23, below).

Decisions (Ben, 2026-09-23): **a module in the omarchy-shell bar next to the
battery + the `hwmon` CLI is enough.** No tray app, no history window. Push to
Gitea (`techno/hwmon`).

## Measured on this machine (2026-09-23) — the ground truth this spec builds on

| fact | value |
|---|---|
| Bar | **omarchy-shell** (Quickshell 0.3.1, QML). **Waybar is not installed.** |
| Bar layout | `~/.config/omarchy/shell.json` → `bar.layout.right` = tray, agents, bluetooth, network, audio, monitor, **power** (battery lives in `omarchy.power`) |
| User plugins | `~/.config/omarchy/plugins/<plugin-id>/` with a `manifest.json` (schemaVersion 1, `kinds: ["bar-widget"]`); hot-reloads on save; never edit `/usr/share/omarchy/` |
| Autostart | `~/.config/hypr/autostart.lua`, syntax `o.launch_on_start("…")` — **not** used here (systemd user unit instead) |
| CPU | i5-4278U, 2 cores / 4 threads; `coretemp` → Package id 0, Core 0, Core 1 |
| GPU | Intel Haswell-ULT iGPU only — **no discrete GPU, no GPU die sensor** beyond SMC `TCGC` |
| Fans | **ONE fan** — `applesmc.768/fan1_{input,min,max,manual}`, label "Right Side", min 1299 / max 6199 RPM |
| SMC temps | 31 `applesmc` `tempN_{label,input}`; **several read `-127000`, `-43000`, `-42750`** (absent/invalid sensors: TH0C, TH0F, TH0R, TH0c, THSP, TMLB, TW0P) |
| Battery | `/sys/class/power_supply/BAT0/`: capacity, status, charge_now/full/full_design, current_now/avg, voltage_now/min_design, cycle_count, temp, manufacturer, model_name |
| AC | `/sys/class/power_supply/ADP1/online` |
| Thermal zones | thermal_zone0, thermal_zone1 |
| Disk | `sda` APPLE SSD SD0128F over SATA; `drivetemp` **not loaded** |
| Python | 3.14 system; **`psutil` not installed**; PyQt not installed |

## Architecture

```
 hwmon-collector (systemd --user, Python stdlib only)
   │ every 1 s: read sysfs/procfs
   ├─► $XDG_RUNTIME_DIR/hwmon/latest.json   (atomic write: tmp + rename)
   └─► ~/.local/share/hwmon/hwmon.db        (SQLite, WAL, retention tiers)
                │                                   │
   bar widget (QML, FileView watchChanges)     `hwmon` CLI (reads both)
```

One producer, two readers, **no network listener**.

## ADDED

**A1 — Collector daemon.** `hwmon daemon` (run by `hwmon.service`, a systemd
user unit, `WantedBy=graphical-session.target`, `Restart=on-failure`). Samples
every 1 s using only the Python standard library. Writes `latest.json`
atomically, then inserts one row into SQLite. A failing sensor read yields
`null` for that field and never stops the loop.

**A2 — Snapshot contract** (`latest.json`). This table IS the seam between the
Python and QML halves. **`tests/fixtures/latest.example.json` is committed
first and both halves test against it**: Python shape-asserts every snapshot
it writes against the fixture's key set and types; the QML is loaded against
the fixture in check 7. Units live in the key suffix. Every leaf is nullable
(`null` = could not read) except `schema` and `ts`. Rates (`*_bps`, usage %)
are `null` on the first sample.

| key | type | unit / meaning | source |
|---|---|---|---|
| `schema` | int | always `1` | — |
| `ts` | float | unix seconds, UTC | `time.time()` |
| `battery.pct` | int | % | `BAT0/capacity` |
| `battery.status` | str | as reported: Charging, Discharging, Full, Not charging, Unknown | `BAT0/status` |
| `battery.power_w` | float | W, **signed**: − discharging, + charging, **0 for any other status** | `voltage_now × current_now` (µV×µA→W). **Driver reports current as an unsigned magnitude (measured 1,390,000 while Discharging)** — sign comes from `status` only |
| `battery.voltage_v` | float | V | `voltage_now` µV |
| `battery.current_a` | float | A, unsigned magnitude | `current_now` µA |
| `battery.temp_c` | float | °C | `BAT0/temp` is **deci-°C** (345 → 34.5) |
| `battery.cycles` | int | count | `cycle_count` |
| `battery.health_pct` | float | raw `charge_full / charge_full_design × 100`, **may exceed 100** (measured 104.8 on the new cell) — do not clamp | |
| `battery.charge_now_ah`, `charge_full_ah`, `charge_design_ah` | float | Ah | µAh |
| `ac.online` | bool | | `ADP1/online` |
| `cpu.package_c` | float | °C | coretemp "Package id 0" |
| `cpu.cores_c` | object label→float | °C | coretemp "Core N" |
| `cpu.load` | [float,float,float] | 1/5/15 min | `/proc/loadavg` |
| `cpu.usage_pct` | float | all CPUs | `/proc/stat` delta |
| `cpu.per_core` | array of `{usage_pct: float, freq_mhz: float}`, one per logical CPU (4 here) | | `/proc/stat`, `cpufreq/scaling_cur_freq` kHz |
| `fan.label` | str | stripped (`"Right Side  "` → `"Right Side"`) | `fan1_label` |
| `fan.rpm`, `fan.min_rpm`, `fan.max_rpm` | int | RPM | `fan1_{input,min,max}` |
| `fan.manual` | bool | | `fan1_manual` |
| `temps` | object SMC label→float | °C, **valid applesmc sensors only** (A3) | `applesmc.768/tempN_{label,input}` m°C |
| `sensors_invalid` | array of str | SMC labels rejected by A3 — absence is visible, never silent | |
| `system.mem_used_bytes`, `mem_total_bytes`, `swap_used_bytes`, `swap_total_bytes` | int | bytes; used = Total − Available | `/proc/meminfo` |
| `system.disk` | `{device: "sda", read_bps: float, write_bps: float}` | bytes/s | `/proc/diskstats` × 512 |
| `system.net` | `{iface: str, rx_bps: float, tx_bps: float}` | bytes/s; iface = default-route device from `/proc/net/route`, else whole object `null` | `/proc/net/dev` |

Discovery rules: find hwmon devices **by their `name` file, never by index**
(coretemp is `hwmon3` today; `hwmon2` has no `name` file at all); a missing
file is `null`, never an exception. The thermal zones (`BAT0`, `x86_pkg_temp`)
duplicate the sources above and are **not** read. All readers take a
`sysfs_root` / `procfs_root` parameter so fixture trees can stand in for the
real ones.

**A3 — Invalid-sensor filter.** A reading ≤ −40 °C or ≥ 130 °C is invalid and
lands in `sensors_invalid`, not in `temps`. (Measured invalids are −127, −43,
−42.75.)

**A4 — Battery power, signed.** `battery.power_w = voltage_now × current_now`
(µV × µA → W), **negative while discharging, positive while charging**, using
`status`. `battery.health_pct = charge_full / charge_full_design × 100`.

**A5 — Storage and retention.** `~/.local/share/hwmon/hwmon.db`,
`journal_mode=WAL`, `synchronous=NORMAL` (no fsync per second on the SSD).
Table `raw(ts REAL PRIMARY KEY, cpu_package_c, fan_rpm, battery_power_w,
battery_pct, battery_temp_c, cpu_usage_pct, snapshot TEXT)` — the eight
headline columns for fast queries, plus the full snapshot JSON. One INSERT +
commit per tick. Table `minute(ts_min INTEGER PRIMARY KEY, <metric>_min/avg/max
for the six headline metrics>)`. Raw rows kept 24 h, minute aggregates 30
days. **History beyond 24 h exists only for those six metrics** — stated
here so nobody expects otherwise. Aggregation + pruning run every 10 min in
the daemon. No timed `VACUUM`. `hwmon history` metric names = the six column
names above.

**A6 — Bar widget.** User plugin `techno.hwmon` at
`~/.config/omarchy/plugins/techno.hwmon/` (source in the repo under
`plugin/techno.hwmon/`). Required manifest fields: `schemaVersion, id, name,
version, kinds: ["bar-widget"], entryPoints.barWidget, barWidget.displayName,
barWidget.category`. **No `plugins[]` entry in `shell.json`**: a bar widget is
enabled iff it sits in `bar.layout.*` (`PluginRegistry.findEntryLocation`).
Placed in `bar.layout.right` **immediately before `omarchy.power`**. Reads
`$XDG_RUNTIME_DIR/hwmon/latest.json` (QML: `Quickshell.env("XDG_RUNTIME_DIR")`,
fallback `/run/user/<uid>`) via `FileView { watchChanges: true; onFileChanged:
reload() }` — **advisor measured that this re-arms across tmp+rename
replacements**. A 1 s `Timer` re-evaluates staleness (A9) independently of
file events, because a dead collector produces no events. Compact label:
`<package_c rounded>° <rpm/1000, 1 decimal>k`, e.g. `59° 1.3k`; a null field
renders as `–` in its slot. Colour escalates on thresholds (A8). Colours come
from `Color.*` / `bar.foreground` — none hard-coded. The widget declares
`ipcTarget: "techno.hwmon"` with a `state()` method returning `{label, stale,
age_s}` so checks can read what the bar is actually showing.

**A7 — Widget popup (two pages, per Ben 2026-09-22 "prioritize low level stats
and offer a high level on another page").** Built on the first-party pattern
(read `/usr/share/omarchy/shell/plugins/panels/power/Panel.qml` and
`.../agents/Panel.qml`): root is `qs.Ui` `Panel` + `BarIconButton` +
`KeyboardPanel { anchorItem; owner; open }`, sections via
`PanelSectionHeader`, spacing `Style.space()`. **Pages use the agents pattern
(`Repeater` + `selected: index === root.pageIndex`)** — `switchPanel()`
switches between widgets, not pages. Click opens:
- **Hardware** (default): battery (pct, status, power W signed, V, A, temp,
  cycles, health %), AC, fan (rpm vs min/max as a bar), CPU package/cores,
  every valid SMC sensor with its label, and the invalid-sensor count.
- **System**: load avg, per-core usage + freq, RAM/swap, `sda` read/write,
  net rx/tx.
- Header shows snapshot age; **stale (> 5 s) is shown, never hidden** (A9).

**A8 — Thresholds** (single table in the plugin, not scattered): CPU package
≥ 80 °C warn, ≥ 95 °C critical; fan ≥ 5000 RPM warn; battery temp ≥ 45 °C warn.

**A9 — Staleness.** If `latest.json` is missing or older than 5 s, the label
reads `hwmon —` in the muted colour and the popup says why (file missing vs
stale by N s). The widget must never show an old number as current.

**A10 — CLI** `hwmon` (installed to `~/.local/bin/hwmon`):
- `hwmon` — human-readable snapshot (hardware first)
- `hwmon --json` — the snapshot verbatim
- `hwmon history [metric] [--minutes N]` — min/avg/max + a text sparkline
- `hwmon peaks` — highest recorded values since boot and in the last 24 h
- `hwmon daemon` — the collector (A1)
Exit non-zero with a clear message when the snapshot is missing or stale.

**A11 — Install/uninstall.** `install.sh` (idempotent, no root):
1. `omarchy plugin validate plugin/techno.hwmon` — abort on failure.
2. Copy the CLI to `~/.local/bin/hwmon` and the package beside it.
3. Stage the plugin as a **real copy (`cp -rL`, never a symlink — the validator
   refuses symlinks)** into a temp dir under `~/.config/omarchy/plugins/`,
   then one `mv` into place (a file-by-file copy fires one hot-reload per file).
4. Install + `systemctl --user enable --now hwmon.service`.
5. Timestamped backup of `shell.json`, then **use the platform, never
   hand-edit `shell.json`** (the shell watches and writes it itself):
   `omarchy-shell shell rescanPlugins` →
   `omarchy plugin enable techno.hwmon --section right --before omarchy.power`
   (leaves an already-placed widget where it is).

`install.sh --uninstall`: `omarchy plugin disable techno.hwmon`, stop/disable
the unit, remove the plugin dir and CLI. **Keeps `hwmon.db`** unless
`--purge`.

**A12 — systemd unit.** `Type=simple`, `ExecStart=%h/.local/bin/hwmon daemon`,
`RuntimeDirectory=hwmon` (created/cleaned under `$XDG_RUNTIME_DIR`),
`Restart=on-failure`, `PartOf=graphical-session.target`,
`WantedBy=graphical-session.target`, `Nice=10`.

## MODIFIED (from v1)

- **M1** Waybar module → omarchy-shell bar widget (A6/A7). Waybar isn't here.
- **M2** Two fans (front/rear, "total airflow") → **one fan** with min/max.
- **M3** Autostart via `awful.spawn` (AwesomeWM syntax) → systemd user unit.
- **M4** "GPU die temp / RAM temp / Northbridge" → whatever valid SMC labels
  exist, shown by label; no invented sensors.
- **M5** `psutil` dependency → stdlib `/proc` parsing (psutil isn't installed;
  one less package on an old box).
- **M6** Retention 1s/24h + 1min/30d + 1h/90d + 50 MB cap + VACUUM →
  two tiers (A5). Simpler, bounded by time.

## REMOVED (from v1)

- **R1** PyQt5 tray app and pyqtgraph history window (Ben: bar + CLI is enough).
- **R2** HTTP server on `127.0.0.1:8936`. Its only consumers were Waybar and the
  tray app; both are gone, and the file-watch path is the shell's native idiom.
  No listening socket = nothing to secure.
- **R3** `smartctl` SSD temperature — needs root, contradicts "no root". A
  future option is loading `drivetemp` (one-time root), out of scope here.

## Acceptance (WHEN / THEN) — each must be *observed*, not inferred

0. WHEN the collector writes a snapshot THEN its key set and types equal
   `tests/fixtures/latest.example.json` (shape assertion, run live and in
   tests). **Positive control:** a snapshot with one key removed fails it.
1. WHEN `hwmon.service` runs for 60 s THEN, sampled 60 times at 1 Hz,
   `latest.json` `ts` is ≤ 2 s old every time, and the DB has ≥ 55 raw rows
   for that minute.
2. WHEN a sensor reads −127 °C THEN it appears in `sensors_invalid` and not in
   `temps` — proven with a fixture, and with a **positive control** (a valid
   sensor in the same fixture appears in `temps`).
3. WHEN the machine is on battery THEN `battery.power_w` < 0; WHEN charging
   THEN > 0 (fixture both ways; live check for whichever state is current).
4. WHEN the collector is stopped THEN within 6 s
   `omarchy-shell shell call techno.hwmon state` reports `stale: true` and the
   label `hwmon —`; WHEN restarted THEN it returns to a live label within 3 s.
   One `grim` screenshot of the bar in each state for the human.
5. WHEN `install.sh` runs twice THEN `shell.json` contains `techno.hwmon`
   exactly once, positioned immediately before `omarchy.power`, and a backup
   exists; WHEN `--uninstall` runs THEN the layout equals the pre-install
   layout.
6. WHEN the collector runs for 10 min THEN its average CPU use is < 1 % of one
   core and RSS < 40 MB (measured from `/proc/<pid>/stat`, not estimated).
7. WHEN the shell reloads plugins THEN **(positive control first)**
   `omarchy plugin list --json` includes `techno.hwmon` as enabled,
   `omarchy plugin validate` exits 0, and
   `journalctl --user _COMM=quickshell --since <install time>` shows the
   plugin loading; AND that same journal window has no QML warning or error
   mentioning `techno.hwmon`. A clean log with no load line is a FAIL.
8. WHEN rows are older than 24 h THEN they are aggregated then deleted — tested
   with an injected clock on a throwaway DB, never by waiting.

## Non-goals
Fan control (writing `fan1_manual`/`fan1_output`); any network egress or OB2
push; root-only sensors; Waybar support.

## Repo layout

```
hwmon/
├── specs/spec.md
├── hwmon/              # Python package (stdlib only)
│   ├── __main__.py     # CLI entry
│   ├── sensors.py      # sysfs/procfs readers (sysfs_root/procfs_root params) + invalid filter
│   ├── snapshot.py     # schema build + atomic write
│   ├── store.py        # SQLite schema, insert, retention
│   └── daemon.py       # 1 s loop
├── bin/hwmon           # thin launcher
├── plugin/techno.hwmon/  # manifest.json, Widget.qml, Popup.qml, Thresholds.js
├── systemd/hwmon.service
├── tests/              # stdlib unittest
│   └── fixtures/       # latest.example.json (THE contract) + fake sysfs/procfs trees
├── install.sh
└── README.md
```
