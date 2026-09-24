# techno.hwmon — omarchy-shell bar widget

The QML half of `hwmon` (spec: `../specs/spec.md` A6–A9, v3 A13–A15, M7). It reads the snapshot
that `hwmon.service` writes once a second and never talks to hardware itself.

- **Input:** `$XDG_RUNTIME_DIR/hwmon/latest.json` (falls back to `/run/user/<uid>`),
  watched with `FileView { watchChanges: true; onFileChanged: reload() }`.
  The contract is `../tests/fixtures/latest.example.json` (**schema 3**); every leaf may be `null`.
  Any other `schema` is treated as not live (`hwmon —`), so collector and widget ship together.
- **Bar label:** `59° 1.3k` (CPU package °C, rounded · fan RPM / 1000, 1 decimal).
  A null reading shows `–` in its slot (`–° 1.3k`). Missing, unreadable or older
  than 5 s → `hwmon —` in the theme's muted colour.
- **Colour:** worst level across every rule in `Thresholds.js`. normal = bar
  foreground, warn = theme accent, critical = bar urgent. No colour is hard-coded.
  - CPU package ≥ 80 °C warn, ≥ 95 °C critical; battery temp ≥ 45 °C warn.
  - **Sleep inhibited on low battery** (A13) — critical when `battery.pct ≤ 10`
    AND `status == "Discharging"` AND `power_guard.sleep_blocked === true`. An
    indicator of state, not a diagnosis.
  - **Throttling** (A15) — warn when `cpu.throttle.recent === true`.
  - **Cooling saturated** (M7/S5) — warn when `fan.rpm ≥ 0.95 × fan.max_rpm` AND
    package ≥ 80 °C has held for ≥ 60 s of snapshot time. The widget tracks the
    run itself (`{since, lastTs}` by snapshot `ts`); it restarts when the
    condition breaks, the data goes stale, the ts goes backwards, or two observed
    samples are > 5 s apart — so it never fires on one sample. (v2's
    fan ≥ 5000 RPM warn is removed: mbpfan reaches ~5,300 RPM on video.)
- **Popup:** click. Two pages, switched with the buttons or ←/→:
  *Hardware* (default) — a "Sleep is blocked by: `who — why`" banner when a
  `block`-mode sleep inhibitor is held (urgent colour only under the A13 critical
  rule), battery + "Sleep blocked", AC, fan speed with `target N rpm`, `Control:
  SMC auto | mbpfan | manual`, fan vs min/max, CPU package/cores, core/package
  throttle counts and "throttled < 60 s", every valid SMC sensor, invalid-sensor
  count; *System* — load, per-core usage + freq, RAM/swap,
  disk and network rates. The header always shows the snapshot age; when stale it
  says why and dims the last values.

## Files

| file | role |
|---|---|
| `manifest.json` | plugin manifest (`kinds: ["bar-widget"]`, entry `Widget.qml`) |
| `Widget.qml` | entry point: data, staleness timer, bar label, IPC, popup shell |
| `HardwarePage.qml`, `SystemPage.qml` | the two popup pages |
| `StatRow.qml`, `Meter.qml` | small row / gauge components |
| `Thresholds.js` | the single threshold table (A8/M7), the A13/A15 rules, the saturation sustain tracker, and the 5 s stale bound (A9) |
| `Format.js` | pure snapshot → text logic (null-safe; testable outside the shell) |
| `../js_tests.mjs` | node tests of `Format.js` / `Thresholds.js` against the fixture: `node plugin/js_tests.mjs` |

## IPC

The widget registers its own IPC target, `techno.hwmon`:

```
omarchy-shell techno.hwmon state     # {"label":"59° 1.3k","stale":false,"age_s":0.4}
omarchy-shell techno.hwmon toggle    # also open / close / show / hide
```

`age_s` is `null` when there is no snapshot. Note that
`omarchy-shell shell call techno.hwmon state` does **not** reach it: the shell's
`call` only routes to panel/overlay/menu plugins, not bar widgets
(`shell.qml` `callIfLoaded` looks up `panelLoaders`, which only the panel
Instantiator fills).

## Install

Use `../install.sh` — it validates, copies (never symlinks) into
`~/.config/omarchy/plugins/techno.hwmon/`, and places the widget before
`omarchy.power` with `omarchy plugin enable`. Check with:

```
omarchy plugin validate plugin/techno.hwmon
```
