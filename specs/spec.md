# hwmon — Hardware Telemetry Monitor for omarchy (MacBook Pro 11,1)

## Architecture Overview

A single Python application with three interfaces to the same telemetry engine:

```
┌─────────────────────────────────────────────┐
│              hwmon (Python)                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │ sysfs    │  │ sysfs    │  │ procfs/  │  │
│  │ power    │  │ hwmon    │  │ /proc    │  │
│  │ supply   │  │ devices  │  │ + psutil │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  │
│       └──────────────┴──────────────┘       │
│              Telemetry Engine               │
│    ┌────────────────┴───────────┐           │
│    ▼                            ▼            │
│  SQLite Store          Pub/Sub Bus         │
│  (1s intervals,       (in-process        │
│   rolling)             signals)           │
│    ▲            ▲              ▲           │
│    │            │              │           │
│  Tray App   waybar Module   CLI Dump      │
│  (PyQt5)    (JSON poll)     (on-demand)   │
└─────────────────────────────────────────────┘
```

## Interface Design

| Interface | What It Shows | Trigger |
|-----------|--------------|---------|
| **Tray app** (primary) | Two-page GUI: Hardware tab (fans, temps, voltage, battery) + System tab (CPU/RAM/disk/net). Click opens full history window with charts. Auto-starts at login. Always in system tray next to battery icon. | Auto-start (Hyprland autostart), always running |
| **waybar module** | Compact inline display: `BAT 87% · 42°C · Fan 3200` — updates every 2s via polling the tool's local HTTP API | waybar config, automatic refresh |
| **CLI dump** | Single-line JSON or formatted text output of latest snapshot | `hwmon --json` (on-demand) |

## Telemetry Engine

### Polling sources at 1-second intervals

**Hardware page (priority):**

| Metric | Source | Notes |
|--------|--------|-------|
| Battery % / state | `/sys/class/power_supply/BAT0/` | charge_now, charge_full, voltage_now, current_now, temperature, cycle_count |
| Discharge rate (W) | calculated: `voltage × current` | real-time power draw |
| AC status | `/sys/class/power_supply/ADP1/online` | plugged/unplugged |
| CPU die temp(s) | hwmon0-3 + thermal_zone0+1 | MBP SMC keys via applesmc module |
| GPU die temp | hwmon + thermal | iGPU dGPUs available |
| Fan RPM (front/rear) | hwmon fan inputs | Pre/post 2026-09-14 sensors verified |
| Northbridge/Southbridge temps | hwmon or thermal zones | MBP-specific SMC keys via applesmc |
| RAM temp | hwmon (if available) | On-die DDR sensors |
| SSD/NVMe temp | `/sys/class/hwmon/` + `smartctl -a /dev/sda` (read-only, no root) | If smartctl available without sudo |

**System page:**

| Metric | Source |
|--------|--------|
| CPU freq (per-core) | `/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq` or `top`/`psutil` |
| CPU load avg + per-core usage | `/proc/loadavg`, `/proc/stat` |
| RAM / swap used | `/proc/meminfo` |
| Disk I/O (per-device) | `/proc/diskstats` |
| Network throughput | `/proc/net/dev` or psutil |

### Data Retention

- On-disk SQLite at `~/.local/share/hwmon/hwmon.db`
- 1s samples for last 24h, then downsample to 1min for 30 days, then 1hr for 90 days
- Auto-cleanup via `VACUUM` on idle
- Size capped at ~50MB (auto-prune oldest buckets when full)
- **Fully local — no network dependency, no OB2 push**

## GUI — Tray App (PyQt5 + Charting)

### Tray icon area:
- Hover tooltip: `BAT 87% | 42°C | Fan 3200 RPM | AC ⚡`
- Left-click: toggles main window

### Main window — two tabs:

**Tab 1: Hardware (default, always visible)**
```
┌─────────────────────────────────────┐
│ hwmon · MacBook Pro 11,1            │
├─────────────────────────────────────┤
│ BATTERY                             │
│ ████████████░░ 87%   ⚡ Charging    │
│ Voltage: 12.4V | Current: 3.2A      │
│ Temp: 38°C | Cycle count: 1          │
│ Health: 98% (full/now)              │
├─────────────────────────────────────┤
│ TEMPERATURES                        │
│ CPU:   █████████░ 45°C             │
│ GPU:   ██████░░ 38°C               │
│ SMC:   ████░░ 28°C                 │
│ RAM:   ██░ 18°C                    │
├─────────────────────────────────────┤
│ FANS                                │
│ Front: ██████████ 3200 RPM          │
│ Rear:  █████████░ 2800 RPM          │
│ Total airflow: 6000 RPM             │
└─────────────────────────────────────┘
```

**Tab 2: System**
- CPU frequency graph (per-core sparkline)
- Load average + usage bars
- RAM/swap bars with history
- Disk I/O throughput gauges
- Network up/down rates with history

**History view (click any chart to expand):**
- Time-range selector: 5m, 15m, 1h, 6h, 24h, 30d
- Smooth line chart with zoom/pan via `Qwt` or `pyqtgraph`
- Crosshair tooltip shows exact value at cursor time

## waybar Integration

waybar config snippet:
```json
"custom/hwmon": {
    "format": "{}",
    "exec": "curl -s http://127.0.0.1:8936/api/latest | jq -r '\"BAT \" + .battery.pct + \"%\" + \" · \" + .cpu.temp + \"°C\" + \" · Fan \" + .fan.rpm + \" RPM\"'",
    "interval": 2,
    "return-type": "json"
}
```

The tray app exposes a local HTTP server on `http://127.0.0.1:8936/` with:
- `GET /api/latest` — latest snapshot as JSON
- `GET /api/history?metric=battery&hours=24` — time-series data for waybar (if needed)

## CLI Interface

```bash
hwmon              # formatted human-readable output
hwmon --json       # raw JSON (for waybar or other consumers)
hwmon history      # show 5m trend of most critical metric
hwmon peaks        # highest recorded values since boot
```

## Auto-start

Added to `~/.config/hypr/autostart.lua`:
```lua
-- hardware telemetry monitor
print("Starting hwmon...")
awful.spawn.with_shell("hwmon &")
```

## Dependencies

| Dependency | Purpose | Notes |
|-----------|---------|-------|
| `psutil` | CPU, RAM, disk, network stats | pip installable |
| `pyqt5` + `pyqtgraph` | GUI with charting | pip installable |
| `applesmc` (kernel module) | SMC key access via sysfs | **already loaded** |
| SQLite3 | Local time-series storage | stdlib, zero-config |

## File Structure

```
hwmon/
├── specs/
│   └── spec.md              # This file
├── hwmon.py                 # CLI entry point
├── engine/
│   ├── __init__.py          # telemetry collection at 1s interval
│   ├── sysfs_reader.py      # /sys/class/power_supply, hwmon, thermal
│   ├── system_stats.py      # CPU/RAM/disk/net via psutil
│   └── store.py             # SQLite schema + query helpers
├── gui/
│   ├── __init__.py          # tray app launcher
│   ├── tray.py              # system tray icon + hover tooltip
│   ├── window.py            # main window (two tabs)
│   ├── hardware_tab.py      # fan/temp/voltage/battery widgets
│   ├── system_tab.py        # CPU/RAM/disk/net widgets
│   └── charts/
│       ├── base.py          # pyqtgraph chart base class
│       └── sparkline.py     # inline sparklines
├── web/
│   ├── __init__.py          # local HTTP server (port 8936)
│   └── routes.py            # /api/latest, /api/history
├── setup.sh                 # pip install + autostart hooks
└── README.md
```

## Build Order

1. `engine/sysfs_reader.py` — read all hardware sensors (already works via applesmc)
2. `engine/store.py` — SQLite schema + retention policy
3. `engine/system_stats.py` — psutil-based metrics
4. `engine/__init__.py` — polling loop, Pub/Sub for UI updates
5. `web/` — HTTP API server for waybar + CLI
6. `gui/charts/` — pyqtgraph charting components
7. `gui/hardware_tab.py` + `system_tab.py` — widget layouts
8. `gui/tray.py` + `window.py` — app shell
9. `hwmon.py` — CLI entry point, auto-start detection
10. `setup.sh` — install + Hyprland autostart hook
