"""R-N7 -- NAS backup freshness, the `recovery.nas_backup` key added at
schema 4 (deliverables `omarchy-power-loss-recovery/SPEC-nas-backup.md`,
requirement R-N7 and its v2 amendment A-S5, which stays owned by hwmon and
reads only the status file, never the repo/mount/network).

The status file is written by a job OUTSIDE this repo (a separate agent's
scope, exactly like `recovery.py`'s snapper `home` config): `$XDG_STATE_HOME/
omarchy-recovery/nas-backup.json`, falling back to
`~/.local/state/omarchy-recovery/nas-backup.json` when `XDG_STATE_HOME` is
unset (XDG default). Contract (as handed to this module):
`{ts, iso, result: ok|skipped|failed, reason, snapshot_id, bytes_added,
files_new, files_changed, source_snapper, duration_s, last_ok_ts}` -- this
module reads only `result`, `reason`, `ts` and `last_ok_ts`; the rest is the
writer's own bookkeeping and is not hwmon's concern.

Four states, matching `feedback_absent_is_not_zero` (said-clean /
could-not-answer / never-asked) plus a `failed` state driven by the writer's
own report:

- `not_configured` -- the status file does not exist yet (the backup job has
  never run, or hasn't been built at all). Checked by the file's existence,
  never by a parse failure, so it can be told apart from every other
  problem, and it MUST NOT read as stale/red.
- `unknown` -- the file exists but is unreadable, is not valid JSON, is not
  a JSON object, or its `result` field is missing/not one of
  `ok`/`skipped`/`failed` -- could not answer, never shown as fresh.
- `failed` -- the file's own last-recorded `result` is `failed`; `reason` is
  carried through for display. **`skipped` alone never produces this
  state** -- a skipped run (the laptop was simply away) only lets `age_s`
  keep growing toward `stale`, per the task requirement.
- a real reading: `fresh` (`age_s <= STALE_AFTER_S`) or `stale` (older, OR
  there has never been a recorded `ok` while the file exists at all --
  "no ok ever" must not be reported as fresh by omission).

`age_s` is computed from `last_ok_ts` when present; when `result == "ok"`
and the writer hasn't (yet) filled in `last_ok_ts` on its own success row,
this run's own `ts` stands in for it (an `ok` result **is** a last-ok
event). A `skipped`/`failed` row's `last_ok_ts` is expected to keep
carrying forward the previous success's timestamp so the age keeps growing
correctly across intervening non-ok runs.

Queried at a low rate (>= 60 s, task requirement) and cached between ticks,
exactly like `recovery.RecoveryCache` -- this module reads one small local
JSON file and never touches the network or any mount.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

STATE_NOT_CONFIGURED = "not_configured"
STATE_UNKNOWN = "unknown"
STATE_FAILED = "failed"
STATE_FRESH = "fresh"
STATE_STALE = "stale"

RESULT_OK = "ok"
RESULT_SKIPPED = "skipped"
RESULT_FAILED = "failed"
_VALID_RESULTS = frozenset({RESULT_OK, RESULT_SKIPPED, RESULT_FAILED})

STALE_AFTER_S = 72 * 3600.0
DEFAULT_CACHE_TTL_S = 60.0

_NULL_RESULT: dict = {"state": STATE_NOT_CONFIGURED, "age_s": None, "reason": None}


def default_state_path() -> Path:
    """`$XDG_STATE_HOME/omarchy-recovery/nas-backup.json`, falling back to
    `~/.local/state/omarchy-recovery/nas-backup.json` (the XDG default) when
    the variable is unset -- matches the writer's own contract path."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "omarchy-recovery" / "nas-backup.json"


# --- file I/O --------------------------------------------------------------


def read_status_file(path: Path) -> str | None:
    """Read the status file's text, or `None` on any failure (missing,
    unreadable, a directory, ...). Never raises."""
    try:
        return path.read_text()
    except OSError:
        return None


# --- pure classification -----------------------------------------------------


def compute_nas_backup_state(
    *,
    file_present: bool,
    raw_json: str | None,
    now: float,
    stale_after_s: float = STALE_AFTER_S,
) -> dict:
    """`{state, age_s, reason}` -- pure, no I/O. See the module docstring
    for the four states."""
    if not file_present:
        return dict(_NULL_RESULT)
    if raw_json is None:
        return {"state": STATE_UNKNOWN, "age_s": None, "reason": None}
    try:
        data = json.loads(raw_json)
    except (ValueError, TypeError):
        return {"state": STATE_UNKNOWN, "age_s": None, "reason": None}
    if not isinstance(data, dict):
        return {"state": STATE_UNKNOWN, "age_s": None, "reason": None}

    result = data.get("result")
    if result not in _VALID_RESULTS:
        return {"state": STATE_UNKNOWN, "age_s": None, "reason": None}

    last_ok_ts = data.get("last_ok_ts")
    if not isinstance(last_ok_ts, (int, float)) or isinstance(last_ok_ts, bool):
        last_ok_ts = None
    ts = data.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        ts = None

    effective_last_ok = last_ok_ts
    if effective_last_ok is None and result == RESULT_OK:
        # An "ok" result IS a last-ok event, even if the writer hasn't (yet)
        # echoed it back into last_ok_ts on this same row.
        effective_last_ok = ts

    reason = data.get("reason")
    reason = reason if isinstance(reason, str) else None

    if result == RESULT_FAILED:
        age_s = None if effective_last_ok is None else round(max(0.0, now - effective_last_ok), 1)
        return {"state": STATE_FAILED, "age_s": age_s, "reason": reason}

    if effective_last_ok is None:
        # "skipped" (or a malformed "ok") with no last-ok timestamp at all --
        # "no ok ever while the file exists" is stale, never a guessed fresh.
        return {"state": STATE_STALE, "age_s": None, "reason": None}

    age_s = max(0.0, now - effective_last_ok)
    state = STATE_STALE if age_s > stale_after_s else STATE_FRESH
    return {"state": state, "age_s": round(age_s, 1), "reason": None}


@dataclass
class NasBackupCache:
    """Polls at most once per `ttl_s` (default 60 s) -- mirrors
    `recovery.RecoveryCache` exactly (monotonic clock, injectable
    `read_fn`/`clock`, `poll_count` for tests). Reads only the one small
    local status file; never the network, never a mount."""

    ttl_s: float = DEFAULT_CACHE_TTL_S
    state_path: Path = field(default_factory=default_state_path)
    read_fn: Callable[[Path], str | None] = read_status_file
    clock: Callable[[], float] = time.monotonic
    now_fn: Callable[[], float] = time.time
    poll_count: int = field(default=0, init=False)
    _last_poll_ts: float | None = field(default=None, init=False, repr=False)
    _last_result: dict = field(default_factory=lambda: dict(_NULL_RESULT), init=False, repr=False)

    def get(self) -> dict:
        now = self.clock()
        stale = self._last_poll_ts is None or (now - self._last_poll_ts) >= self.ttl_s
        if stale:
            present = self.state_path.is_file()
            raw = self.read_fn(self.state_path) if present else None
            self._last_result = compute_nas_backup_state(
                file_present=present, raw_json=raw, now=self.now_fn()
            )
            self._last_poll_ts = now
            self.poll_count += 1
        return self._last_result
