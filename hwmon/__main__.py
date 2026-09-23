"""`hwmon` CLI entry point (A10).

    hwmon                              human-readable snapshot, hardware first
    hwmon --json                       the snapshot verbatim
    hwmon history [metric] [--minutes N]   min/avg/max + a text sparkline
    hwmon peaks                        highest recorded values since boot / 24h
    hwmon fancurve [--hours N] [--bin C]   fan RPM vs CPU temp, by control mode (A16)
    hwmon events [--days N]            power-loss events (A17)
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
from collections import defaultdict
from pathlib import Path

from . import daemon, events, snapshot, store

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
        run_root=args.run_root,
        interval=args.interval,
        iterations=args.iterations,
    )
    return 0


def _fan_control_for_row(snap: dict) -> str:
    """A16/S6: group by `fan.control` when the row carries it (schema 2+);
    older rows (schema 1, no `fan.control` key) are derived from
    `fan.manual` -- False -> "smc", True -> "mbpfan". *(Interpretation:
    schema-1 history on this machine never distinguished a deliberate
    manual RPM test from mbpfan actually driving the fan, since neither
    ever wrote a `control` field; empirically reproducing the spec's S6
    measured table (min/max match exactly; n differs only by elapsed wall
    time since the DB kept collecting after that table was captured)
    confirmed this single-bucket derivation, rather than a 3-way
    smc/manual/mbpfan split, is what the measured numbers came from.)*
    """
    fan = snap.get("fan", {}) if isinstance(snap, dict) else {}
    control = fan.get("control")
    if isinstance(control, str):
        return control
    manual = fan.get("manual")
    if manual is True:
        return "mbpfan"
    if manual is False:
        return "smc"
    return "unknown"


def _cmd_fancurve(args: argparse.Namespace) -> int:
    if args.hours > 24:
        print("hwmon: --hours must be <= 24 (raw retention is 24h, A16)", file=sys.stderr)
        return 1
    if args.hours <= 0:
        print("hwmon: --hours must be > 0", file=sys.stderr)
        return 1
    if args.bin <= 0:
        print("hwmon: --bin must be > 0", file=sys.stderr)
        return 1
    db_path = _resolve_db_path(args)
    now = time.time()
    since = now - args.hours * 3600
    with store.Store(db_path) as st:
        rows = st.fancurve_raw(since)
    if not rows:
        print(f"hwmon: no raw rows in the last {args.hours}h")
        return 0

    groups: dict[tuple[str, int], list[tuple[float, float | None]]] = defaultdict(list)
    for package_c, fan_rpm, fan_target_rpm, snapshot_text in rows:
        if package_c is None or fan_rpm is None:
            continue
        try:
            snap = json.loads(snapshot_text)
        except json.JSONDecodeError:
            snap = {}
        control = _fan_control_for_row(snap)
        bucket = int(package_c // args.bin) * args.bin
        groups[(control, bucket)].append((fan_rpm, fan_target_rpm))

    print(f"{'control':<8} {'bin_c':>6} {'n':>6} {'min':>7} {'avg':>7} {'max':>7} {'avg_target':>11}")
    for control, bucket in sorted(groups):
        samples = groups[(control, bucket)]
        rpms = [s[0] for s in samples]
        targets = [s[1] for s in samples if s[1] is not None]
        avg_target = f"{sum(targets) / len(targets):.0f}" if targets else "-"
        print(
            f"{control:<8} {bucket:>6} {len(rpms):>6} {min(rpms):>7.0f} "
            f"{sum(rpms) / len(rpms):>7.0f} {max(rpms):>7.0f} {avg_target:>11}"
        )
    return 0


def _cmd_events(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    now = time.time()
    with store.Store(db_path) as st:
        recorded = st.list_events(args.days, now)
        # S5 (advisor): dedupe the live re-scan against EVERY recorded
        # event (`existing_event_ts_starts()`), not just the ones inside
        # the `--days` display window -- `list_events(days)` is for
        # DISPLAY only. Deduping against `recorded` instead would let a
        # short `--days` window re-classify (and needlessly re-query the
        # journal for) a gap that is already recorded further back.
        existing_ts = st.existing_event_ts_starts()
        points_raw = st.raw_points_for_events()

    points = [events.RawPoint(ts=ts, pct=pct, status=status) for ts, pct, status in points_raw]
    boots = events.list_boots()
    live_new: list[events.EventRecord] = []
    if boots is None:
        print(
            "hwmon: journalctl --list-boots unavailable; showing recorded events only",
            file=sys.stderr,
        )
    else:
        # A17: "it also scans the retained raw for gaps not yet in events
        # ... without writing" -- this command reports but never persists.
        live_new = events.find_new_events(existing_ts, points, boots, events.query_boot_journal, now=now)

    all_rows = list(recorded) + [
        (r.ts_start, r.ts_end, r.kind, r.last_pct, r.last_status, r.detail, r.boot_id) for r in live_new
    ]
    all_rows.sort(key=lambda r: r[0])

    if not all_rows:
        print("hwmon: no power-loss events in the last {} day(s)".format(args.days))
        return 0

    for ts_start, ts_end, kind, last_pct, last_status, detail, boot_id in all_rows:
        start_str = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(ts_start))
        end_str = "-" if ts_end is None else time.strftime("%H:%M:%S", time.localtime(ts_end))
        print(
            f"{start_str} -> {end_str}  {kind}  last_pct={_fmt(last_pct)}  "
            f"last_status={_fmt(last_status)}  boot={_fmt(boot_id)}"
        )
        if detail:
            print(f"    {detail}")
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
    p_daemon.add_argument("--run-root", type=Path, default=Path("/run"), help="override /run (A14 mbpfan.pid)")

    p_history = sub.add_parser("history", help="show metric history")
    p_history.add_argument("metric", nargs="?", default=None, help=f"one of {store.HEADLINE_METRICS}")
    p_history.add_argument("--minutes", type=int, default=60)

    sub.add_parser("peaks", help="show peak values since boot and in the last 24h")

    p_fancurve = sub.add_parser(
        "fancurve", help="fan RPM vs CPU temp, grouped by control mode (A16)"
    )
    p_fancurve.add_argument(
        "--hours", type=float, default=24.0, help="lookback window, <= 24 (raw retention); default 24"
    )
    p_fancurve.add_argument("--bin", type=float, default=5, help="CPU-package temperature bin width, C; default 5")

    p_events = sub.add_parser("events", help="power-loss events (A17)")
    p_events.add_argument("--days", type=float, default=30, help="lookback window in days; default 30")

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
    if args.command == "fancurve":
        return _cmd_fancurve(args)
    if args.command == "events":
        return _cmd_events(args)
    return _cmd_default(args)


if __name__ == "__main__":
    sys.exit(main())
