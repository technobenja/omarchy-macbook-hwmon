# hwmon — Hardware Telemetry for omarchy (MacBook Pro 11,1)

**v2 — 2026-09-23** (Fable advisor review APPROVE-WITH-CHANGES; B1, B2, S1–S4 and nits folded in same day). Supersedes v1 (`36bd734`, 2026-09-22). v1 was written by a
local model in OpenCode and never built; its shape is kept, its platform
assumptions are corrected against the machine (measured 2026-09-23, below).

Decisions (owner, 2026-09-23): **a module in the omarchy-shell bar next to the
battery + the `hwmon` CLI is enough.** No tray app, no history window.

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

*(Corrected 2026-09-23: -40 → 0 °C after TH0F read -34.75 live; exact-key
temps check replaced by structural check.)*

**A4 — Battery power, signed.** `battery.power_w = voltage_now × current_now`
(µV × µA → W), **negative while discharging, positive while charging**, using
`status`. `battery.health_pct = charge_full / charge_full_design × 100`.

**A5 — Storage and retention.** `~/.local/share/hwmon/hwmon.db`,
`journal_mode=WAL`, `synchronous=NORMAL` (no fsync per second on the SSD).
Table `raw(ts REAL PRIMARY KEY, cpu_package_c, fan_rpm, battery_power_w,
battery_pct, battery_temp_c, cpu_usage_pct, snapshot TEXT)` — six
headline metric columns for fast queries, plus the full snapshot JSON. One INSERT +
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

