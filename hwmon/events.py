"""A17 -- power-loss events, as amended by the advisor (B1, B2, S4).

This module is deliberately split into pure classification (``find_gaps``,
``find_crossed_boot``, ``classify_gap``, ``scan_for_events``,
``find_new_events``) and two thin subprocess wrappers (``list_boots``,
``query_boot_journal``). Every classification function takes its inputs as
plain parameters -- raw points, a boot list, a ``journal_query`` callable --
so the whole pipeline can be driven from a fixture (real captured data, or a
synthetic one) with no journalctl/subprocess involved at all. That is what
lets ``tests/fixtures/poweroff_2026-09-23/`` drive this classifier directly.

**B1 (advisor) -- gap attribution by btime + boot id.** A gap is a
power-loss *candidate* only if it crosses a boot boundary: the boot list
(``journalctl --list-boots -o json``) holds a boot whose ``first_entry`` is
the first one after the last sample before the gap. If no such boot exists,
the last sample and the resumed sample belong to the SAME boot -- a
``systemctl --user stop`` or a suspend inside one boot -- and the classifier
returns ``off_or_stopped`` WITHOUT calling ``journal_query`` at all (proven
in tests by a fake that raises/records calls: it must never be invoked for
this case). When it does cross a boundary, that boot is queried BY ID
(``_BOOT_ID=<id>``), never ``-b 0`` -- a live daemon restart must never
borrow another boot's fsck line.

**B2 -- backfill + permanent control.** ``find_new_events`` filters out any
gap whose ``ts_start`` already has an ``events`` row (the `UNIQUE(ts_start)`
column is the shared dedupe key across the daemon-start check, the one-time
backfill, and `hwmon events`'s own live re-scan) -- so re-running this scan
is always cheap and never re-queries the journal for an already-recorded
gap.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from . import procutil

DEFAULT_TIMEOUT_S = 5.0

#: A17: "if the newest raw row is > 30 s older than now" -- the same
#: threshold is reused for every internal gap found while scanning raw
#: history (B2 backfill), since nothing in the spec suggests a different
#: cutoff for a historical gap vs. the live daemon-start one.
GAP_THRESHOLD_S = 30.0

#: A17: "last sample Discharging with pct <= 5".
LOW_BATTERY_PCT = 5

_FSCK_IDENT = "systemd-fsck"
_FSCK_PATTERN = "Dirty bit is set"
_JOURNALD_IDENT = "systemd-journald"
_JOURNALD_PATTERN = "uncleanly shut down"

KIND_HARD_POWEROFF = "hard_poweroff"
KIND_UNCLEAN_SHUTDOWN = "unclean_shutdown"
KIND_OFF_OR_STOPPED = "off_or_stopped"


@dataclass(frozen=True)
class BootInfo:
    """One row of `journalctl --list-boots -o json`."""

    index: int
    boot_id: str
    first_entry_us: int
    last_entry_us: int

    @property
    def first_entry_s(self) -> float:
        return self.first_entry_us / 1_000_000.0


@dataclass(frozen=True)
class RawPoint:
    """One `raw` row's (ts, battery pct, battery status) -- everything the
    classifier needs from a sample."""

    ts: float
    pct: float | None
    status: str | None


@dataclass(frozen=True)
class EventRecord:
    """One `events` table row (A17, B1: `boot_id` and `last_pct` as INT)."""

    ts_start: float
    ts_end: float
    kind: str
    last_pct: int | None
    last_status: str | None
    detail: str | None
    boot_id: str | None


# --- subprocess wrappers (only called at daemon start / backfill / `hwmon events`) ---


def list_boots(timeout: float = DEFAULT_TIMEOUT_S) -> list[BootInfo] | None:
    """`journalctl --list-boots -o json` -> boots oldest-first, or `None` on
    any failure."""
    out = procutil.run(["journalctl", "--list-boots", "-o", "json"], timeout=timeout)
    if out is None:
        return None
    try:
        rows = json.loads(out)
        return [
            BootInfo(
                index=int(r["index"]),
                boot_id=str(r["boot_id"]),
                first_entry_us=int(r["first_entry"]),
                last_entry_us=int(r["last_entry"]),
            )
            for r in rows
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def query_boot_journal(boot_id: str, timeout: float = DEFAULT_TIMEOUT_S) -> list[dict] | None:
    """B1: fsck / journald evidence for exactly one boot, queried BY ID.

    Filters on `SYSLOG_IDENTIFIER=` (B1, measured: `-u systemd-fsck`
    matches nothing on this machine). Same-field repeats are OR'd by
    journalctl, so one call returns both identifiers' lines. Readable as a
    normal user via the ACL on `/var/log/journal` (measured) -- no root, no
    `systemd-journal` group membership required.
    """
    out = procutil.run(
        [
            "journalctl",
            f"_BOOT_ID={boot_id}",
            f"SYSLOG_IDENTIFIER={_FSCK_IDENT}",
            f"SYSLOG_IDENTIFIER={_JOURNALD_IDENT}",
            "-o",
            "json",
            "--no-pager",
        ],
        timeout=timeout,
    )
    if out is None:
        return None
    lines: list[dict] = []
    for raw_line in out.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            lines.append(entry)
    return lines


# --- pure classification -----------------------------------------------------


def _find_evidence(lines: Iterable[dict]) -> str | None:
    """The fsck "Dirty bit" line if present, else the journald "uncleanly
    shut down" line, else `None`. fsck is preferred as the more specific
    signal (matches the poweroff_2026-09-23 fixture's expected detail)."""
    fsck_line: str | None = None
    journald_line: str | None = None
    for entry in lines:
        ident = entry.get("SYSLOG_IDENTIFIER")
        message = entry.get("MESSAGE")
        if not isinstance(message, str):
            continue
        if ident == _FSCK_IDENT and fsck_line is None and _FSCK_PATTERN in message:
            fsck_line = message
        elif ident == _JOURNALD_IDENT and journald_line is None and _JOURNALD_PATTERN in message:
            journald_line = message
    return fsck_line if fsck_line is not None else journald_line


def find_gaps(
    points: Sequence[RawPoint], threshold: float = GAP_THRESHOLD_S
) -> list[tuple[RawPoint, RawPoint]]:
    """Consecutive-row gaps (by ts) wider than `threshold`, oldest first."""
    ordered = sorted(points, key=lambda p: p.ts)
    return [(a, b) for a, b in zip(ordered, ordered[1:]) if b.ts - a.ts > threshold]


def find_crossed_boot(ts_before: float, ts_after: float, boots: Sequence[BootInfo]) -> BootInfo | None:
    """B1: "the boot whose first entry is the first after the last sample".

    **S1 (advisor):** a crossed boot must satisfy
    `ts_before < first_entry_s <= ts_after` -- not just `> ts_before`. The
    original form picked the EARLIEST boot after `ts_before` with no upper
    bound, which is wrong once a boot in between has since rotated out of
    `journalctl --list-boots`: it would then reach past the gap entirely
    and attribute the gap to a LATER boot, borrowing that boot's fsck
    line for a crash it has nothing to do with. Bounding by `ts_after`
    means a rotated-out intervening boot now correctly yields `None`
    (`off_or_stopped`), never a wrong boot.

    `None` means the gap did NOT cross a boot boundary within the boots we
    can still see -- the caller must not consult the journal in that case
    (a `systemctl --user stop` or a suspend inside one boot must never
    borrow that boot's fsck line). Note: because `journalctl`'s boot clock
    can skew by a few seconds either side of the sample clock, a crash
    reboot faster than roughly `GAP_THRESHOLD_S` is not reliably
    distinguishable from remaining in the same boot -- this is an accepted
    gap in coverage, not a bug to chase.
    """
    candidates = [b for b in boots if ts_before < b.first_entry_s <= ts_after]
    if not candidates:
        return None
    return min(candidates, key=lambda b: b.first_entry_s)


def _to_int_pct(pct: float | None) -> int | None:
    return None if pct is None else int(pct)


def classify_gap(
    before: RawPoint,
    after: RawPoint,
    boots: Sequence[BootInfo],
    journal_query: Callable[[str], list[dict] | None],
) -> EventRecord:
    """Classify one gap (B1). `ts_start` is `before.ts` (B1: "ts_start
    comes from raw", not from any journal timestamp -- journald can lose
    the last ~30 s of a boot that ends in a hard power-off)."""
    boot = find_crossed_boot(before.ts, after.ts, boots)
    if boot is None:
        # Intra-boot gap -- the journal is never consulted for this case.
        return EventRecord(
            ts_start=before.ts,
            ts_end=after.ts,
            kind=KIND_OFF_OR_STOPPED,
            last_pct=_to_int_pct(before.pct),
            last_status=before.status,
            detail=None,
            boot_id=None,
        )
    lines = journal_query(boot.boot_id)
    evidence = _find_evidence(lines) if lines else None
    if evidence is None:
        return EventRecord(
            ts_start=before.ts,
            ts_end=after.ts,
            kind=KIND_OFF_OR_STOPPED,
            last_pct=_to_int_pct(before.pct),
            last_status=before.status,
            detail=None,
            boot_id=boot.boot_id,
        )
    low_battery = (
        before.status == "Discharging" and before.pct is not None and before.pct <= LOW_BATTERY_PCT
    )
    kind = KIND_HARD_POWEROFF if low_battery else KIND_UNCLEAN_SHUTDOWN
    return EventRecord(
        ts_start=before.ts,
        ts_end=after.ts,
        kind=kind,
        last_pct=_to_int_pct(before.pct),
        last_status=before.status,
        detail=evidence,
        boot_id=boot.boot_id,
    )


def _all_gaps(
    points: Sequence[RawPoint], *, now: float | None, threshold: float
) -> list[tuple[RawPoint, RawPoint]]:
    """`find_gaps` plus -- when `now` is given -- the trailing gap between
    the newest point and `now` (A17's daemon-start check), if it is wider
    than `threshold`."""
    ordered = sorted(points, key=lambda p: p.ts)
    gaps = find_gaps(ordered, threshold)
    if now is not None and ordered and (now - ordered[-1].ts) > threshold:
        gaps.append((ordered[-1], RawPoint(ts=now, pct=None, status=None)))
    return gaps


def scan_for_events(
    points: Sequence[RawPoint],
    boots: Sequence[BootInfo],
    journal_query: Callable[[str], list[dict] | None],
    *,
    now: float | None = None,
    threshold: float = GAP_THRESHOLD_S,
) -> list[EventRecord]:
    """Classify every gap found in `points` (B2 backfill), and -- when `now`
    is given -- also the trailing gap between the newest point and `now`
    (A17's daemon-start check), if it is wider than `threshold`.
    """
    gaps = _all_gaps(points, now=now, threshold=threshold)
    return [classify_gap(before, after, boots, journal_query) for before, after in gaps]


def find_new_events(
    existing_ts_starts: set[float],
    points: Sequence[RawPoint],
    boots: Sequence[BootInfo],
    journal_query: Callable[[str], list[dict] | None],
    *,
    now: float | None = None,
    threshold: float = GAP_THRESHOLD_S,
) -> list[EventRecord]:
    """Gaps not already recorded (B2's `UNIQUE(ts_start)` dedupe key),
    classified. Filters BEFORE classifying -- unlike `scan_for_events` then
    filtering after the fact, this never calls `journal_query` for a gap
    that's about to be thrown away, so a repeat scan (every daemon start,
    or `hwmon events`) never re-queries the journal for an already
    recorded gap.
    """
    gaps = _all_gaps(points, now=now, threshold=threshold)
    new_gaps = [(before, after) for before, after in gaps if before.ts not in existing_ts_starts]
    return [classify_gap(before, after, boots, journal_query) for before, after in new_gaps]
