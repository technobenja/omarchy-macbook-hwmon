"""R-L4.1 -- `/home` snapper snapshot age, the `recovery` key added at
schema 3 (deliverables SPEC.md).

Source: `snapper --csvout --utc -c home list --columns number,date`. `--utc`
and `--columns` are both requested explicitly so the reading never depends
on the caller's timezone or on which columns a snapper version shows by
default; `man snapper` documents that `--csvout` output is always ISO format
regardless of `--iso` ("ISO format is always used for machine-readable
outputs"), and `--utc` additionally removes any UTC-offset ambiguity rather
than requiring this module to parse one.

Three states, matching `feedback_absent_is_not_zero` (said-clean /
could-not-answer / never-asked):

- `not_configured` -- there is NO `home` snapper config at all yet, checked
  by the config FILE's existence (`/etc/snapper/configs/home`), never by
  `snapper` exiting non-zero -- so this state can be told apart from every
  OTHER failure. This is the default, pre-P2 state on this machine today
  (the deliverables spec's D4/P2 owns creating that file, in a separate
  repo), and it MUST NOT read as stale/red (task requirement).
- `unknown` -- the config file exists but the command failed, or its output
  had no snapshot row to compute an age from. Measured live 2026-09-23: even
  the pre-existing `root` config returns "No permissions." as the unprivileged user
  (`ALLOW_USERS` is not yet set for either config) -- so `unknown` is the
  live state for BOTH configs today, not a hypothetical.
- a real reading: `fresh` (age_s <= `STALE_AFTER_S`) or `stale` (older).

**Measured caveat:** the CSV column layout above is built from `man
snapper`'s documented `--columns` names, not from a live `home` config --
none exists on this machine yet, and creating one is out of this module's
scope (D4/P2, a separate repo). Re-verify the header/date format once
`home` exists for real.

Queried at a low rate (>= 60 s, task requirement) and cached between ticks,
like `inhibitors.InhibitorCache` -- `snapper` is a much heavier subprocess
than the `busctl` calls elsewhere in this package, and the collector's whole
tick budget is ~1% of one core (A6/acceptance 6).
"""

from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import procutil

STATE_NOT_CONFIGURED = "not_configured"
STATE_UNKNOWN = "unknown"
STATE_FRESH = "fresh"
STATE_STALE = "stale"

STALE_AFTER_S = 2 * 3600.0
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_CACHE_TTL_S = 60.0
DEFAULT_HOME_CONFIG_PATH = Path("/etc/snapper/configs/home")

_SNAPPER_CMD: tuple[str, ...] = ("snapper", "--csvout", "--utc", "-c", "home", "list", "--columns", "number,date")

_NULL_RESULT: dict = {"home_snapshot_state": STATE_NOT_CONFIGURED, "home_snapshot_age_s": None}


# --- subprocess wrapper --------------------------------------------------------------


def home_config_exists(config_path: Path = DEFAULT_HOME_CONFIG_PATH) -> bool:
    return config_path.is_file()


def list_home_snapshots(timeout: float = DEFAULT_TIMEOUT_S) -> str | None:
    return procutil.run(list(_SNAPPER_CMD), timeout=timeout)


# --- pure classification --------------------------------------------------------------


def parse_newest_snapshot_ts(csv_text: str) -> float | None:
    """The newest (max) snapshot `date` column, as a UTC unix timestamp, or
    `None` if the CSV has no header, no data rows, or no parseable date at
    all -- never raises on garbage input."""
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if len(rows) < 2:
        return None
    header = [c.strip().lower() for c in rows[0]]
    try:
        date_idx = header.index("date")
    except ValueError:
        return None
    timestamps: list[float] = []
    for row in rows[1:]:
        if len(row) <= date_idx:
            continue
        raw = row[date_idx].strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # --utc: no offset in the output
        timestamps.append(dt.timestamp())
    return max(timestamps) if timestamps else None


def compute_recovery_state(
    *,
    home_config_present: bool,
    csv_text: str | None,
    now: float,
    stale_after_s: float = STALE_AFTER_S,
) -> dict:
    """`{home_snapshot_state, home_snapshot_age_s}` -- pure, no I/O. See the
    module docstring for the three states."""
    if not home_config_present:
        return dict(_NULL_RESULT)
    if csv_text is None:
        return {"home_snapshot_state": STATE_UNKNOWN, "home_snapshot_age_s": None}
    newest_ts = parse_newest_snapshot_ts(csv_text)
    if newest_ts is None:
        return {"home_snapshot_state": STATE_UNKNOWN, "home_snapshot_age_s": None}
    age_s = max(0.0, now - newest_ts)
    state = STATE_STALE if age_s > stale_after_s else STATE_FRESH
    return {"home_snapshot_state": state, "home_snapshot_age_s": round(age_s, 1)}


@dataclass
class RecoveryCache:
    """Polls at most once per `ttl_s` (default 60 s) -- mirrors
    `inhibitors.InhibitorCache` exactly (monotonic clock, injectable
    `list_fn`/`clock`, `poll_count` for tests)."""

    ttl_s: float = DEFAULT_CACHE_TTL_S
    home_config_path: Path = DEFAULT_HOME_CONFIG_PATH
    list_fn: Callable[[], str | None] = list_home_snapshots
    clock: Callable[[], float] = time.monotonic
    now_fn: Callable[[], float] = time.time
    poll_count: int = field(default=0, init=False)
    _last_poll_ts: float | None = field(default=None, init=False, repr=False)
    _last_result: dict = field(default_factory=lambda: dict(_NULL_RESULT), init=False, repr=False)

    def get(self) -> dict:
        now = self.clock()
        stale = self._last_poll_ts is None or (now - self._last_poll_ts) >= self.ttl_s
        if stale:
            present = home_config_exists(self.home_config_path)
            csv_text = self.list_fn() if present else None
            self._last_result = compute_recovery_state(
                home_config_present=present, csv_text=csv_text, now=self.now_fn()
            )
            self._last_poll_ts = now
            self.poll_count += 1
        return self._last_result
