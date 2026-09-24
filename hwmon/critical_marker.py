"""R-L1.4 -- log a marker line and flush the journal the first time this
boot sees `Discharging && pct <= critical_pct` (deliverables SPEC.md; the
advisor NIT hwmon v3 AS EXECUTED deferred: "log a marker line at
`pct <= 5 && Discharging`").

Pure decision logic lives here (`is_critical`, `marker_line`); the actual
`journalctl --user --sync` subprocess call is also here (a thin wrapper,
same shape as every other subprocess call in this codebase -- see
`procutil.py`), but the DB dedupe check ("has this already fired this
boot?") is wired in `daemon.py`, matching the split every other v3/v4
module uses between "what happened" and "talk to the DB".

**Threshold, not measured live:** the deliverables spec's R-L1.3 raises
UPower's own `PercentageAction`/`PercentageCritical` through an install
script this repo does not own (a separate, non-Python repo, per the spec's
D4) -- and that install has not happened. Rather than hard-code today's
UPower default (5, measured in the deliverables spec's §2) as if it were
permanent, `critical_pct` is a plain parameter with that value as its
default; `daemon.run`'s own `critical_pct` parameter is the override point
once the real number is fixed.

**The journal line itself** is written with a syslog priority prefix
(`"<4>"`, `LOG_WARNING`) rather than a `logging`/`syslog` module call: this
package is Python-stdlib-only, and every other daemon condition is already
logged with a plain `print(..., file=sys.stderr)` (see `daemon.py`).
systemd's own capture of a unit's stderr recognizes that prefix
(`SyslogLevelPrefix=`, systemd's default for an `ExecStart` stream) and
files the line at that journal priority -- no extra dependency, and no
change to how every other line in this codebase is logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import procutil

#: LOG_WARNING (RFC 3164 syslog level 4) as a systemd `SyslogLevelPrefix=`
#: prefix -- see the module docstring.
MARKER_PRIORITY_PREFIX = "<4>"

#: Measured UPower default, 2026-09-23 (deliverables SPEC.md §2: "Critical
#: 5"). Not the spec's final number -- see the module docstring.
DEFAULT_CRITICAL_PCT = 5

DEFAULT_SYNC_TIMEOUT_S = 5.0

KIND_CRITICAL_BATTERY_MARKER = "critical_battery_marker"


def is_critical(
    pct: int | float | None, status: str | None, critical_pct: float = DEFAULT_CRITICAL_PCT
) -> bool:
    """R-L1.4's trigger condition: `Discharging && pct <= critical_pct`.
    Either reading missing is never "critical" -- a `None` must not be
    treated as a low number (`feedback_absent_is_not_zero`)."""
    return status == "Discharging" and pct is not None and pct <= critical_pct


def marker_line(pct: int | float | None) -> str:
    """`"hwmon: critical battery N%"` at LOG_WARNING (R-L1.4, verbatim
    required text, prefixed per the module docstring)."""
    return f"{MARKER_PRIORITY_PREFIX}hwmon: critical battery {pct}%"


def sync_journal(timeout: float = DEFAULT_SYNC_TIMEOUT_S) -> bool:
    """`journalctl --user --sync` -- measured by the deliverables spec's
    advisor (A6) to exit 0 as the unprivileged user, no root. Returns whether it
    succeeded; never raises (matches `procutil.run`)."""
    return procutil.run(["journalctl", "--user", "--sync"], timeout=timeout) is not None


@dataclass
class State:
    """Cross-tick, in-process latch: once this process has either fired the
    marker or discovered (via the DB) that an earlier process already fired
    it this boot, stop re-checking the DB every tick for the rest of this
    process's life. A NEW process (a `systemctl --user restart`) starts a
    fresh `State`, but its first critical tick still finds the DB record
    from before the restart and does not re-fire (see `daemon.py`'s
    `_maybe_log_critical_battery`, which is what makes this "once per boot"
    rather than merely "once per process")."""

    checked_this_process: bool = field(default=False)
