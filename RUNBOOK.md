# hwmon — operator runbook

Hardware telemetry collector + omarchy-shell bar widget for a MacBook Pro
11,1 (13-inch Retina, Mid 2014) running Arch Linux + Omarchy. Released as
**1.3.0** (snapshot **schema 4**). Design detail lives in
[`specs/spec.md`](specs/spec.md); this file is what to type and what to expect.

Two halves, always shipped together: the collector (`hwmon.service`, a
`systemd --user` unit) reads `/sys` and `/proc` once a second into
`$XDG_RUNTIME_DIR/hwmon/latest.json` and `~/.local/share/hwmon/hwmon.db`; the
widget (`techno.hwmon`, inside `omarchy-shell`) only watches `latest.json`.

## 1. What it shows

**Bar label:** `78° 4.1k` — CPU package °C, fan RPM/1000. A null reading shows
`–` in its slot. Missing, unreadable, wrong-schema or older than 5 s →
**`hwmon —`** in the muted colour (never an old number shown as current).

**Colour** is the worst level across every rule in
`plugin/techno.hwmon/Thresholds.js` — normal = bar foreground, **warn = theme
accent**, **critical = bar urgent**:

| level | rule |
|---|---|
| warn | CPU package ≥ 80 °C · battery temp ≥ 45 °C · `cpu.throttle.recent` · cooling saturated (fan ≥ 95 % of max **and** package ≥ 80 °C for ≥ 60 s) · a backup row stale/failed/never, or the UPower check divergent |
| critical | CPU package ≥ 95 °C · battery ≤ 10 %, discharging, and a `block` sleep inhibitor held |

**Popup** (click, or `omarchy-shell techno.hwmon open`; ←/→ switch pages):

