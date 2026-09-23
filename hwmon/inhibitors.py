"""A13 -- sleep-block guard. Source: logind's ``ListInhibitors`` over
``busctl`` (no root; measured 2026-09-23).

**S1 (advisor, measured):** ``busctl --system -j call
org.freedesktop.login1 /org/freedesktop/login1
org.freedesktop.login1.Manager ListInhibitors`` returns
``json.loads(out)["data"][0]`` as a list of ``[what, who, why, mode, uid,
pid]`` rows. ``what`` is colon-joined (e.g. ``"sleep:shutdown"``) -- split
on ``:`` and test for the ``"sleep"`` token, never substring-match the raw
field.

Only entries whose ``what`` contains ``sleep`` AND whose ``mode`` is
``"block"`` count (A13): ``delay`` inhibitors are always present and
harmless (measured live 2026-09-23: NetworkManager, UPower, Omarchy
lock-screen, all ``delay``).

Queried at most every 10 s, cached between ticks (A13) -- see
``InhibitorCache`` below. Any busctl failure (missing binary, timeout,
non-zero exit, malformed JSON) yields ``sleep_blocked: None`` -- never a
crash, never a stale ``True``/``False`` presented as fresh (S7:
``blockers`` is always a list, ``[]`` when nothing blocks, never null).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

from . import procutil

BUSCTL_CMD: tuple[str, ...] = (
    "busctl",
    "--system",
    "-j",
    "call",
    "org.freedesktop.login1",
    "/org/freedesktop/login1",
    "org.freedesktop.login1.Manager",
    "ListInhibitors",
)

# S6 (advisor, measured): busctl itself answers in ~3ms; 1s leaves ample
# headroom for a loaded system without ever approaching the bar widget's
# own 5s staleness bound (A9) -- the old 5.0s timeout numerically equalled
# that bound, which meant a genuinely hung busctl call could, on its own,
# make the WHOLE snapshot look stale rather than just power_guard.
DEFAULT_TIMEOUT_S = 1.0
DEFAULT_CACHE_TTL_S = 10.0

_NULL_RESULT: dict = {"sleep_blocked": None, "blockers": []}


def list_inhibitors(timeout: float = DEFAULT_TIMEOUT_S) -> list[list] | None:
    """Run ``busctl ... ListInhibitors`` and return the raw
    ``[what, who, why, mode, uid, pid]`` rows, or ``None`` on any failure."""
    out = procutil.run(list(BUSCTL_CMD), timeout=timeout)
    if out is None:
        return None
    try:
        parsed = json.loads(out)
        rows = parsed["data"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(rows, list):
        return None
    return rows


def compute_power_guard(rows: list[list] | None) -> dict:
    """A13: `{sleep_blocked, blockers}` from raw ListInhibitors rows.

    `rows is None` (a failed poll) yields `sleep_blocked: None` -- never a
    crash, and never a stale True/False presented as current (matches the
    snapshot-wide "null = could not read" convention, A2).
    """
    if rows is None:
        return dict(_NULL_RESULT)
    blockers: list[dict] = []
    for row in rows:
        try:
            what, who, why, mode = row[0], row[1], row[2], row[3]
        except (IndexError, TypeError):
            continue
        if not isinstance(what, str) or not isinstance(mode, str):
            continue
        if mode == "block" and "sleep" in what.split(":"):
            blockers.append({"who": who, "why": why})
    return {"sleep_blocked": bool(blockers), "blockers": blockers}


@dataclass
class InhibitorCache:
    """Polls ``list_inhibitors`` at most once per ``ttl_s`` seconds (A13:
    "Queried at most every 10 s (cached between ticks)"); every other
    ``get()`` call in that window returns the last computed result with no
    subprocess call at all.

    **S6 (advisor):** the TTL clock is ``time.monotonic()`` by default, NOT
    the wall clock (``time.time()``). The daemon's own per-tick "now" is
    wall time (it's also the snapshot's ``ts``), but wall time can jump --
    an NTP correction, a manual clock change, a DST transition -- and any
    backward jump would make this cache look fresher than it is (missing a
    poll it should have made) while a forward jump would force an
    unnecessary immediate re-poll. A monotonic clock only ever moves
    forward at a steady rate, so the TTL means what it says regardless of
    what happens to the wall clock. ``clock`` is injectable so tests can
    drive the TTL boundary deterministically without a real monotonic
    clock or real sleeping.

    ``poll_fn`` defaults to the real busctl call; tests inject a fake
    (returning canned rows, or ``None`` to simulate a busctl failure) to
    prove both the caching boundary and A13's block/delay filter without
    ever shelling out for real. ``poll_count`` lets a test prove the fake
    was called exactly as many times as the TTL allows -- not once per
    tick.
    """

    ttl_s: float = DEFAULT_CACHE_TTL_S
    poll_fn: Callable[[], list[list] | None] = list_inhibitors
    clock: Callable[[], float] = time.monotonic
    poll_count: int = field(default=0, init=False)
    _last_poll_ts: float | None = field(default=None, init=False, repr=False)
    _last_result: dict = field(default_factory=lambda: dict(_NULL_RESULT), init=False, repr=False)

    def get(self) -> dict:
        now = self.clock()
        stale = self._last_poll_ts is None or (now - self._last_poll_ts) >= self.ttl_s
        if stale:
            rows = self.poll_fn()
            self._last_result = compute_power_guard(rows)
            self._last_poll_ts = now
            self.poll_count += 1
        return self._last_result
