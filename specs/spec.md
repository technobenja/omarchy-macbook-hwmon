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

---

# v3 delta — 2026-09-23 (fan/battery findings)

Scope approved by Ben 2026-09-23: items 1–6 of the post-mbpfan review. Item 7
(power profile on the System page) **REMOVED — `omarchy.power` already shows
it** (Ben). Background, measured today: the SMC never raised the fan above
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

**S6 → acceptance 12 corrected.** Before 15:31 PDT the data includes Ben's
manual 4000 RPM test (15:22:32–15:23:16, max 4259). `hwmon fancurve` groups by
`fan.control` (v2 rows: derived from `fan.manual`), and check 12 expects: SMC
rows flat ~1300 except the manual window; mbpfan rows (after 15:31:42) rising
by bin — measured n/min/avg/max: 60: 52/1112/1713/2553 · 65: 303/1092/2188/3847
· 70: 134/1255/2392/5225 · 75: 37/1763/3295/5335 · 80: 7/2383/3881/5231.

**S7 → types.** `power_guard.blockers` is `[]` (never null) when nothing
blocks; `cpu.throttle.recent` is `null` on the first sample; `fan.target_rpm`
int|null. The schema-2 fixture holds **one** `block` blocker so the
per-element check can fail.
