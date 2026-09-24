"""R-L3.1 -- one-time, read-only triage after a `hard_poweroff` or
`unclean_shutdown` event (deliverables SPEC.md, amended by A10c: QUOTE
journal lines, don't parse them -- unlike `events.py`'s classifier, whose
whole job is to test for the one phrase it already needs, this module's job
is to SHOW a human what the journal/pacman.log actually said, not to
interpret it beyond info/action/unknown).

Runs exactly once per crash: `daemon.py` only calls `run_triage` for events
that `events.find_new_events` (A17/B2's own `UNIQUE(ts_start)` dedupe)
returns as genuinely new, so no separate dedupe is needed in this module.

**Two different boots, two different clocks, named explicitly**
(`feedback_two_clocks`):

- `record.boot_id` (per `events.py`'s B1) is the RECOVERY boot -- the one
  that starts right after the gap, in which fsck/journald/kernel evidence
  about the crash actually appears (fsck runs at the next mount, not the
  boot that died). Its evidence is read the same way
  `events.query_boot_journal` already does: `journalctl _BOOT_ID=<id> ...
  -o json`, journal clock, quoted verbatim.
- The CRASHED boot itself ("boot -1") is found from `record.ts_start` (B1:
  the last raw sample before the gap, i.e. the moment of the crash) against
  the `boots` list -- the boot whose window contains that instant.
  `pacman.log` is checked ONLY inside that boot's window, because a pacman
  transaction that ran the boot before or after the crash is not this
  crash's business. `pacman.log` timestamps carry an explicit numeric UTC
  offset (`-0700` etc.); they are parsed with that offset and converted to
  UTC epoch seconds BEFORE any comparison against `record.ts_start` or a
  boot's `first_entry_s` -- never compared as strings, and never assumed to
  already be UTC.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from . import procutil

DEFAULT_PACMAN_LOG = Path("/var/log/pacman.log")
DEFAULT_LOCK_PATH = Path("/var/lib/pacman/db.lck")
DEFAULT_TIMEOUT_S = 5.0

SEVERITY_INFO = "info"
SEVERITY_ACTION = "action"
SEVERITY_UNKNOWN = "unknown"

KIND_TRIAGE = "triage"
KIND_JOURNAL_UNAVAILABLE = "journal_unavailable"

#: A10: never a snapshot NUMBER unless it can actually be read (listing root
#: snapshots needs root, which this session does not have).
SNAPSHOT_HINT_GENERIC = "boot the newest pre-update snapshot from the Limine Snapshots menu"

_PACMAN_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s*\[ALPM\]\s*(?P<msg>.*)$")
_TRANSACTION_STARTED = "transaction started"
_TRANSACTION_COMPLETED = "transaction completed"


# --- pacman.log -------------------------------------------------------------------


@dataclass(frozen=True)
class PacmanLogLine:
    ts: float  # UTC epoch -- parsed from the line's own numeric offset
    message: str


def parse_pacman_log_line(raw: str) -> PacmanLogLine | None:
    match = _PACMAN_LINE_RE.match(raw.strip())
    if not match:
        return None
    try:
        dt = datetime.strptime(match.group("ts"), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return PacmanLogLine(ts=dt.timestamp(), message=match.group("msg"))


def parse_pacman_log(text: str) -> list[PacmanLogLine]:
    lines: list[PacmanLogLine] = []
    for raw in text.splitlines():
        parsed = parse_pacman_log_line(raw)
        if parsed is not None:
            lines.append(parsed)
    return lines


def read_pacman_log(path: Path = DEFAULT_PACMAN_LOG) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def find_interrupted_transaction(
    lines: Sequence[PacmanLogLine], window_start: float, window_end: float
) -> PacmanLogLine | None:
    """The last "transaction started" line inside `[window_start,
    window_end]` (the crashed boot's own window) that has no "transaction
    completed" line anywhere AFTER it in the whole log -- not bounded to the
    window, since a transaction that was mid-flight at the crash can only
    complete, if it ever does, on a LATER boot."""
    ordered = sorted(lines, key=lambda line: line.ts)
    started_in_window = [
        line for line in ordered if window_start <= line.ts <= window_end and _TRANSACTION_STARTED in line.message
    ]
    if not started_in_window:
        return None
    last_started = started_in_window[-1]
    for line in ordered:
        if line.ts > last_started.ts and _TRANSACTION_COMPLETED in line.message:
            return None
    return last_started


# --- stale lock ---------------------------------------------------------------------


def lock_file_exists(path: Path = DEFAULT_LOCK_PATH) -> bool:
    return path.exists()


def pacman_process_running(timeout: float = 2.0) -> bool | None:
    """`pgrep -x pacman` -- exit 0 = running, 1 = not running, anything else
    (missing `pgrep`, a timeout) = unknown. NEVER `-f`/`-a`: either would
    print every matching process's full command line, which could contain
    anything another tool passed as an argument
    (`feedback_credential_in_argv`'s sibling trap) -- `-x` only ever needs
    the process NAME, exact-matched."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "pacman"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def is_lock_stale(lock_exists: bool, pacman_running: bool | None) -> bool | None:
    """`None` (could not check) if the process check itself failed -- a
    stale-or-not verdict built on an unknown process state would be a
    guess, not a check."""
    if pacman_running is None:
        return None
    return lock_exists and not pacman_running


# --- journal (boot -1's fsck / boot 0's kernel BTRFS lines) --------------------------


def query_boot_journal_fields(
    boot_id: str, fields: Sequence[str], extra_args: Sequence[str] = (), timeout: float = DEFAULT_TIMEOUT_S
) -> list[dict] | None:
    """Generic `journalctl _BOOT_ID=<id> <fields...> <extra_args...> -o
    json`, the same JSON-lines parsing as `events.query_boot_journal` -- a
    separate, more general helper here (rather than reusing that function)
    because R-L3.1 needs different field/priority filters than A17's fixed
    fsck+journald pair."""
    cmd = ["journalctl", f"_BOOT_ID={boot_id}", *fields, *extra_args, "-o", "json", "--no-pager"]
    out = procutil.run(cmd, timeout=timeout)
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


def query_esp_fsck_lines(boot_id: str, timeout: float = DEFAULT_TIMEOUT_S) -> list[dict] | None:
    """Item 3: that (recovery) boot's `systemd-fsck` lines, quoted verbatim
    -- never pattern-matched for a specific phrase (A10c), unlike
    `events.classify_gap`, which only needs to know the ONE phrase it
    already keys its own classification on."""
    return query_boot_journal_fields(boot_id, ["SYSLOG_IDENTIFIER=systemd-fsck"], timeout=timeout)


def query_btrfs_warning_lines(boot_id: str, timeout: float = DEFAULT_TIMEOUT_S) -> list[dict] | None:
    """Item 4: that boot's kernel BTRFS lines at warning-or-worse.
    `-p warning` filters by priority (emerg..warning) and `_TRANSPORT=kernel`
    scopes to the kernel ring buffer; "BTRFS" itself is matched in Python
    (never journalctl `--grep`), keeping this on the exact same
    subprocess+JSON-parse shape as every other query in this codebase."""
    lines = query_boot_journal_fields(boot_id, ["_TRANSPORT=kernel"], extra_args=["-p", "warning"], timeout=timeout)
    if lines is None:
        return None
    return [line for line in lines if "BTRFS" in str(line.get("MESSAGE", ""))]


# --- snapshot pointer (item 5 / A10) --------------------------------------------------


def default_list_root_snapshots() -> list[dict] | None:
    """Listing root snapshots needs root (measured live 2026-09-23: `snapper
    -c root list` as the unprivileged user -> "No permissions."). The real default
    therefore always returns `None` -- this hook exists so a FUTURE
    privileged reader can be injected without changing this module, and so
    a test can prove the "readable" branch works even though it is inert
    live today."""
    return None


def snapshot_pointer_hint(
    list_root_snapshots_fn: Callable[[], list[dict] | None] = default_list_root_snapshots,
) -> str:
    snapshots = list_root_snapshots_fn()
    if not snapshots:
        return SNAPSHOT_HINT_GENERIC
    newest = max(snapshots, key=lambda s: s.get("number", -1))
    number = newest.get("number")
    if number is None:
        return SNAPSHOT_HINT_GENERIC
    return f"boot snapshot #{number} ({SNAPSHOT_HINT_GENERIC})"


# --- notification --------------------------------------------------------------------


def send_notification(summary: str, body: str = "", timeout: float = 2.0) -> None:
    """Same "skip if absent" contract as `backstop.send_notification`."""
    if shutil.which("notify-send") is None:
        return
    procutil.run(["notify-send", summary, body], timeout=timeout)


# --- boot window lookup ---------------------------------------------------------------


def find_boot_containing(ts: float, boots: Sequence) -> object | None:
    """The boot whose window contains `ts` -- used to find "boot -1", the
    CRASHED boot, from `record.ts_start` (module docstring). `boots`
    elements only need a `.first_entry_s` attribute (matches
    `events.BootInfo`)."""
    candidates = [b for b in boots if b.first_entry_s <= ts]
    if not candidates:
        return None
    return max(candidates, key=lambda b: b.first_entry_s)


# --- the report -----------------------------------------------------------------------


@dataclass(frozen=True)
class TriageReport:
    severity: str
    pacman_interrupted: str | None  # the quoted "transaction started" line, or None
    pacman_lock_stale: bool | None  # None = could not check
    esp_fsck_lines: list[str] | None  # quoted MESSAGE text; None = could not check
    btrfs_warning_lines: list[str] | None  # quoted MESSAGE text; None = could not check
    pacman_log_ok: bool
    snapshot_hint: str

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "pacman_interrupted": self.pacman_interrupted,
            "pacman_lock_stale": self.pacman_lock_stale,
            "esp_fsck_lines": self.esp_fsck_lines,
            "btrfs_warning_lines": self.btrfs_warning_lines,
            "pacman_log_ok": self.pacman_log_ok,
            "snapshot_hint": self.snapshot_hint,
        }


def build_report(
    *,
    pacman_interrupted: PacmanLogLine | None,
    pacman_log_ok: bool,
    pacman_lock_stale: bool | None,
    esp_fsck_lines: list[dict] | None,
    btrfs_warning_lines: list[dict] | None,
    snapshot_hint: str,
) -> TriageReport:
    """Severity: `action` (item 1 or 2 found something), `unknown` (any
    check could not run -- three states, not two,
    `feedback_absent_is_not_zero`: this is "could not answer", never
    silently reported as "clean"), else `info`."""
    journal_ok = esp_fsck_lines is not None and btrfs_warning_lines is not None
    if pacman_interrupted is not None or pacman_lock_stale is True:
        severity = SEVERITY_ACTION
    elif not (pacman_log_ok and journal_ok and pacman_lock_stale is not None):
        severity = SEVERITY_UNKNOWN
    else:
        severity = SEVERITY_INFO
    return TriageReport(
        severity=severity,
        pacman_interrupted=pacman_interrupted.message if pacman_interrupted is not None else None,
        pacman_lock_stale=pacman_lock_stale,
        esp_fsck_lines=None if esp_fsck_lines is None else [line.get("MESSAGE", "") for line in esp_fsck_lines],
        btrfs_warning_lines=(
            None if btrfs_warning_lines is None else [line.get("MESSAGE", "") for line in btrfs_warning_lines]
        ),
        pacman_log_ok=pacman_log_ok,
        snapshot_hint=snapshot_hint if pacman_interrupted is not None else "",
    )


def run_triage(
    record,  # events.EventRecord, already classified hard_poweroff/unclean_shutdown
    boots: Sequence,  # events.BootInfo list
    *,
    pacman_log_reader: Callable[[], str | None] = read_pacman_log,
    lock_exists_fn: Callable[[], bool] = lock_file_exists,
    pacman_running_fn: Callable[[], bool | None] = pacman_process_running,
    fsck_query_fn: Callable[[str], list[dict] | None] = query_esp_fsck_lines,
    btrfs_query_fn: Callable[[str], list[dict] | None] = query_btrfs_warning_lines,
    list_root_snapshots_fn: Callable[[], list[dict] | None] = default_list_root_snapshots,
) -> TriageReport:
    """The orchestrator: gathers every check (each independently
    injectable, so a test can exercise any single failure mode without a
    real subprocess anywhere) and hands them to the pure `build_report`."""
    crashed_boot = find_boot_containing(record.ts_start, boots)
    pacman_text = pacman_log_reader()
    window_known = crashed_boot is not None
    pacman_readable = pacman_text is not None
    pacman_log_ok = pacman_readable and window_known
    pacman_interrupted = None
    if pacman_log_ok:
        lines = parse_pacman_log(pacman_text)
        pacman_interrupted = find_interrupted_transaction(lines, crashed_boot.first_entry_s, record.ts_start)

    lock_exists = lock_exists_fn()
    pacman_running = pacman_running_fn()
    lock_stale = is_lock_stale(lock_exists, pacman_running)

    esp_fsck_lines = fsck_query_fn(record.boot_id) if record.boot_id else None
    btrfs_warning_lines = btrfs_query_fn(record.boot_id) if record.boot_id else None

    return build_report(
        pacman_interrupted=pacman_interrupted,
        pacman_log_ok=pacman_log_ok,
        pacman_lock_stale=lock_stale,
        esp_fsck_lines=esp_fsck_lines,
        btrfs_warning_lines=btrfs_warning_lines,
        snapshot_hint=snapshot_pointer_hint(list_root_snapshots_fn),
    )


def notification_body(report: TriageReport) -> str:
    if report.pacman_interrupted:
        return f"Interrupted update detected. {report.snapshot_hint}"
    if report.pacman_lock_stale:
        return "Stale pacman lock found with no pacman process running."
    if report.severity == SEVERITY_UNKNOWN:
        return "Could not fully check (journal or pacman.log unavailable)."
    return "No action needed."
