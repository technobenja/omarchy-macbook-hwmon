# techno.hwmon — omarchy-shell bar widget

The QML half of `hwmon` (spec: `../specs/spec.md` A6–A9). It reads the snapshot
that `hwmon.service` writes once a second and never talks to hardware itself.

- **Input:** `$XDG_RUNTIME_DIR/hwmon/latest.json` (falls back to `/run/user/<uid>`),
  watched with `FileView { watchChanges: true; onFileChanged: reload() }`.
  The contract is `../tests/fixtures/latest.example.json`; every leaf may be `null`.
- **Bar label:** `59° 1.3k` (CPU package °C, rounded · fan RPM / 1000, 1 decimal).
  A null reading shows `–` in its slot (`–° 1.3k`). Missing, unreadable or older
  than 5 s → `hwmon —` in the theme's muted colour.
- **Colour:** worst level across the table in `Thresholds.js` (package ≥ 80 warn,
  ≥ 95 critical; fan ≥ 5000 warn; battery ≥ 45 °C warn). normal = bar foreground,
  warn = theme accent, critical = bar urgent. No colour is hard-coded.
- **Popup:** click. Two pages, switched with the buttons or ←/→:
  *Hardware* (default) — battery, AC, fan vs min/max, CPU package/cores, every valid
  SMC sensor, invalid-sensor count; *System* — load, per-core usage + freq, RAM/swap,
  disk and network rates. The header always shows the snapshot age; when stale it
  says why and dims the last values.

## Files

| file | role |
|---|---|
| `manifest.json` | plugin manifest (`kinds: ["bar-widget"]`, entry `Widget.qml`) |
| `Widget.qml` | entry point: data, staleness timer, bar label, IPC, popup shell |
| `HardwarePage.qml`, `SystemPage.qml` | the two popup pages |
| `StatRow.qml`, `Meter.qml` | small row / gauge components |
| `Thresholds.js` | the single threshold table (A8) and the 5 s stale bound (A9) |
| `Format.js` | pure snapshot → text logic (null-safe; testable outside the shell) |

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