- **Hardware** — sleep-blocked banner (`who — why`), BATTERY (health may
  exceed 100 %, never clamped; the **UPower check** row compares UPower's
  reading with the kernel's — see the table), FAN (actual · target, `Control: SMC auto |
  mbpfan | manual`), CPU (package, cores, throttle), SMC SENSORS (N VALID),
  `Invalid sensors: 7 (TH0C, …)`.
- **System** — LOAD, PER CORE, MEMORY, DISK, NETWORK, **BACKUPS** (Home
  snapshots, NAS backup). The recovery rows, wherever they sit:

| row | text | state | bar |
|---|---|---|---|
| Home snapshots | `59 min 35 s ago` | `fresh` (≤ 2 h) | normal |
| | `STALE · 3 h 2 min ago` | `stale` | warn |
| | `configured, none yet` | `empty` — snapper `home` config exists, no snapshot yet | normal |
| | `not set up` | `not_configured` — no `/etc/snapper/configs/home` | normal |
| | `could not check` | `unknown` — snapper failed (e.g. "No permissions") | normal |
| UPower check | `agrees · UPower 100.0 % / battery 100.0 %` | `ok` | normal |
| | `DISAGREES · UPower 3.3 % / battery 33.0 %` | `divergent` — > 5 points apart, or `EnergyFull/EnergyFullDesign` > 1.2 | warn |
| | `could not check` | `unknown` | normal |
| NAS backup | `2 h 4 min ago` / `STALE · 4 d 1 h ago` | `fresh` (≤ 72 h) / `stale` | normal / warn |
| | `FAILED · <reason>` | `failed` — last **non-skipped** attempt failed | warn |
| | `configured, never completed` | `never` — file exists, no `ok` ever | warn |
| | `not set up` | `not_configured` — no status file | normal |
| | `couldn't check (not a backup failure)` | `unknown` — unreadable/invalid JSON | normal |

The NAS backup row reads only `~/.local/state/omarchy-recovery/nas-backup.json`
and **requires omarchy-laptop-recovery's backup job** to write it; without
that job it stays `not set up`.

**Widget IPC** (the check that proves the plugin is loaded and live):

```
$ omarchy-shell techno.hwmon state
{"label":"78° 4.1k","stale":false,"age_s":0.3}
```

## 2. Install / update / uninstall

Requirements: Omarchy, `applesmc` loaded, Python 3 (stdlib only), `systemd --user`. No root.

```
git clone https://github.com/technobenja/omarchy-macbook-hwmon.git ~/dev/hwmon
~/dev/hwmon/install.sh
```

Expected: `==> validating plugin` … `==> restarting hwmon.service` …
`==> waiting up to 10s for hwmon.service to report the current schema` …
`==> install complete` and the reminder to `omarchy restart shell`. The
script dies with "HALF-UPGRADED" if the collector never reports the new
schema — check `journalctl --user -u hwmon.service -n 20` and re-run.

**Update** — run in this order, each step gating the next:

```
cd ~/dev/hwmon && git pull --ff-only
python3 -m unittest discover -s tests -t .      # expect: Ran 474 tests … OK
node plugin/js_tests.mjs                          # expect: 660 passed, 0 failed
omarchy plugin validate plugin/techno.hwmon
./install.sh
systemctl --user restart hwmon.service            # MUST after any Python change
omarchy restart shell                             # MUST after any plugin change
omarchy-shell techno.hwmon state                  # expect stale:false, age_s < 2
```

- `omarchy restart shell` is required: the shell hot-reloads `Widget.qml`
  but Quickshell keeps cached copies of the popup's other QML files, so a
  changed popup keeps its old layout until the shell restarts.
- **A schema bump must land in both halves in one deploy**: `SCHEMA_VERSION`
  in `hwmon/snapshot.py`, `SCHEMA` in `plugin/techno.hwmon/Format.js`, and
  `tests/fixtures/latest.example.json`. Any other schema is "not live", so a
  Python-only deploy is a permanent `hwmon —`. Run **both** suites — the JS
  suite is the one that goes red.
- Version lives in three places that must agree: `hwmon/__init__.py`,
  `plugin/techno.hwmon/manifest.json`, `CHANGELOG.md`.

**Uninstall:** `~/dev/hwmon/install.sh --uninstall` (keeps `hwmon.db`);
add `--purge` to delete history too.

## 3. CLI

Every command that reads `latest.json` exits 1 with a message if the
snapshot is missing or > 5 s old.

```
hwmon                       # Battery / AC / Fan / CPU / SMC sensors / System
hwmon --json | jq .schema   # 4 on this branch (3 for v1.2.0)
hwmon --json | jq .recovery # the backup rows + UPower check as data
hwmon history               # six headline metrics, last 60 min, sparklines
hwmon history cpu_package_c --minutes 240
hwmon peaks                 # e.g. cpu_package_c: last_24h=92.0  all_time=99.0
hwmon fancurve --hours 6    # fan RPM vs CPU °C bins, grouped smc/mbpfan/manual
hwmon events --days 7       # power-loss and recovery events (see §4)
```

`hwmon` prints e.g. `Fan [Right Side]: 4093 rpm (min 1299, max 6199, manual yes)`
and `CPU package: 78.0 C`, then the SMC sensors and system lines.

## 4. Battery protection and post-crash triage

**Critical-battery marker** (always on): at `Discharging && capacity <= 5`,
once per boot, the collector logs `hwmon: critical battery 5%` at warning
priority and runs `journalctl --user --sync`, so the last minutes before a
hard power-off survive. Find it with
`journalctl --user -u hwmon.service -p warning -g 'critical battery'`.

**Hibernate backstop** — off by default. `~/.config/hwmon/config.json`:

```json
{"hibernate_backstop": true, "backstop_action_pct": 5}
```

omarchy-laptop-recovery's `10-battery-policy` step writes this file; edits
take effect within ~10 s, no restart. Trigger: sysfs `capacity <=
backstop_action_pct - 2` while discharging for 20 consecutive samples.
Refuses (once per boot) on a `block` sleep inhibitor, `CanHibernate != yes`,
or an unreadable `PreparingForSleep`; hibernates at most once per boot;
**never reads UPower**. A missing `backstop_action_pct` logs
`hwmon: backstop: could not check` once and does nothing. If the value
disagrees with `UPower.conf`'s `PercentageAction`, one notification says so.

**Events** (`hwmon events`, 30-day retention):

| kind | meaning | do |
|---|---|---|
| `hard_poweroff` | gap crossed a boot with fsck "Dirty bit"/journald "uncleanly shut down"; last sample discharging ≤ 5 % | battery ran out: read the `triage` row; check the backstop was armed and whether a `backstop_refused` explains it |
| `unclean_shutdown` | same evidence, not low battery | power loss or crash; read the `triage` row |
| `off_or_stopped` | gap inside one boot, or no evidence | suspend or `systemctl --user stop`; nothing to do |
| `triage` | one read-only report after either crash kind (`detail` = JSON: interrupted pacman transaction, stale lock, ESP fsck and btrfs lines quoted); notification `hwmon: triage (info\|action\|unknown)` | `action`: finish/roll back the update (boot the newest pre-update snapshot) or remove the stale lock; `unknown`: re-check by hand — it was not proven clean |
| `critical_battery_marker` | marker fired | none |
| `backstop_hibernate` | logind `Hibernate` succeeded | none — it worked |
| `backstop_refused` | `detail` = reason: `sleep_blocked`, `can_hibernate='no'`, `preparing_for_sleep_unknown`, `hibernate_call_failed` | clear the inhibitor / fix hibernation; the backstop will try again next episode |
| `upower_divergent` | UPower % vs sysfs held > 5 points apart for 60 s | `sudo systemctl restart upower`, re-measure (§6) |
| `journal_unavailable` | `journalctl --list-boots` failed 3 daemon starts in a row | fix journal access; crash classification is blind until then |

## 5. The fan

On this model the SMC's automatic policy never ramps under Linux: measured
flat 1275–1320 RPM from 40 °C to 87 °C. Install `mbpfan` (AUR) and
`sudo systemctl enable --now mbpfan`; hwmon then shows `Control: mbpfan` and
the fan tracks temperature (~1700 RPM at 60 °C, ~3900 at 80 °C, max ~5300).
**`fan1_manual=1` is normal with mbpfan** — mbpfan driving the target, not a
stuck override. `hwmon fancurve`: `mbpfan` rows rise with `bin_c`, `smc` rows
stay flat. Hunting 2k↔5k on bursty loads is mbpfan's 1 s poll, not a fault.

## 6. Troubleshooting

| symptom | check | fix |
|---|---|---|
| bar shows `hwmon —` | `systemctl --user status hwmon.service`; `journalctl --user -u hwmon.service -n 20`; `hwmon --json \| jq .schema` vs `SCHEMA` in `Format.js` | `systemctl --user restart hwmon.service`; if schemas differ the machine is half-upgraded — re-run `install.sh`, then `omarchy restart shell` |
| `omarchy-shell shell call techno.hwmon …` says `unknown` | that verb resolves only panel/overlay plugins | bar-widget IPC is **`omarchy-shell techno.hwmon <method>`** (`state`, `open`, `close`, `toggle`) |
| `omarchy plugin list --json` shows `active: False` | true for **every** bar widget, first-party included | not a failure; `enabled: True` + a live `state` reply is the proof |
| `Invalid sensors: 7 (…)` | SMC sensors reading ≤ 0 °C (`-127`, or a drifting `-34.75`) are counted, not hidden | nothing — dead sensors on this board (e.g. TH0C, TH0F, TH0R, THSP, TMLB, TW0P) |
| "something wrote `fan1_manual`" | sysfs mtimes are **not** write evidence — untouched files carry the same kernfs timestamp | use the `sudo` journal or `mbpfan` status, never mtimes |
| after `omarchy restart shell` no "plugin loaded" line | a fresh shell start logs nothing per plugin; only hot-reload logs `Local plugin changed, reloading` | prove the load with `omarchy-shell techno.hwmon state` and no QML warnings naming `techno.hwmon` |
| UPower % ≈ sysfs/10, `DISAGREES` row, `upower_divergent` event | `upower -i /org/freedesktop/UPower/devices/battery_BAT0 \| grep energy-full` — `energy-full` ~10× `energy-full-design` | `sudo systemctl restart upower`; the row returns to `agrees` within ~10 s |
| journal: `snapshot shape mismatch … system.net: expected object, got null` | no default route (Wi-Fi down) | benign; clears itself with `shape mismatch cleared` |
