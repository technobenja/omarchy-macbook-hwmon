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

from . import snapshot, store

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


def run(
    *,
    state_dir: Path,
    db_path: Path,
    sysfs_root: Path = Path("/sys"),
    procfs_root: Path = Path("/proc"),
    interval: float = DEFAULT_INTERVAL_S,
    aggregate_every: float = AGGREGATE_EVERY_S,
    iterations: int | None = None,
    install_signal_handlers: bool = True,
) -> int:
    """Run the collector loop. Returns the number of samples written.

    `iterations`, when set, stops the loop after that many samples instead
    of running forever — used by tests and by the foreground proof-run
    rather than any wall-clock sleep hack.
    """
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
        last_aggregate = time.time()
        while not stop_requested:
            loop_start = time.time()
            try:
                snap, prev_state = snapshot.build_snapshot(
                    sysfs_root, procfs_root, prev_state, now=loop_start
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