**A7 — Widget popup (two pages, per the owner, 2026-09-22 "prioritize low level stats
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

- **R1** PyQt5 tray app and pyqtgraph history window (the owner: bar + CLI is enough).
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
   `omarchy-shell techno.hwmon state` reports `stale: true` and the
   label `hwmon —`; WHEN restarted THEN it returns to a live label within 3 s.
   One `grim` screenshot of the bar in each state for the human.
   *(Corrected 2026-09-23: v2 said `omarchy-shell shell call …`, which only
   resolves panel/overlay/menu plugins — `shell.qml:1279` looks up
   `panelLoaders` — and returns `unknown` for a bar widget. A bar widget is
   reached through its own `IpcHandler` target, as `omarchy.power` is.)*
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
Fan control (writing `fan1_manual`/`fan1_output`); any network egress or push to
external services; root-only sensors; Waybar support.

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

---

# v3 delta — 2026-09-23 (fan/battery findings)

Scope approved by the owner 2026-09-23: items 1–6 of the post-mbpfan review. Item 7
(power profile on the System page) **REMOVED — `omarchy.power` already shows
it** (the owner). Background, measured today: the SMC never raised the fan above
~1320 RPM at 40–87 °C; a manual 4000 RPM override reached 4259 RPM (fan
hardware good); `mbpfan` 2.4.0 now controls the fan (`fan1_manual=1`,
`/run/mbpfan.pid`); the 09:36 battery death was a hard power-off (dirty FAT
fsck + journald "uncleanly shut down" on the next boot) with UPower's 2 %
action blocked, most likely by `block`-mode sleep inhibitors.

**Contract:** `schema` becomes **2**. `tests/fixtures/latest.example.json` is
updated first (schema 2) and both halves test against it, as in v2. The widget
treats any schema other than 2 as not-live (existing A9 rule), so collector and
widget ship together; `install.sh` restarts `hwmon.service` on update, and the
README's `omarchy restart shell` note applies.

## ADDED

**A13 — Sleep-block guard (item 1).** Snapshot key `power_guard`:
`{sleep_blocked: bool|null, blockers: [{who: str, why: str}]}`. Source:
logind `ListInhibitors` via `busctl --system call org.freedesktop.login1
/org/freedesktop/login1 org.freedesktop.login1.Manager ListInhibitors`
(no root; measured). **Only entries whose `what` contains `sleep` AND whose
mode is `block` count** — `delay` inhibitors are always present and harmless
(measured: NetworkManager, UPower, Omarchy lock-screen, all `delay`). Queried
at most every 10 s (cached between ticks); failure → `null`, never a crash.
Widget: **critical** when `battery.pct ≤ 10` AND `status == Discharging` AND
`sleep_blocked == true`; the popup shows a banner naming each blocker
(`who — why`). Threshold lives in Thresholds.js.

**A14 — Fan control + target (item 2).** New keys `fan.target_rpm` (int,
`fan1_output`) and `fan.control` (str): `"smc"` if `fan1_manual == 0`;
`"mbpfan"` if `fan1_manual == 1` and `/run/mbpfan.pid` names a live process
whose `/proc/<pid>/comm` is `mbpfan`; otherwise `"manual"`; `null` if
unreadable. Popup replaces the `manual yes/no` row with `Control: SMC auto |
mbpfan | manual` and shows `target N rpm` next to actual.

**A15 — Throttle tracking (item 5).** New key `cpu.throttle`:
`{core_count: int, package_count: int, recent: bool}` — counts summed over
`/sys/devices/system/cpu/cpu*/thermal_throttle/{core,package}_throttle_count`;
`recent` = either sum increased within the last 60 s. Widget: **warn** when
`recent` is true; popup CPU section shows both counts.

**A16 — `hwmon fancurve` (item 4).** `hwmon fancurve [--hours N ≤ 24]
[--bin C, default 5]`: one row per CPU-package temperature bin — samples,
min/avg/max `fan_rpm`, and avg `fan.target_rpm` when present. Reads `raw`
only (pairing needs per-second rows), so ≤ 24 h — stated in `--help`.
Adds `fan_target_rpm` as a column in `raw`, migrated with
`ALTER TABLE … ADD COLUMN` when absent (old rows NULL).

**A17 — Power-loss events (item 6).** New table `events(ts_start REAL,
ts_end REAL, kind TEXT, last_pct INT, last_status TEXT, detail TEXT)`, kept
30 days. At daemon start, if the newest `raw` row is > 30 s older than now,
the daemon classifies the gap and inserts one row:
- `hard_poweroff` — last sample Discharging with `pct ≤ 5`, AND the current
  boot's journal (`journalctl -b 0`, readable without root — measured) has
  `systemd-fsck` "Dirty bit is set" or `systemd-journald` "uncleanly shut
  down"; `detail` quotes the matched line.
- `unclean_shutdown` — the journal evidence without the low-battery condition.
- `off_or_stopped` — neither (clean shutdown, collector stopped, suspend).
`hwmon events [--days N]` lists them; it also scans the retained `raw` for
gaps not yet in `events` (so history present at upgrade time is covered),
without writing.

## MODIFIED

- **M7 (A8 thresholds, item 3):** remove `fan ≥ 5000 RPM` warn (with mbpfan
  the fan reaches 5,300 RPM during video playback — measured). Replace with
  **"cooling saturated" warn**: `fan.rpm ≥ 0.95 × fan.max_rpm` AND
  `cpu.package_c ≥ 80`. CPU and battery-temp thresholds unchanged.
- **M8:** snapshot `schema` 1 → 2 (A13–A15 keys). The shape check's exact-key
  rule for non-dynamic dicts now covers `power_guard`, `fan`, `cpu.throttle`;
  `power_guard.blockers` is a list checked per element.

## REMOVED

- **R4:** power-profile display (item 7) — duplicate of `omarchy.power`.

## Acceptance (v3) — observed, each with a positive control

9. WHEN the inhibitor list holds only `delay` entries THEN `sleep_blocked` is
   false — **live** (today's three are all delay); AND WHEN a fixture holds a
   `block` sleep inhibitor THEN true and it appears in `blockers`.
10. WHEN `mbpfan` runs THEN live `fan.control == "mbpfan"` (current state);
    fixtures cover `smc` (`manual=0`) and `manual` (`manual=1`, no pidfile).
11. WHEN fixture counts rise between two samples THEN `throttle.recent` is
    true, and false 61 s later (injected clock).
12. WHEN `hwmon fancurve --hours 24` runs on the real DB THEN the rows before
    15:31 PDT show a flat ~1300 RPM and rows after show a rising average —
    i.e. it reproduces today's measured table.
13. WHEN `hwmon events` runs on the real DB THEN it reports a
    `hard_poweroff` around **09:36 PDT 2026-09-23 at ≤ 3 %** with the fsck line
    as detail — today's real event is the positive control. A fixture with a
    gap but no journal evidence yields `off_or_stopped`.
14. WHEN the label is fed fan 5,300 RPM at 72 °C THEN it is NOT warn (M7);
    WHEN fed 5,950/6,199 RPM at 84 °C THEN warn. WHEN pct 8, Discharging,
    sleep_blocked THEN critical.
15. Checks 0–8 still pass against schema 2.

## v3 amendment — Fable advisor review 2026-09-23: APPROVE-WITH-CHANGES

Folded the same day; claims marked *measured* were re-checked by the lead.
These rules override the v3 text above wherever they differ.

**B1 → A17 gap attribution.** A gap is a power-loss *candidate* only if the
newest `raw` ts is **earlier than this boot's `btime`** (`/proc/stat`;
measured 15:09:32 PDT). Otherwise it is `off_or_stopped` and the journal is
not consulted (a `systemctl --user stop` or suspend inside one boot must never
borrow that boot's fsck line). For a candidate, map it to the boot whose first
entry is the first after the last sample (`journalctl --list-boots -o json`)
and query **that boot by id**, never `-b 0`. Journal filter:
`SYSLOG_IDENTIFIER=systemd-fsck` / `systemd-journald` (*measured*: `-u
systemd-fsck` matches nothing). Journal is readable as `techno` via the ACL on
`/var/log/journal`; don't require the `systemd-journal` group. `ts_start` comes
from `raw` (journald lost the last ~30 s of boot −1: it ends 09:36:32, raw
ends 09:37:02). Add `boot_id TEXT` to `events`; `last_pct` stored as INT.

**B2 → backfill + permanent control.** The `events` migration **backfills**:
scan `raw` gaps, classify as above, insert. `UNIQUE(ts_start)` is the dedupe
key shared by the backfill, the daemon-start check and the `hwmon events`
scan. **Today's event is preserved in `tests/fixtures/poweroff_2026-09-23/`**
(668 real rows, one gap 09:37:02 → first post-boot row at pct 2; boot list;
the three boot-0 evidence lines) — captured 2026-09-23 15:48 PDT before
retention deleted it. Acceptance 13 runs against that fixture (permanent) and
additionally live if built before ~09:47 PDT 2026-09-24.

**B3 → install ordering.** `install.sh` on update: copy package →
`systemctl --user restart hwmon.service` → wait ≤ 3 s for `latest.json` with
the new `schema` → only then stage + `mv` the plugin. (*Measured*: `enable
--now` left `NRestarts=0` and the collector on schema 1.)

**S1 → inhibitor source.** `busctl --system -j call … ListInhibitors` →
`json.loads(out)["data"][0]` rows `[what, who, why, mode, uid, pid]`
(*measured*). `what` is colon-joined — split on `:` and test for `sleep`.

**S2 → A13 background corrected.** The v3 background said UPower's 2 % action
was "most likely" blocked by `block`-mode sleep inhibitors. **That is
unproven and probably wrong:** `upowerd` runs as root, and logind lets root
override inhibitors (advisor's reading of logind; not measured). Boot −1 has
no `upowerd` lines at all and lost its last 30 s, so the cause of the missed
2 % action is **unknown**. A13 stays as a *"sleep inhibited on low battery"*
indicator (still useful: it is exactly the state you want to see), not as a
diagnosis.

**S3 → A15 aggregation.** `package_count` = **max** across CPUs (each logical
CPU carries a copy of the one package counter). `core_count` = sum over
distinct `topology/core_id` of the max across that core's siblings
(*measured*: cpu0,2 → core 0; cpu1,3 → core 1). All counters read 0 live, so
the positive control is a fixture with unequal non-zero values: package 3 on
every CPU → 3 (not 12); cores 2,2 / 1,1 → 3 (not 6).

**S4:** covered by B1's journal filter.

**S5 → M7 is informational.** mbpfan reaches 0.95 × 6199 = 5889 RPM only at
≥ 86 °C, where the CPU ≥ 80 °C warn already fires (measured today: max 5335
RPM at 75 °C). Redefine "cooling saturated" as fan ≥ 0.95 × max **sustained
≥ 60 s**; it is expected to be rare. Acceptance 14's 5950 RPM / 84 °C case is
**fixture-only** (mbpfan cannot produce it).

**S6 → acceptance 12 corrected.** Before 15:31 PDT the data includes the owner's
manual 4000 RPM test (15:22:32–15:23:16, max 4259). `hwmon fancurve` groups by
`fan.control` (v2 rows: derived from `fan.manual`), and check 12 expects: SMC
rows flat ~1300 except the manual window; mbpfan rows (after 15:31:42) rising
by bin — measured n/min/avg/max: 60: 52/1112/1713/2553 · 65: 303/1092/2188/3847
· 70: 134/1255/2392/5225 · 75: 37/1763/3295/5335 · 80: 7/2383/3881/5231.

**S7 → types.** `power_guard.blockers` is `[]` (never null) when nothing
blocks; `cpu.throttle.recent` is `null` on the first sample; `fan.target_rpm`
int|null. The schema-2 fixture holds **one** `block` blocker so the
per-element check can fail.

## v3 AS EXECUTED — 2026-09-23 (released as v1.1.0)

Built by two agents (Python, QML) against the schema-2 fixture; battery
pieces reviewed pre-deploy by a Fable advisor (SHIP-WITH-FIXES: S1 crossed
boot must start inside the gap; S2 startup scan via `json_extract`, 0.19 s at
86,400 rows; S3 install wait 10 s + half-upgrade message; S4 five test gaps
incl. one that could not fail; S5 events dedupe set; S6 monotonic cache
clock, busctl timeout 1 s) — all applied. 224 Python + 555 widget tests.
Deployed with the B3 install ordering (restart → schema 2 → plugin swap),
then `omarchy restart shell`.

Verified **live** on omarchy (with positive controls):
- 9: only `delay` inhibitors → `sleep_blocked:false`; a temporary user
  `block` sleep inhibitor (`hwmon-test`) → `true` with the blocker listed,
  back to `false` after it expired. **Banner not seen in the live popup** (it
  opened on the System page and a synthetic key press didn't switch) —
  verified in the harness only.
- 10: `fan.control == "mbpfan"`, target 2969 vs actual 2968 RPM.
- 11: **real throttling during the owner's stress runs**: package temp 99 °C at
  16:53:34 PDT, `throttle.recent` true from the first event; counters
  package 58 on each CPU / core 49 & 9 aggregated to 58 / 58 (naive sum
  would be 232).
- 12: `hwmon fancurve --hours 24`: SMC rows flat ~1300 (one 4000 from the
  manual test), mbpfan rows rising to ~5800 avg at 85 °C.
- 13: the daemon's first v3 start backfilled `hard_poweroff 09:37:02 PDT,
  last_pct 2, boot 77091d3f…, "Dirty bit is set…"` into `events` — now
  persistent past 24 h raw retention.
- 6: collector steady state **0.92 %** of one core over 480 s (`/proc` delta,
  same method as v2's 0.89 %; window overlapped the owner's stress runs), RSS
  24.9 MB.

Notes: acceptance 14's "30 s → not warn" holds for the saturation rule
itself, but at 84 °C the unchanged CPU ≥ 80 °C warn colours the label anyway,
so "cooling saturated" is visible only in the popup's fan section. During the
~100 s full-load run the fan sat at max from +4 s and the CPU still reached
99 °C and throttled (58 events, ≤ 11 ms each) — the chassis limit, not a
fault. Deferred to the future hard-power-loss spec (advisor NITs): record
"journal unavailable" after repeated failures; log a marker line at
`pct ≤ 5 && Discharging`.

## Smoke + field test — 2026-09-23 18:10 PDT (v1.1.0)

- **Smoke (19/19):** service + mbpfan active; widget fresh; plugin valid;
  placed once before `omarchy.power`; versions 1.1.0 in repo, package, plugin,
  tag; installed files identical to the repo; all six CLI commands exit 0;
  schema 2 live; no journal warnings in 1 h; 224 + 555 tests; Gitea and GitHub
  at the same commit.
- **Fresh install from GitHub:** public clone → tests pass → `install.sh`
  (schema wait passed) → widget live; installed copy identical to GitHub.
- **Real load** (`stress --cpu 4`, 60 s): label tracked 64° → 86° at +3 s →
  fan at max (6.2k) by +7 s → peak 98 °C / 6290 RPM; fan held ≥ 0.95 × max at
  ≥ 80 °C for 57 samples (cooling-saturated condition met); no new throttle
  events at this load (counter stayed 58); after the load the fan eased
  6.2k → 5.4k → 4.0k → 2.3k → 1.3k over ~35 s. Widget never stale.
- **Stale path:** collector stopped 6 s → `hwmon —`, `stale:true`,
  `age_s:null`; live again 1.1 s after start; the short gap was correctly not
  recorded as a power-loss event.

---

# v4 delta — 2026-09-23 (hwmon's slice of the hard-power-loss recovery spec)

Scope: the Python-only requirements of `~/deliverables/omarchy-power-loss-recovery/SPEC.md`
(DRAFT v2, §10 amendments supersede its §5 requirement text) that live in
this repo per its §6.5 — R-L1.4, R-L1.5 (amended by §10 A4/A5), R-L3.1
(amended by §10 A10c), R-L4.1, and the two events-table kinds R-L1.5/R-L3.1
add. **The `omarchy-recover` CLI, the snapper `home` config, the UPower
drop-in, and `install.sh`'s R-L1.2/R-L1.3/R-L4.2 steps are OUT of this
repo's scope (a separate repo, per that spec's D4)** — this delta only
covers what hwmon itself observes and records. Not deployed by this delta:
no `install.sh` change, no version bump, no plugin/QML change (a later
agent does the widget side of `recovery`/the new event kinds).

**Contract:** `schema` becomes **3**. `tests/fixtures/latest.example.json`
is updated first (schema 3, adding `recovery`) and both halves test against
it, as in v3 — the widget treats any schema other than 3 as not-live
(existing A9 rule).

## ADDED

**A18 — Local config file (R-L1.5's gate).** `hwmon/config.py`:
`$XDG_CONFIG_HOME/hwmon/config.json` (falling back to
`~/.config/hwmon/config.json`), currently one key, `hibernate_backstop`
(bool, default `false`). Read fresh every tick (a lightweight local file
read, not a subprocess) so toggling it live takes effect within one tick,
with no daemon restart. Any read failure — missing file, unreadable,
invalid JSON, wrong type for the key — yields the default (`false`), never
an exception and never a silent "on".

**A19 — Critical-battery journal marker (R-L1.4).** The first time in a
boot that `battery.status == "Discharging" && battery.pct <=
critical_pct` (default 5, the measured UPower default as of the
deliverables spec's §2 — not yet the spec's final `PercentageAction`-derived
number, since that install has not happened), hwmon logs
`"hwmon: critical battery N%"` to its own stderr at `LOG_WARNING`
(a `"<4>"` `SyslogLevelPrefix=` prefix — stdlib-only, no `syslog`/`logging`
dependency) and runs `journalctl --user --sync`. Recorded as one
`critical_battery_marker` event, deduped by `(kind, boot_id)` against the
`events` table (`Store.has_event_kind_for_boot`) so a `systemctl --user
restart` mid-boot does not re-fire it — "once per boot", not merely "once
per process".

**A20 — Hibernate backstop (R-L1.5, §10 A4/A5 SUPERSEDED by §11 M1/R1-R4
below — a same-day fix pass, before this delta shipped).** **DISABLED BY
DEFAULT** (A18) — the deliverables spec gates any automatic hibernate on a
supervised round-trip test (its R-L1.1) that has not happened.

- **§11 M1 (measured):** UPower's percentage was found ~10x wrong live
  (sysfs `capacity=33` vs UPower `percentage=3.27161`; `energy-full`
  722.698 Wh vs `energy-full-design` 72.576 Wh) -- a live UPower reliability
  fault, not a fixed offset. Two hours earlier the two scales had agreed
  within 2 points (49 vs 47.3).
- **§11 R1 (BLOCKER):** the trigger reads sysfs `battery.pct`/`status` --
  the SAME fields `critical_marker.py` already reads -- and makes NO
  UPower call of its own. A backstop must not share a failure mode with
  the primary (UPower's own critical action) it backs up.
- **§11 R2:** the threshold is hwmon config, `backstop_action_pct`
  (`config.py`), with NO default that enables anything -- `PercentageAction`
  turned out not to be a real D-Bus property at all (measured: "No such
  property"). If `hibernate_backstop` is true but `backstop_action_pct` is
  absent, the backstop logs "could not check" once and does nothing.
  §10 A4's `read_percentage_action` and its tests are deleted. An optional,
  alert-only consistency check (`backstop.read_upower_conf_percentage_action`)
  parses `/etc/UPower/UPower.conf` + `UPower.conf.d/*.conf` (later files
  win) and flags a mismatch against `backstop_action_pct` -- never a second
  trigger source, and a parse failure never disables the backstop.
- **§11 R3 (BLOCKER, standing check):** UPower is still watched, just never
  trusted alone. A low-rate (`UPowerCache`, ~10s) poll of `DisplayDevice`
  `Percentage`/`EnergyFull`/`EnergyFullDesign` feeds `recovery.upower`
  (A23) every tick and, once divergent (>5 points, OR energy ratio >1.2x)
  for >=60s continuously, records one `upower_divergent` event per boot +
  a notification. This never feeds back into the trigger.
- **§11 R4:** `backstop_hibernate` is recorded ONLY on a successful
  `hibernate_fn()`; a failed call records `backstop_refused`
  (`reason="hibernate_call_failed"`) instead and does NOT consume the
  per-boot `backstop_hibernate` cap.
- **SHOULD 1 (review):** `PreparingForSleep` reading `None` (could not
  check) REFUSES (`reason="preparing_for_sleep_unknown"`), never proceeds
  as though it were clear.

If `Discharging` and `pct <= backstop_action_pct − 2` (sysfs) holds for 20
consecutive samples, and logind's `PreparingForSleep` is exactly `False`,
hwmon calls logind `Hibernate` — refusing instead (recording
`backstop_refused`, once per boot) when a `block` sleep inhibitor is
present (the existing A13 `power_guard`), `PreparingForSleep` is
unreadable, or `CanHibernate` is not exactly `"yes"` (the last two read
fresh at the moment the threshold is crossed, never cached). At most one
`backstop_hibernate` per boot (checked against `events`, like A19); at most
one ATTEMPT (fire or refuse) per continuous low-battery episode — the
20-sample counter must reset (pct rises out of the danger zone) and cross
the threshold again before a second attempt in the same boot.

**A21 — Post-crash triage (R-L3.1, amended by §10 A10c).** After
`hwmon.service` records a new `hard_poweroff` or `unclean_shutdown` event
(A17), hwmon runs one read-only triage, once (naturally deduped — it only
runs for events A17's own `find_new_events` reports as genuinely new), and
sends one desktop notification (`notify-send`, skipped entirely if absent).
Checks: (1) the last `[ALPM]` block in `/var/log/pacman.log` inside the
**crashed** boot's own time window (found from the event's `ts_start`
against the boot list — NOT the event's own `boot_id`, which per A17/B1 is
the *recovery* boot that starts after the gap) — `transaction started`
with no later `transaction completed` anywhere in the log; (2)
`/var/lib/pacman/db.lck` present with no `pacman` process running
(`pgrep -x pacman`, never `-f`/`-a`); (3) that (recovery) boot's
`systemd-fsck` lines, quoted verbatim, never pattern-matched; (4) that
boot's kernel `BTRFS` lines at warning-or-worse, quoted verbatim; (5) for
an interrupted transaction, a generic instruction to boot the newest
pre-update snapshot from the Limine *Snapshots* menu — never a snapshot
NUMBER, since listing root snapshots needs root (this session doesn't have
it; the hook to plug in a privileged reader exists and is exercised only by
a test). `pacman.log`'s explicit numeric UTC offset is parsed and converted
before any comparison against the journal's own clock
(`feedback_two_clocks`). Stored as one `triage` event with a JSON report
(severity `info`/`action`/`unknown`).

**A22 — `journal_unavailable` (R-L3.1's journal-failure counter).** After
3 consecutive `journalctl --list-boots` failures (the same call A17's
events check already makes at daemon start), hwmon records one
`journal_unavailable` event and resets the streak (so a persistently broken
journal records one event per 3-failure streak, not one per failure). Any
success resets the streak to 0. `feedback_absent_is_not_zero`: three
states, not two — a failed journal/pacman.log read anywhere in A21's triage
makes that check's `..._ok`/lines field `None`/`null` and pushes the whole
report's severity to `unknown`, which is reported as "could not check",
never silently as "clean".

**A23 — `recovery` snapshot key (R-L4.1, `upower` sub-key added by §11
R3).** New key `recovery`:
`{home_snapshot_state: "not_configured"|"unknown"|"fresh"|"stale",
home_snapshot_age_s: float|null, upower: {state: "ok"|"divergent"|"unknown",
upower_pct: float|null, sysfs_pct: float|null}}`. `home_snapshot_state`/
`_age_s` source: `snapper --csvout --utc -c home list --columns
number,date` (`recovery.py`), queried at most once per 60 s
(`recovery.RecoveryCache`, mirroring `InhibitorCache`). `not_configured` —
checked by `/etc/snapper/configs/home`'s FILE existence, never by
`snapper`'s exit code — is the state before that config exists (the
default on this machine today; that file is out of this repo's scope, D4)
and MUST NOT read as stale. `unknown` is the config-exists-but-unreadable
state (measured live 2026-09-23: even the pre-existing `root` config
returns "No permissions." as `techno`). `stale` is `age_s > 7200`.
`upower` is A20/§11 R3's INSTANTANEOUS standing-check reading (not the
60s-sustained `upower_divergent` event) -- `unknown` when either
percentage is unreadable; positive controls measured the same day: 33 vs
3.27 -> `divergent`; 49 vs 47.3 -> `ok`.

## MODIFIED

- **`hwmon` events table**: new `kind` values `backstop_hibernate`,
  `backstop_refused`, `journal_unavailable`, `triage`,
  `critical_battery_marker`, `upower_divergent` (all share the existing
  `events` schema — no column changes; `triage`'s JSON report lives in the
  existing `detail` column). `Store` gains `has_event_kind_for_boot(kind,
  boot_id)` (the per-boot dedupe A19/A20/A23 use) and a small generic
  `meta(key, value)` table (A22's failure streak).
- **`hwmon` snapshot**: schema 2 → 3, adding `recovery` (A23, including its
  `upower` sub-key added by the same-day §11 fix pass). The fixture led the
  code both times: `tests/fixtures/latest.example.json` and
  `test_snapshot_shape.py`'s new cases were committed first and were red
  (`recovery: unexpected key` / drift-guard failure, and again for
  `recovery.upower`) until `snapshot.py`'s `_REFERENCE_SNAPSHOT` and
  `build_snapshot()` were updated to match — same method as v3's `3680f1c`.
- **`hwmon/config.py`**: `backstop_action_pct` key added alongside
  `hibernate_backstop`; `ConfigCache` added (SHOULD 2, TTL-cached, ~10s).
- **`hwmon/backstop.py`**: `read_percentage_action` DELETED (§11 R2 --
  `PercentageAction` is not a real D-Bus property); `HibernateBackstop.evaluate()`'s
  `percentage_action` parameter renamed `action_pct` and now sourced from
  hwmon config, never UPower; `UPowerCache` now caches `{pct, energy_full,
  energy_full_design}` (was `(pct, percentage_action)`) and is used ONLY
  for the §11 R3 standing check, never the trigger.

## REMOVED

Nothing.

## Acceptance (v4) — observed, each with a positive control

16. WHEN `Discharging && pct <= critical_pct` first holds in a boot THEN
    one `<4>hwmon: critical battery N%` line + one `journalctl --user
    --sync` fire, and one `critical_battery_marker` event is recorded;
    **positive control:** at `pct` one point above the threshold, nothing
    fires. WHEN the daemon restarts mid-boot with the marker already
    recorded THEN it does not fire (or sync) again.
17. WHEN `hibernate_backstop` is absent/false in config THEN the backstop
    never even reads UPower (proven: an injected UPower read records zero
    calls). WHEN true, `backstop_action_pct` is set, and 20 consecutive
    SYSFS samples cross `backstop_action_pct − 2` THEN `Hibernate` is
    called using ONLY sysfs (proven: an injected, wildly different UPower
    reading never affects the outcome) and one `backstop_hibernate` event
    is recorded only if `hibernate_fn()` reports success (§11 R4);
    **positive control (no per-sample retries):** 50 further samples in
    the same episode fire nothing more. WHEN `hibernate_fn()` fails THEN
    `backstop_refused` (`hibernate_call_failed`) is recorded instead and
    the per-boot cap is NOT consumed. WHEN `backstop_action_pct` is absent
    while enabled THEN "could not check" is logged once (not per tick) and
    nothing acts. WHEN a `block` sleep inhibitor is present, or
    `PreparingForSleep` is unreadable (SHOULD 1), at the trigger THEN
    `backstop_refused` is recorded instead and `Hibernate` is never called;
    a later episode, inhibitor cleared, may still fire.
18. WHEN triage runs against the real `poweroff_2026-09-23/` fixture THEN
    severity is `info` (fsck dirty-bit line quoted, no interrupted
    transaction, no stale lock); **positive control:** a synthetic
    `pacman.log` with a `transaction started` and no `transaction
    completed` inside the crashed boot's window yields severity `action`
    and the generic snapshot hint (never a number).
19. WHEN `journalctl --list-boots` fails 3 times in a row THEN one
    `journal_unavailable` event is recorded; **positive control:** 2
    failures then a success resets the streak, so 2 more failures do not
    reach a 4th event.
20. WHEN no `/etc/snapper/configs/home` file exists THEN
    `recovery.home_snapshot_state == "not_configured"` (never `"stale"`);
    **positive control:** the file present but the command failing (or
    unparseable) yields `"unknown"`, and a snapshot 1h/3h old yields
    `"fresh"`/`"stale"` respectively (boundary at exactly 2h is `"fresh"`).
21. Checks 0–15 still pass against schema 3.
22. WHEN UPower percentage 3.27 and sysfs capacity 33 are fed to the §11 R3
    divergence check THEN `recovery.upower.state == "divergent"`;
    **positive control (today's earlier, healthy reading):** 47.3 vs 49
    yields `"ok"`. WHEN divergent for >=60s continuously THEN one
    `upower_divergent` event is recorded per boot + one notification;
    **positive control:** a single non-divergent sample resets the streak.
    An `energy_full/energy_full_design` ratio >1.2 (measured 9.96x) is
    `"divergent"` even when the two percentages happen to agree.
23. WHEN `backstop_action_pct` and `UPower.conf`'s `PercentageAction`
    disagree THEN one alert (log + notification), not per tick;
    **positive control:** matching values never alert, and a `UPower.conf`
    parse failure alerts nothing (and does not touch the backstop's own
    trigger — proven by the same run completing without the test's
    failing `hibernate_fn` firing).

## v4 AS EXECUTED — 2026-09-23

Implemented: `hwmon/config.py`, `hwmon/critical_marker.py`,
`hwmon/backstop.py`, `hwmon/triage.py`, `hwmon/recovery.py`; `sensors.py`
gained `read_boot_id`; `store.py` gained `has_event_kind_for_boot` and the
`meta` table; `daemon.py` wires all five in (each new external call —
`sync_journal_fn`, `hibernate_fn`, `*_notify_fn`, `triage_runner`,
`config_loader`, `upower_cache`, `hibernate_backstop`, `recovery_cache` — is
injectable, defaulting to the real thing; no test in the suite calls a real
hibernate). 352 Python tests (up from 229 before this delta). Not run live
on the collector (no deploy in this delta — `install.sh`/`hwmon.service`
untouched, per scope). `hibernate_backstop` confirmed structurally
default-off; never exercised against real UPower/logind D-Bus objects
(`busctl` calls are shape-tested against the exact JSON `inhibitors.py`
already measured live, per S1, but not independently re-measured for the
three new property/method names in this delta).

## v4 fix pass AS EXECUTED — 2026-09-23 (§11 M1/R1-R4 + review SHOULDs, before ship)

Same-day fix pass, applied before this delta was ever deployed (§10 A4 was
built and immediately found unsafe by the build session's own measurements
-- see §11). Changed: `backstop.py` (`read_percentage_action` deleted;
`UPowerCache` redesigned around `{pct, energy_full, energy_full_design}`;
`in_danger_zone`/`HibernateBackstop.evaluate()` take sysfs `pct` + config
`action_pct`, never UPower; `read_upower_conf_percentage_action`,
`is_config_mismatched`, `compute_upower_divergence`, `divergence_sustained`,
`DivergenceState`, `ConfigWarningState` added); `config.py`
(`backstop_action_pct` key, `ConfigCache`); `snapshot.py`
(`recovery.upower`, still schema 3 -- this fix pass landed before v4 shipped,
so it revises the same in-flight delta rather than opening a v5); `daemon.py`
(`_run_backstop_tick` rewritten for R1/R2/R4, `_run_upower_divergence_tick`
and `_maybe_alert_config_mismatch` added, `_OncePerPoll` rate-limiter added).

Test hygiene (review SHOULD 3): every `daemon.run()` call across
`test_daemon.py`, `test_daemon_v3.py`, `test_critical_marker.py`,
`test_backstop.py`, `test_triage.py` now pins `XDG_CONFIG_HOME` to a temp
dir and injects a `hibernate_fn` that raises `AssertionError` if ever
called, plus a quiet `UPowerCache` fake (§11 R3 runs every tick regardless
of `hibernate_backstop`, so it would otherwise attempt a real `busctl` call
in every one of those files).

352 → 400 Python tests, all passing (`python3 -m unittest discover -s
tests -t .`). Mutation proof: reverted §11 R4 (recorded `backstop_hibernate`
unconditionally, ignoring `hibernate_fn()`'s return value) — exactly
`test_failed_hibernate_call_records_refused_not_hibernate_and_does_not_consume_cap`
went red; restored and re-confirmed green.

Not exercised live: same caveat as above, plus the three new §11 R3
property names (`EnergyFull`, `EnergyFullDesign`) and the
`UPower.conf`/`conf.d` consistency parser (no `home` config or drop-in
exists yet to read for real).

## v4 fix pass round 2 AS EXECUTED — 2026-09-23 (review BLOCKED; live shapes measured)

Round-2 review blocked the fix pass above; the main session verified the
blockers live before this round started. Measured live 2026-09-23:
`busctl --system -j call org.freedesktop.login1 /org/freedesktop/login1
org.freedesktop.DBus.Properties Get ss org.freedesktop.login1.Manager
PreparingForSleep` -> `{"type":"v","data":[{"type":"b","data":false}]}`;
the same `Get` for UPower `DisplayDevice`'s `EnergyFullDesign` ->
`{"type":"v","data":[{"type":"d","data":0.0}]}`; `CanHibernate` ->
`{"type":"s","data":["yes"]}`.

1. **BLOCKER, fixed:** `backstop._dbus_get_property` unwrapped only the
   OUTER `data[0]` -- `Properties.Get`'s "v" out-parameter is ITSELF a
   variant, so the real shape nests a second `{"type","data"}`. Every
   property read through it (`read_preparing_for_sleep`, the UPower
   percentage/energy readers) silently returned `None` on a healthy bus.
   Fixed with a second unwrap; live-shape tests added using the exact
   fixtures above; mutation-proved (see below). Ran the fixed readers live:
   `read_preparing_for_sleep() -> False`, `read_can_hibernate() -> "yes"`,
   `find_battery_device_path() -> "/org/freedesktop/UPower/devices/battery_BAT0"`,
   `read_battery_upower_properties() -> {"pct": 7.05, "energy_full": 722.70,
   "energy_full_design": 72.58}` (sysfs `capacity` was 70 at the same
   moment -- confirms the §11 M1 ~10x UPower fault is STILL live on this
   machine).
2. **BLOCKER, fixed:** `config._validate_action_pct` now rejects `bool`,
   non-finite (`Infinity`/`-Infinity`/`NaN` -- `json.loads` accepts these as
   an extension), and anything outside `(0, 100]`, logging one warning line
   per rejection; `backstop_action_pct` stays `None` (§11 R2's "no default
   that enables anything") on any rejection.
3. **Fixed:** the standing check (R3) now reads `Percentage`/`EnergyFull`/
   `EnergyFullDesign` from the REAL battery device
   (`/org/freedesktop/UPower/devices/battery_BAT0`), discovered via
   `EnumerateDevices` rather than hard-coded -- `DisplayDevice`'s
   `EnergyFullDesign` was measured live to be `0.0`. `compute_upower_divergence`
   now yields `"unknown"` (not a silent `"ok"`) when the energy ratio's
   denominator is exactly `0` and the pct signal alone doesn't already say
   `"divergent"`.
4. **Fixed:** `_maybe_alert_config_mismatch` only re-reads `UPower.conf`
   when the config loader's own `poll_count` changes (falls back to every
   tick for a loader with no `poll_count`, e.g. a test's plain lambda).
5. **Fixed:** `backstop.DivergenceState.notified_without_boot_id` -- a
   per-process latch for when `boot_id` is `None`, since
   `has_event_kind_for_boot(kind, None)` is a SQL `boot_id = NULL`
   comparison that never matches, which is what sent the round-2 reviewer a
   real desktop notification on every tick.
6. **Fixed:** `HibernateBackstop.reset()`, called by `daemon.py` every tick
   `hibernate_backstop` is off, so toggling it back on starts a fresh
   20-sample count.
7. **NIT, fixed:** `UPowerCache` now does one `Properties.GetAll` per
   device instead of three separate `Get`s.
8. **Fixed:** every `daemon.run()` test across `test_daemon.py`,
   `test_daemon_v3.py`, `test_critical_marker.py`, `test_backstop.py`,
   `test_triage.py` now injects no-op `backstop_notify_fn`/`triage_notify_fn`
   (a real `notify-send` reached the round-2 reviewer's desktop from an
   uninjected test run).

Public-repo hygiene: the coordinator replaced the remaining username
references (`as \`techno\`` in three comments, `ALLOW_USERS=techno` in
`test_recovery.py`) with generic placeholders before this round; preserved
here, not reintroduced.

420 Python tests, all passing. Mutation proof (fix 1): removed the second
variant unwrap in `_dbus_get_property` -- `test_percentage_double_wrapped_variant_parses`,
`test_energy_full_design_real_measured_shape_on_display_device_is_zero`,
`test_preparing_for_sleep_true_real_measured_shape`, and
`test_single_wrapped_variant_is_rejected_not_misread` all went red; restored
and reconfirmed green.

## v4 AS EXECUTED — deploy (2026-09-24, released as v1.2.0)

Widget half built on the branch (schema 2 → 3 in `Format.js`, RECOVERY rows,
`recoveryLevel` in `worstLevel`). Found while building it: **the JS suite was
already red on the branch** (the fixture moved to schema 3 and the widget
rejected it). Deploying the Python half alone would have left the bar on a
permanent `hwmon —`. Pre-deploy: widget review SHIP-WITH-FIXES (the new
formatters were missing from the render sweep; an edit meant to add them had
never applied, and its anchor was not asserted), advisor GO-WITH-CONDITIONS,
and a live isolated smoke test of the new collector (0.81 % of a core against
0.98 % for v1.1.0 over the same 160 s; backstop enabled, 0 hibernate attempts;
it found `recovery.upower.sysfs_pct` typed int in the reference, now float on
both paths).

Verified **live** after `install.sh` + collector restart + `omarchy restart shell`:
- widget IPC `{"stale":false,"age_s":0.4}` from the NEW shell process. That is
  the plugin-load proof: a fresh shell start logs no per-plugin load line, only
  hot-reload does, so the close-out's "load line must be present" check cannot
  apply after a shell restart.
- stale path: collector stopped 6 s → `hwmon —`, `stale:true`; live again < 1 s after start.
- `hwmon --json`: schema 3, `recovery.upower.state = ok` (98.75 vs 98.82, which
  proves the D-Bus readers work inside the service), `home_snapshot_state =
  not_configured`; installed package and plugin byte-identical to the repo;
  `techno.hwmon` once, before `omarchy.power`; no QML warnings naming it.
- popup screenshot: System page shows RECOVERY → "Home snapshots: not set up",
  "UPower vs battery: agrees · 98.9 % vs 98.9 %"; nothing truncated.
- pre-drain: critical action `Hibernate`, `CanHibernate` yes, `sleep_blocked`
  false, swapfile 0 B used, no pending image, backstop config armed (5).

Only in tests: the backstop firing path, triage after a real hard power-off,
`upower_divergent` after 60 s. The first real battery drain started
immediately after this deploy.

---

# v5 delta — 2026-09-24 (NAS backup freshness, R-N7/A-S5)

Scope: `~/deliverables/omarchy-power-loss-recovery/SPEC-nas-backup.md`
requirement **R-N7**, as superseded by its own **§9 A-S5** (the transport
changed from NFS to `rest-server --append-only`, but hwmon's slice is
unaffected either way: it reads only the status file a separate job writes,
never the repo, the mount or the network). That writer job, the systemd
timer, and the `restic` invocation are **out of this repo's scope** (a
different repo/agent, exactly like v4's snapper `home` config was) — this
delta covers only what hwmon itself reads and displays. Not deployed by
this delta: no `install.sh` change, no version bump, no `systemctl`.

**Contract:** `schema` becomes **4**. `tests/fixtures/latest.example.json`
is updated first (schema 4, adding `recovery.nas_backup`) and both halves
test against it, as in v3/v4 — the widget treats any schema other than 4 as
not-live (existing A9 rule), so the collector and the bar widget ship
together. As with v4, the fixture led the code: `test_snapshot_shape.py`'s
new cases and the fixture's schema bump were committed first and were red
(`recovery.nas_backup: unexpected key`, `test_schema_is_4`) until
`snapshot.py`'s `_REFERENCE_SNAPSHOT`/`SCHEMA_VERSION` were updated to
match.

## ADDED

**A24 — `hwmon/nas_backup.py` (R-N7).** Reads the status file the backup
job writes: `$XDG_STATE_HOME/omarchy-recovery/nas-backup.json`, falling
back to `~/.local/state/omarchy-recovery/nas-backup.json` (XDG default)
when the variable is unset. Contract as handed to this module: `{ts, iso,
result: ok|skipped|failed, reason, snapshot_id, bytes_added, files_new,
files_changed, source_snapper, duration_s, last_ok_ts}` — this module reads
only `result`, `reason`, `ts` and `last_ok_ts`; the rest is the writer's own
bookkeeping. Four states, matching `feedback_absent_is_not_zero`:

- `not_configured` — the status file does not exist (checked by file
  existence, never a parse failure). MUST NOT read as stale.
- `unknown` — the file exists but is unreadable, not valid JSON, not a JSON
  object, or its `result` is missing/not one of `ok`/`skipped`/`failed`.
- `failed` — the file's own last-recorded `result` is `failed`; `reason` is
  carried through. **`skipped` alone never produces this state** — it only
  lets `age_s` grow toward `stale` (task requirement, verbatim).
- `fresh` (`age_s <= 72h`) or `stale` (older, OR there has never been a
  recorded `ok` while the file exists — "no ok ever" is stale, not a
  guessed fresh).

`age_s` is computed from `last_ok_ts` when present; an `ok` row without its
own `last_ok_ts` (yet) falls back to that row's own `ts` — an `ok` result
**is** a last-ok event. Cached via `NasBackupCache` (mirrors
`recovery.RecoveryCache` exactly: monotonic clock, `ttl_s=60` default,
injectable `read_fn`), reading the one small local file at most once per 60
s — never the network, never a mount, matching A-S5.

**A25 — `recovery.nas_backup` snapshot key (schema 4).** New sub-key under
`recovery`: `{state, age_s, reason}`. Wired in `daemon.py` exactly like
`recovery.upower` (§11 R3's fix pass): `daemon.run()` gains an injectable
`nas_backup_cache` parameter (default `nas_backup.NasBackupCache()`), and
each tick merges `recovery_state["nas_backup"] = nas_backup_cache.get()`
into the `recovery` dict passed to `snapshot.build_snapshot()`. `_NULL_RECOVERY`
gains the same `not_configured` default so a snapshot built with no
`NasBackupCache` involved (most tests) never looks stale/red by accident.

## MODIFIED

- **M9 (`recovery.py`, small related fix).** `home_snapshot_state` gains a
  fourth state, **`empty`**: the config exists and `snapper` ran
  successfully (a CSV with the expected `date` column), but every data
  row's date field is blank — measured live 2026-09-24, `snapper -c home
  list` shows only the synthetic "0"/current row before any snapshot has
  been taken. Distinguished on purpose from `unknown` (a real parse
  failure: no `date` column at all, or a data row with a non-blank but
  unparseable date) via a new pure helper, `_csv_all_dates_blank()`. Level:
  normal (shown in the popup, never raised in the bar), same as
  `unknown`/`not_configured`.
- **`hwmon` snapshot**: schema 3 → 4, adding `recovery.nas_backup` (A25).
- **Widget** (`Format.js`): `SCHEMA` 3 → 4. New `nasBackup(snapshot)`
  formatter: `"<age> ago"` (fresh) / `"STALE · <age> ago"` (stale) /
  `"FAILED · <reason>"` or `"FAILED"` with no reason / `"not set up"`
  (not_configured) / `"could not check"` (unknown). `homeSnapshots()` gains
  the `empty` branch → `"set up, none yet"`. Both formatters added to
  `js_tests.mjs`'s `renderAll()` null/missing sweep (memory lesson: assert
  the edit applied — confirmed via `grep` before running the suite, since a
  prior session's edit to this exact sweep silently failed to apply).
- **Widget** (`Thresholds.js`): `recoveryLevel()` also warns when
  `recovery.nas_backup.state` is `stale` or `failed`; `unknown` /
  `not_configured` / `empty` stay normal.
- **Widget** (`SystemPage.qml`): RECOVERY section gains a "NAS backup" row
  under "UPower vs battery", reading `Format.nasBackup(snapshot)`.

## REMOVED

Nothing.

## Acceptance (v5) — observed, each with a positive control

24. WHEN the status file's last `ok` (`last_ok_ts`) is back-dated 73 h THEN
    `recovery.nas_backup.state == "stale"` and the widget's `recoveryLevel`
    is `warn`; **positive control:** exactly 72 h is `"fresh"`, not
    `"stale"`.
25. WHEN the last recorded `result` is `failed` THEN
    `recovery.nas_backup.state == "failed"` and `reason` is carried
    through into the widget text (`"FAILED · <reason>"`); **positive
    control:** a `skipped` row with the same old `last_ok_ts` reads
    `"stale"` (from age), never `"failed"` — mutation-proved (§ below).
26. WHEN the status file does not exist THEN
    `recovery.nas_backup.state == "not_configured"` (never `"stale"`, never
    a warn); **positive control:** the file present but unreadable /
    invalid JSON / missing a required key yields `"unknown"`.
27. WHEN `/etc/snapper/configs/home` exists and `snapper -c home list`
    returns only the synthetic current row (blank date) THEN
    `recovery.home_snapshot_state == "empty"` (level normal); **positive
    control:** a missing `date` column, or a non-blank unparseable date,
    still yields `"unknown"`.
28. Checks 0–23 still pass against schema 4.

## v5 AS EXECUTED — 2026-09-24

Implemented: `hwmon/nas_backup.py` (new); `daemon.py` gained
`nas_backup_cache` (defaults to a real `NasBackupCache()`, matching every
other injectable in this module — no test in the suite ever points it at
the real host status file except via an explicit temp-dir `state_path`);
`recovery.py` gained `STATE_EMPTY`/`_csv_all_dates_blank()`; `snapshot.py`
`SCHEMA_VERSION` 3 → 4, `_REFERENCE_SNAPSHOT`/`_NULL_RECOVERY` updated.
Widget: `Format.js` (`SCHEMA`, `nasBackup()`, `homeSnapshots()`'s `empty`
branch), `Thresholds.js` (`recoveryLevel()`), `SystemPage.qml` (new row).

Both suites green: 465 Python tests (up from 425) and 656 Node tests (up
from 615, `plugin/js_tests.mjs`). Mutation proof, one per rule, each
reverted after confirming red:

- "`skipped` alone never sets `failed`"
  (`test_skipped_alone_never_sets_failed` + 2 others) — reverted by making
  `compute_nas_backup_state` also treat `skipped` as `failed`.
- "`empty` is distinct from `unknown`"
  (`test_config_exists_only_synthetic_current_row_is_empty_not_unknown` + 1
  other) — reverted by deleting the `_csv_all_dates_blank()` branch in
  `compute_recovery_state`.
- "NAS backup stale/failed reaches the widget's warn level"
  (`nas backup stale -> warn`, `nas backup failed -> warn`, `nas backup
  failed reaches worstLevel`) — reverted by dropping the `nasState`
  disjunct from `Thresholds.recoveryLevel()`.
- The `renderAll()` sweep edit (`F.nasBackup(s)`) was `grep`-verified
  present in `js_tests.mjs` before the suite was trusted (the specific
  lesson from v4's deploy: an edit meant to add a formatter to this exact
  sweep silently failed to apply once already).

Not exercised live: no real status file exists yet (the writer job is a
separate agent's scope, not yet built); `NasBackupCache`'s defaults were
only run against a real, absent `~/.local/state/omarchy-recovery/
nas-backup.json` on this machine, which correctly reads `not_configured`.
No deploy in this delta (no `install.sh` run, no version bump, no
`systemctl`) — left for review on branch `nas-backup`, uncommitted.

## v5 amendment — 2026-09-24 (contract v2: `last_attempt_*`, a `never` state)

**Committed as `fddcd3e`; this amendment continues on top, uncommitted**
(per the Authority Map: correct by appending a dated note, never by editing
the shipped version away). Reason, measured by the main session against
the job's real files: the sequence `ok → failed → skipped(on-battery)`
read as `"fresh"` under the A24 contract above — `compute_nas_backup_state`
only ever looked at the CURRENT row's `result`, which was `skipped`, and
`skipped` alone correctly never set `failed`, but nothing else was carrying
the failure forward either, so it silently vanished within one hourly
tick.

**A26 — contract v2 fields.** The backup job now also writes
`last_attempt_ts`, `last_attempt_result` (`ok`\|`failed`\|`null`), and
`last_attempt_reason`, describing the most recent NON-skipped attempt,
carried forward across `skipped` rows exactly like `last_ok_ts` already
is. `failed` is now driven by `last_attempt_result == "failed"` when the
key is present; `age_s` is unchanged (still always from `last_ok_ts`, or
an `ok` row's own `ts` as fallback). **Backward compatible by key
presence, not value:** when `last_attempt_result` is absent from the JSON
entirely (an older job file), classification falls back to the CURRENT
row's own `result`/`reason` — the exact A24 behaviour, unchanged for that
file shape.

**A27 — a `never` state.** A file can exist with no `ok` ever recorded
AND no failed attempt behind it either (e.g. every run so far has been
skipped on battery, from day one). The old code reported this as `stale`
with `age_s: null`, which the widget rendered as `"STALE · – ago"` — a
bare dash reading as a data problem, not as the real risk it is. Now a
distinct state, `"never"`, shown as `"not backed up yet"` and warned in
the bar exactly like `stale`/`failed`. **Priority: `failed` beats
`never`** — a first-ever attempt that fails (no `ok` ever, but there IS a
concrete failure to report) reads as `failed`, not `never`; `never` is
reserved for "nothing has happened yet that's worth naming."

## MODIFIED (amendment)

- `hwmon/nas_backup.py`: `compute_nas_backup_state()` reworked per A26/A27
  (new `STATE_NEVER`, new `_as_timestamp()`/`_as_optional_str()` helpers,
  presence-gated `last_attempt_result` branch). Docstring rewritten for six
  states (was four).
- `Format.js` `nasBackup()`: new `"never"` branch → `"not backed up yet"`;
  comment updated to explain why `failed` now reflects the last
  NON-skipped attempt, not the current row.
- `Thresholds.js` `recoveryLevel()`: also warns on `nas_backup.state ==
  "never"`.
- No snapshot shape change — `{state, age_s, reason}`'s field TYPES are
  unchanged (`state` is still a string; `validate_shape` checks type, not
  enum), so schema stays 4. Added a documentation-pinning shape test,
  `test_recovery_nas_backup_state_never_is_valid_shape`.

## Acceptance (v5 amendment)

29. WHEN a status file reads `ok → failed → skipped(on-battery)` (the
    measured regression) THEN `recovery.nas_backup.state == "failed"` with
    the failed run's `reason`, and `age_s` still comes from `last_ok_ts`
    (the last known-good backup), not from the failure; **positive
    control:** `last_attempt_result == "ok"` on the same shape does NOT
    force `failed` — normal `fresh`/`stale` aging applies.
30. WHEN a status file has no `last_attempt_result` key at all (a v1 job
    file) THEN classification falls back to the current row's own
    `result`/`reason`, unchanged from A24; **positive control:** a
    `skipped` row in this shape cannot see a failure several rows back —
    documents the boundary of the fallback, not a bug in it.
31. WHEN no `ok` has ever been recorded and the most recent non-skipped
    attempt (if any) did not fail THEN `state == "never"`, shown as "not
    backed up yet" and warned; **positive control:** a first-ever attempt
    that DID fail, with no ok ever either, yields `"failed"`, not
    `"never"` — failed takes priority.
32. Checks 0–28 still pass with the reworked classification.

## v5 amendment AS EXECUTED — 2026-09-24

Implemented as described above. Both suites green: **473 Python tests**
(up from 465; +8 in `LastAttemptFieldsTests` plus 3 existing "no ok ever"
tests renamed/retargeted from `stale` to `never`) and **660 Node tests**
(up from 656). Mutation proof, one per new rule, each reverted after
confirming red:

- "`failed` reflects the last NON-skipped attempt, not the current row" —
  reverted the presence-gated `last_attempt_result` branch back to
  `result == RESULT_FAILED` unconditionally; `test_failed_attempt_survives
  _a_later_skip` and `test_non_string_last_attempt_reason_is_dropped_not_
  fatal` went red.
- "`never` is warned like `stale`/`failed`" — dropped the `nasState ===
  "never"` disjunct from `Thresholds.recoveryLevel()`; `nas backup never
  -> warn` and `nas backup never reaches worstLevel` went red.

Not exercised live: same caveats as the base v5 delta above — no real
status file exists yet on this machine, and the job agent's promised
`samples-v2/` fixtures had not appeared in the scratch directory by the
time this amendment was finished (checked; none to run).

---

# v6 — release 1.3.0 (DRAFT, 2026-09-25) · delta

**What ships:** branch `nas-backup` (`fddcd3e` v5 + `84903fd` v5 amendment + `45e0ab3` RUNBOOK.md), plus the release edits below. **Nothing new is built;** this delta covers release, deploy and live acceptance only. Owner decision (2026-09-25): "start v1.3.0 spec with the experts and advisor".

**Measured before the release (2026-09-25):** installed v1.2.0, live snapshot `schema 3`; the backup status file exists (`result=skipped`, `last_attempt_result=ok`: the timer ran and a backup was not due); `/home` snapper has snapshots (hourly since 2026-09-24 19:00); `node plugin/js_tests.mjs` 660/0, Python 474 OK on the branch.

## ADDED

**R1 — Version 1.3.0 in the three places that must agree** (`hwmon/__init__.py`, `plugin/techno.hwmon/manifest.json`, `CHANGELOG.md` with a `## 1.3.0` section folding v5 + amendment + RUNBOOK).

**R2 — README corrections (public).** Model year: the owner says **13-inch Retina, Mid 2014** (README line 3 "a 2013 MacBook Pro", line 54 "Late 2013" are wrong). Contract `schema 3` → `4` (README.md:148, plugin/README.md:8). Status block → v1.3.0. Link RUNBOOK.md. Mention the optional companion `omarchy-laptop-recovery` as "coming" (not yet public).

**R3 — Deploy order across a schema bump** (lesson 2026-09-24: the widget accepts exactly one schema). Merge `nas-backup` → `master` (fast-forward), then `/hwmon-update`: both suites → `install.sh` (restarts the collector, waits ≤ 10 s for schema 4, then stages the plugin) → `omarchy restart shell` (hot-reload keeps cached popup QML).
- **WHEN** install.sh's schema wait fails **THEN** the machine is half-upgraded (new collector, old widget = "hwmon —"): roll back with `git checkout v1.2.0 && ./install.sh && omarchy restart shell` and stop.

## Acceptance (live, each with its must-fail control, pre-committed before the deploy)

| # | check | expected (live) | control that must go red / the other way |
|---|---|---|---|
| A1 | `omarchy-shell techno.hwmon state` from the NEW shell pid | `stale:false`, `age_s` < 2 | stop the collector 6 s → `stale:true`, `hwmon —`; recovers ≤ 3 s |
| A2 | `hwmon --json` | `schema 4`; `recovery.nas_backup.state == "fresh"`, `age_s` ≈ now − last_ok_ts; `home_snapshot_state == "fresh"` | (tests only) back-dated 73 h → `stale` → bar warns; decided in review whether a live control is worth touching the real status file |
| A3 | popup System page screenshot | RECOVERY shows three rows: Home snapshots (age), UPower vs battery (agrees · x % vs y %), NAS backup (age); nothing truncated | — |
| A4 | journal since deploy | no QML warnings naming techno.hwmon; no per-second spam | the plugin must answer IPC (a fresh shell logs no load line) |
| A5 | per-tick cost | collector CPU over ≥ 90 s ≈ v1.2.0's ~0.9 % of a core | — |
| A6 | installed == repo | `diff -r` package + plugin | — |
| A7 | public scrub before GitHub | scrub regex prints nothing | a planted lab-hostname line, piped in, matches |

## Out of scope
The kit's public release (`omarchy-laptop-recovery`) is a separate step. No widget for the escrow drill or the quarterly prune. No live backstop firing test (tests only, as in v4).

## v6 advisor amendments — 2026-09-25 (APPROVE-WITH-CHANGES)

Advisor (Fable, `system-architect`), weighing a UX review and a monitoring
review. **Owner decisions folded in:** the four RECOVERY strings shipped as
`8261e84` (Format.js + js_tests.mjs + RUNBOOK table, both suites green), and
the live A2 stale control is DROPPED. Staleness stays covered by
`tests/test_nas_backup.py` (72 h fresh / 73 h stale); dropping it was right, since
the proposed command would not even parse (`--state-dir`/`--db` must precede
`daemon`), and without isolation it would have written the real DB and read the
real ARMED backstop config. Nothing else parses those strings (`stateJson` =
`{label, stale, age_s}`; the CLI never prints `recovery`; `Thresholds.js` keys on
state enums).

**R2 (added):** `RUNBOOK.md` lines 5–6 → "released as 1.3.0 (schema 4)". README
"Layout" block: spec v2–v6, schema 4, current test counts; the "System" bullet
gains RECOVERY. Commit this spec BEFORE the merge.

**R3 (amended order):** commit spec → `git merge --ff-only nas-backup` on master
→ both suites → `./install.sh` (it restarts the collector itself) → **post-copy
gate** → `omarchy restart shell` → A1–A7 → tag/push/release. Rollback:
`git checkout v1.2.0 && ./install.sh && omarchy restart shell`, then
`git checkout master`. It is DB-safe: 1.3.0 has no migration and `store.py` is unchanged.
The release notes are scrubbed with the same regex before `gh release create`.

Post-copy gate (between install.sh and the shell restart):
`diff -r ~/dev/hwmon/plugin/techno.hwmon ~/.config/omarchy/plugins/techno.hwmon && grep -q 'nasBackup(' ~/.config/omarchy/plugins/techno.hwmon/Format.js && echo staged=ok`
Must-fail proof, observed 2026-09-25 pre-deploy: three files differ and the gate fails.

## Acceptance (v6, final) — thresholds pre-committed

| # | check | expected (live) | control that must go red / the other way |
|---|---|---|---|
| A1 | `omarchy-shell techno.hwmon state` | `stale:false`, `age_s` < 2 | stop the collector 6 s → `stale:true`, `hwmon —`; recovers ≤ 3 s |
| A2 | `hwmon --json` | `schema 4`; `recovery.nas_backup.state == "fresh"`, `age_s` ≈ now − `last_ok_ts`; `home_snapshot_state == "fresh"`; `upower.state == "ok"` | tests only: 73 h → `stale`, 72 h → `fresh` |
| A3 | popup System page screenshot | three RECOVERY rows; the UPower value (the longest string now) is not elided | look-at-it step, not a gate; record the screenshot path |
| A4 | shell load + journal | `pgrep -x quickshell` PID differs from the pre-restart PID AND A1 answers from it; then no QML warning naming the plugin, no per-second spam | a clean log with an unchanged PID or no IPC answer is a FAIL |
| A5 | per-tick cost | `/proc/<pid>/stat` utime+stime delta over ≥ 300 s ≤ **1.2 %** of one core (v1.2.0: 0.94 %); `VmRSS` ≤ **40 MB** read ≥ 90 s after restart (v1.2.0: 21.5 MB) | not `VmHWM` (43 MB on v1.2.0), not systemd `MemoryCurrent` |
| A6 | installed == repo | `diff -r` package (`-x __pycache__`) and plugin, both empty | the same diff fails pre-deploy |
| A7 | public scrub, tree AND release-notes file | prints nothing | a planted lab-hostname line, piped in, matches |

Version 1.3.0 in three places; the `/hwmon-update` §4 version check must print two
identical versions. Out of hwmon's repo (not this release): `hwmon-update.md` §3
still says "load line must be present" (contradicts the fresh-shell fact); fix it
in `claude-agents`. LATER (UX): per-row colour for RECOVERY values; a backstop
status row; friendlier text for the job's reason codes.
