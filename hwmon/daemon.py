"""The 1 Hz collector loop (`hwmon daemon`, run by `hwmon.service`, A1).

Every tick: build a snapshot, shape-check it, write it atomically to
`latest.json`, insert one row into SQLite. Every 10 minutes: aggregate raw
rows into the minute table and prune both tables. A failing sensor read
already yields `null` for that field (sensors.py never raises for a missing
file); this loop additionally never lets one bad tick kill the process —
an unexpected exception is logged to stderr and the loop continues, since a
`Restart=on-failure` systemd unit only masks a hard crash loop, not a single
transient sample.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable

from . import events, inhibitors, snapshot, store

DEFAULT_INTERVAL_S = 1.0
AGGREGATE_EVERY_S = 600.0  # 10 min, per A5


def default_state_dir() -> Path:
    """`$XDG_RUNTIME_DIR/hwmon`, falling back to `/run/user/<uid>/hwmon`."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        runtime_dir = f"/run/user/{os.getuid()}"
    return Path(runtime_dir) / "hwmon"


def default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "hwmon" / "hwmon.db"


class _StopRequested(Exception):
    pass


def _check_power_loss_events(
    db_store: store.Store,
    *,
    now: float,
    list_boots_fn: Callable[[], list[events.BootInfo] | None],
    journal_query_fn: Callable[[str], list[dict] | None],
) -> None:
    """A17/B1/B2, run once at daemon start: classify any raw-table gap not
    already recorded in `events` and insert it. Covers both a fresh gap
    (this boot resuming after the previous one ended) and any older,
    not-yet-backfilled gap still inside the 24 h raw retention window.

    Never raises into the caller -- a busctl/journalctl failure here must
    not prevent the collector loop from starting.
    """
    try:
        points_raw = db_store.raw_points_for_events()
    except Exception as exc:  # noqa: BLE001 - startup must never abort on this
        print(f"hwmon: events check: could not read raw points: {exc!r}", file=sys.stderr)
        return
    if not points_raw:
        return
    points = [events.RawPoint(ts=ts, pct=pct, status=status) for ts, pct, status in points_raw]
    try:
        boots = list_boots_fn()
    except Exception as exc:  # noqa: BLE001
        print(f"hwmon: events check: list_boots failed: {exc!r}", file=sys.stderr)
        return
    if boots is None:
        print("hwmon: events check: journalctl --list-boots unavailable, skipping", file=sys.stderr)
        return
    try:
        existing = db_store.existing_event_ts_starts()
        new_records = events.find_new_events(existing, points, boots, journal_query_fn, now=now)
        for record in new_records:
            db_store.insert_event(record)
    except Exception as exc:  # noqa: BLE001
        print(f"hwmon: events check failed: {exc!r}", file=sys.stderr)
        return
    if new_records:
        kinds = ", ".join(sorted({r.kind for r in new_records}))
        print(f"hwmon: recorded {len(new_records)} power-loss event(s) ({kinds})", file=sys.stderr)


def run(
    *,
    state_dir: Path,
    db_path: Path,
    sysfs_root: Path = Path("/sys"),
    procfs_root: Path = Path("/proc"),
    run_root: Path = Path("/run"),
    interval: float = DEFAULT_INTERVAL_S,
    aggregate_every: float = AGGREGATE_EVERY_S,
    iterations: int | None = None,
    install_signal_handlers: bool = True,
    inhibitor_cache: inhibitors.InhibitorCache | None = None,
    list_boots_fn: Callable[[], list[events.BootInfo] | None] = events.list_boots,
    journal_query_fn: Callable[[str], list[dict] | None] = events.query_boot_journal,
    check_events_at_start: bool = True,
) -> int:
    """Run the collector loop. Returns the number of samples written.

    `iterations`, when set, stops the loop after that many samples instead
    of running forever — used by tests and by the foreground proof-run
    rather than any wall-clock sleep hack.

    `inhibitor_cache`, `list_boots_fn`, `journal_query_fn` default to the
    real busctl/journalctl calls; tests inject fakes so the whole loop can
    run with zero subprocesses.
    """
    if inhibitor_cache is None:
        inhibitor_cache = inhibitors.InhibitorCache()
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    latest_path = state_dir / "latest.json"

    stop_requested = False

    def _handle_stop(signum: int, frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    if install_signal_handlers:
        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)

    prev_state = None
    n = 0
    # The set of shape-mismatch errors last logged, so a persisting mismatch
    # is logged once (when it first appears) rather than every tick --
    # corrected 2026-09-23 after a live mismatch (SMC sensor TH0F) logged
    # ~86k identical journal lines/day. Logged again only when the SET of
    # errors changes, and once more when it clears.
    last_logged_errors: frozenset[str] = frozenset()
    with store.Store(db_path) as st:
        if check_events_at_start:
            _check_power_loss_events(
                st,
                now=time.time(),
                list_boots_fn=list_boots_fn,
                journal_query_fn=journal_query_fn,
            )
        last_aggregate = time.time()
        while not stop_requested:
            loop_start = time.time()
            try:
                power_guard = inhibitor_cache.get()
                snap, prev_state = snapshot.build_snapshot(
                    sysfs_root,
                    procfs_root,
                    prev_state,
                    now=loop_start,
                    run_root=run_root,
                    power_guard=power_guard,
                )
                current_errors = frozenset(snapshot.validate_shape(snap))
                if current_errors != last_logged_errors:
                    if current_errors:
                        print(
                            f"hwmon: snapshot shape mismatch (writing anyway): {sorted(current_errors)}",
                            file=sys.stderr,
                        )
                    else:
                        print("hwmon: snapshot shape mismatch cleared", file=sys.stderr)
                    last_logged_errors = current_errors
                snapshot.write_atomic(latest_path, snap)
                st.insert_raw(snap)
                n += 1
            except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the loop
                print(f"hwmon: tick failed: {exc!r}", file=sys.stderr)

            if loop_start - last_aggregate >= aggregate_every:
                try:
                    st.aggregate_and_prune(now=loop_start)
                except Exception as exc:  # noqa: BLE001
                    print(f"hwmon: aggregate/prune failed: {exc!r}", file=sys.stderr)
                last_aggregate = loop_start

            if iterations is not None and n >= iterations:
                break

            elapsed = time.time() - loop_start
            remaining = interval - elapsed
            if remaining > 0 and not stop_requested:
                time.sleep(remaining)
    return n
