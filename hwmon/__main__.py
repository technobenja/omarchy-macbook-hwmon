"""`hwmon` CLI entry point (A10).

    hwmon                              human-readable snapshot, hardware first
    hwmon --json                       the snapshot verbatim
    hwmon history [metric] [--minutes N]   min/avg/max + a text sparkline
    hwmon peaks                        highest recorded values since boot / 24h
    hwmon daemon                       run the collector (A1)

`--state-dir` / `--db` (or the `HWMON_STATE_DIR` / `HWMON_DB` environment
variables) override where `latest.json` and `hwmon.db` live — the default is
`$XDG_RUNTIME_DIR/hwmon/latest.json` and `~/.local/share/hwmon/hwmon.db`.
Every command that reads `latest.json` exits non-zero with a clear message
when the snapshot is missing or stale (> `STALE_AFTER_S`, matching the bar
widget's A9 threshold).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import daemon, snapshot, store

STALE_AFTER_S = 5.0

_SPARKLINE_CHARS = "▁▂▃▄▅▆▇█"


def _resolve_state_dir(args: argparse.Namespace) -> Path:
    if args.state_dir is not None:
        return args.state_dir
    import os

    env = os.environ.get("HWMON_STATE_DIR")
    if env:
        return Path(env)
    return daemon.default_state_dir()


def _resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db is not None:
        return args.db
    import os

    env = os.environ.get("HWMON_DB")
    if env:
        return Path(env)
    return daemon.default_db_path()


def _load_latest(state_dir: Path) -> tuple[dict | None, str | None]:
    """Return (snapshot, error_message). Exactly one is non-None."""
    latest_path = state_dir / "latest.json"
    try:
        text = latest_path.read_text()
    except FileNotFoundError:
        return None, f"no snapshot at {latest_path} — is hwmon.service running?"
    except OSError as exc:
        return None, f"could not read {latest_path}: {exc}"
    try:
        snap = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{latest_path} is not valid JSON: {exc}"
    age_s = time.time() - snap.get("ts", 0)
    if age_s > STALE_AFTER_S:
        return None, f"snapshot at {latest_path} is stale ({age_s:.1f}s old, > {STALE_AFTER_S:.0f}s)"
    return snap, None


def _fmt(value, unit: str = "", precision: int | None = None) -> str:
    if value is None:
        return "–"  # en dash, matches the bar widget's null rendering (A6)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if precision is not None and isinstance(value, (int, float)):
        return f"{value:.{precision}f}{unit}"
    return f"{value}{unit}"


def _print_human(snap: dict) -> None:
    b = snap["battery"]
    print("Battery")
    print(
        f"  {_fmt(b['pct'], '%')}  {_fmt(b['status'])}  {_fmt(b['power_w'], ' W', 2)}"
        f"  {_fmt(b['voltage_v'], ' V', 3)}  {_fmt(b['current_a'], ' A', 3)}"
    )
    print(
        f"  temp {_fmt(b['temp_c'], ' C', 1)}  cycles {_fmt(b['cycles'])}"
        f"  health {_fmt(b['health_pct'], '%', 2)}"
    )
    print(f"AC online: {_fmt(snap['ac']['online'])}")

    fan = snap["fan"]
    print(
        f"Fan [{_fmt(fan['label'])}]: {_fmt(fan['rpm'], ' rpm')}"
        f" (min {_fmt(fan['min_rpm'])}, max {_fmt(fan['max_rpm'])}, manual {_fmt(fan['manual'])})"
    )

    cpu = snap["cpu"]
    print(f"CPU package: {_fmt(cpu['package_c'], ' C', 1)}")
    for label, temp in cpu["cores_c"].items():
        print(f"  {label}: {_fmt(temp, ' C', 1)}")
    print(f"  usage {_fmt(cpu['usage_pct'], '%', 1)}  load {cpu['load']}")

    temps = snap["temps"]
    invalid = snap["sensors_invalid"]
    print(f"SMC sensors: {len(temps)} valid, {len(invalid)} invalid")
    for label in sorted(temps):
        print(f"  {label}: {_fmt(temps[label], ' C', 2)}")

    sysinfo = snap["system"]
    print("System")
    print(
        f"  mem {_fmt(sysinfo['mem_used_bytes'])}/{_fmt(sysinfo['mem_total_bytes'])} bytes"
        f"  swap {_fmt(sysinfo['swap_used_bytes'])}/{_fmt(sysinfo['swap_total_bytes'])} bytes"
    )
    disk = sysinfo["disk"]
    print(f"  disk {disk['device']}: read {_fmt(disk['read_bps'])} B/s  write {_fmt(disk['write_bps'])} B/s")
    net = sysinfo["net"]
    if net is None:
        print("  net: no default route")
    else:
        print(f"  net {net['iface']}: rx {_fmt(net['rx_bps'])} B/s  tx {_fmt(net['tx_bps'])} B/s")


def _sparkline(values: list[float | None]) -> str:
    known = [v for v in values if v is not None]
    if not known:
        return ""
    lo, hi = min(known), max(known)
    span = hi - lo or 1.0
    chars = []
    for v in values:
        if v is None:
            chars.append(" ")
            continue
        idx = int((v - lo) / span * (len(_SPARKLINE_CHARS) - 1))
        chars.append(_SPARKLINE_CHARS[idx])
    return "".join(chars)


def _cmd_default(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args)
    snap, error = _load_latest(state_dir)
    if error is not None:
        print(f"hwmon: {error}", file=sys.stderr)
        return 1
    if args.json:
        json.dump(snap, sys.stdout)
        sys.stdout.write("\n")
    else:
        _print_human(snap)
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    now = time.time()
    with store.Store(db_path) as st:
        metrics = [args.metric] if args.metric else list(store.HEADLINE_METRICS)
        for metric in metrics:
            try:
                rows = st.history(metric, args.minutes, now)
            except ValueError as exc:
                print(f"hwmon: {exc}", file=sys.stderr)
                return 1
            if not rows:
                print(f"{metric}: no history in the last {args.minutes} min")
                continue
            avgs = [r[2] for r in rows]
            lo = min((r[1] for r in rows if r[1] is not None), default=None)
            hi = max((r[3] for r in rows if r[3] is not None), default=None)
            print(f"{metric}: min={_fmt(lo)} max={_fmt(hi)}  {_sparkline(avgs)}")
    return 0


def _cmd_peaks(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    now = time.time()
    with store.Store(db_path) as st:
        peaks = st.peaks(now)
    for metric, values in peaks.items():
        print(f"{metric}: last_24h={_fmt(values['last_24h'])}  all_time={_fmt(values['all_time'])}")
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args)
    db_path = _resolve_db_path(args)
    daemon.run(
        state_dir=state_dir,
        db_path=db_path,
        sysfs_root=args.sysfs_root,
        procfs_root=args.procfs_root,
        interval=args.interval,
        iterations=args.iterations,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hwmon", description="Hardware telemetry for omarchy")
    parser.add_argument("--json", action="store_true", help="print the latest snapshot verbatim")
    parser.add_argument(
        "--state-dir", type=Path, default=None,
        help="override the runtime state dir (default $XDG_RUNTIME_DIR/hwmon, or $HWMON_STATE_DIR)",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="override the sqlite database path (default ~/.local/share/hwmon/hwmon.db, or $HWMON_DB)",
    )
    sub = parser.add_subparsers(dest="command")

    p_daemon = sub.add_parser("daemon", help="run the collector loop")
    p_daemon.add_argument("--interval", type=float, default=daemon.DEFAULT_INTERVAL_S)
    p_daemon.add_argument("--iterations", type=int, default=None, help="stop after N samples (for testing)")
    p_daemon.add_argument("--sysfs-root", type=Path, default=Path("/sys"))
    p_daemon.add_argument("--procfs-root", type=Path, default=Path("/proc"))

    p_history = sub.add_parser("history", help="show metric history")
    p_history.add_argument("metric", nargs="?", default=None, help=f"one of {store.HEADLINE_METRICS}")
    p_history.add_argument("--minutes", type=int, default=60)

    sub.add_parser("peaks", help="show peak values since boot and in the last 24h")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "daemon":
        return _cmd_daemon(args)
    if args.command == "history":
        return _cmd_history(args)
    if args.command == "peaks":
        return _cmd_peaks(args)
    return _cmd_default(args)


if __name__ == "__main__":
    sys.exit(main())
