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

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable

from . import backstop, config, critical_marker, events, inhibitors, recovery, sensors, snapshot, store, triage

DEFAULT_INTERVAL_S = 1.0
AGGREGATE_EVERY_S = 600.0  # 10 min, per A5

#: R-L3.1 (deliverables SPEC.md): "after 3 consecutive journal query
#: failures" -- the scenario names `journalctl --list-boots` specifically,
#: so this streak is scoped to THAT call (the one `_check_power_loss_events`
#: already makes at daemon start), not every journal read `triage.py` makes.
JOURNAL_FAILURE_THRESHOLD = 3
_JOURNAL_FAILURE_STREAK_KEY = "journal_failure_streak"


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


def _record_journal_failure(db_store: store.Store, *, now: float, boot_id: str | None) -> None:
    """R-L3.1: "after 3 consecutive journal query failures" ->
    `journal_unavailable`, recorded once per streak (the counter resets to 0
    the moment it fires, so a persistently broken journal doesn't record a
    new event on every subsequent daemon start past the third) and never
    re-recorded for a boot that already has one."""
    streak = db_store.get_meta_int(_JOURNAL_FAILURE_STREAK_KEY, 0) + 1
    if streak < JOURNAL_FAILURE_THRESHOLD:
        db_store.set_meta_int(_JOURNAL_FAILURE_STREAK_KEY, streak)
        return
    db_store.set_meta_int(_JOURNAL_FAILURE_STREAK_KEY, 0)
    if boot_id is not None and db_store.has_event_kind_for_boot(triage.KIND_JOURNAL_UNAVAILABLE, boot_id):
        return
    db_store.insert_event(
        events.EventRecord(
            ts_start=now,
            ts_end=None,
            kind=triage.KIND_JOURNAL_UNAVAILABLE,
            last_pct=None,
            last_status=None,
            detail=f"journalctl --list-boots failed {streak} times in a row",
            boot_id=boot_id,
        )
    )


def _run_triage_for_event(
    db_store: store.Store,
    record: events.EventRecord,
    boots: list[events.BootInfo],
    *,
    triage_runner: Callable[[events.EventRecord, list[events.BootInfo]], "triage.TriageReport"],
    notify_fn: Callable[[str, str], None],
) -> None:
    """R-L3.1: one read-only triage report + one notification, for a single
    newly-discovered `hard_poweroff`/`unclean_shutdown` event. Never raises
    into the caller -- a triage failure must not prevent the events check
    (or the daemon) from starting."""
    try:
        report = triage_runner(record, boots)
    except Exception as exc:  # noqa: BLE001
        print(f"hwmon: triage failed: {exc!r}", file=sys.stderr)
        return
    try:
        db_store.insert_event(
            events.EventRecord(
                ts_start=time.time(),
                ts_end=None,
                kind=triage.KIND_TRIAGE,
                last_pct=record.last_pct,
                last_status=record.last_status,
                detail=json.dumps(report.as_dict()),
                boot_id=record.boot_id,
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"hwmon: triage: could not record event: {exc!r}", file=sys.stderr)
    try:
        notify_fn(f"hwmon: triage ({report.severity})", triage.notification_body(report))
    except Exception as exc:  # noqa: BLE001 - a notify-send hiccup must not break triage
        print(f"hwmon: triage: notification failed: {exc!r}", file=sys.stderr)


def _check_power_loss_events(
    db_store: store.Store,
    *,
    now: float,
    boot_id: str | None,
    list_boots_fn: Callable[[], list[events.BootInfo] | None],
    journal_query_fn: Callable[[str], list[dict] | None],
    triage_runner: Callable[[events.EventRecord, list[events.BootInfo]], "triage.TriageReport"] = triage.run_triage,
    triage_notify_fn: Callable[[str, str], None] = triage.send_notification,
) -> None:
    """A17/B1/B2, run once at daemon start: classify any raw-table gap not
    already recorded in `events` and insert it. Covers both a fresh gap
    (this boot resuming after the previous one ended) and any older,
    not-yet-backfilled gap still inside the 24 h raw retention window.

    R-L3.1 (deliverables SPEC.md): every newly-recorded `hard_poweroff`/
    `unclean_shutdown` also gets one triage report + notification here --
    `new_records` is exactly "genuinely new this run", so triage naturally
    only runs once per crash with no separate dedupe needed.

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
        _record_journal_failure(db_store, now=now, boot_id=boot_id)
        return
    if boots is None:
        print("hwmon: events check: journalctl --list-boots unavailable, skipping", file=sys.stderr)
        _record_journal_failure(db_store, now=now, boot_id=boot_id)
        return
    db_store.set_meta_int(_JOURNAL_FAILURE_STREAK_KEY, 0)
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
        for record in new_records:
            if record.kind in (events.KIND_HARD_POWEROFF, events.KIND_UNCLEAN_SHUTDOWN):
                _run_triage_for_event(
                    db_store, record, boots, triage_runner=triage_runner, notify_fn=triage_notify_fn
                )


def _maybe_log_critical_battery(
    db_store: store.Store,
    *,
    boot_id: str | None,
    pct: int | float | None,
    status: str | None,
    critical_pct: float,
    state: critical_marker.State,
    sync_fn: Callable[[], bool],
) -> None:
    """R-L1.4, wired every tick but cheap in the common (non-critical) case:
    one comparison, no DB hit, unless/until the threshold is actually
    crossed. See `critical_marker.State`'s docstring for why this is "once
    per boot" and not merely "once per process"."""
    if state.checked_this_process:
        return
    if not critical_marker.is_critical(pct, status, critical_pct):
        return
    if boot_id is not None and db_store.has_event_kind_for_boot(critical_marker.KIND_CRITICAL_BATTERY_MARKER, boot_id):
        state.checked_this_process = True
        return
    print(critical_marker.marker_line(pct), file=sys.stderr)
    sync_fn()
    db_store.insert_event(
        events.EventRecord(
            ts_start=time.time(),
            ts_end=None,
            kind=critical_marker.KIND_CRITICAL_BATTERY_MARKER,
            last_pct=None if pct is None else int(pct),
            last_status=status,
            detail=None,
            boot_id=boot_id,
        )
    )
    state.checked_this_process = True


class _OncePerPoll:
    """R4 (review): rate-limits a failure log to once per distinct
    `poll_count` value it's shown -- i.e. once per cache refresh (every
    `ttl_s`), never once per 1 s tick, for a condition that stays true
    across many ticks between refreshes."""

    def __init__(self) -> None:
        self._last_seen: int | None = None

    def maybe_log(self, poll_count: int, should_log: bool, message: str) -> None:
        if should_log and poll_count != self._last_seen:
            print(message, file=sys.stderr)
        self._last_seen = poll_count


def _record_backstop_refused(
    db_store: store.Store,
    *,
    boot_id: str | None,
    pct: float | None,
    status: str | None,
    reason: str | None,
    notify_fn: Callable[[str, str], None],
) -> None:
    """Shared by every refusal reason, including §11 R4's
    `hibernate_call_failed` -- deduped once per boot, same as every other
    per-boot marker in this codebase."""
    if boot_id is not None and db_store.has_event_kind_for_boot(backstop.KIND_BACKSTOP_REFUSED, boot_id):
        return
    notify_fn("hwmon: hibernate backstop refused", reason or "")
    db_store.insert_event(
        events.EventRecord(
            ts_start=time.time(), ts_end=None, kind=backstop.KIND_BACKSTOP_REFUSED,
            last_pct=None if pct is None else int(pct), last_status=status, detail=reason, boot_id=boot_id,
        )
    )


def _run_backstop_tick(
    db_store: store.Store,
    *,
    boot_id: str | None,
    capacity_pct: float | None,
    status: str | None,
    action_pct: float | None,
    hibernate_backstop: backstop.HibernateBackstop,
    sleep_blocked: bool | None,
    hibernate_fn: Callable[[], bool],
    notify_fn: Callable[[str, str], None],
    config_warning_state: backstop.ConfigWarningState,
) -> None:
    """R-L1.5, called only when `config.load_config()["hibernate_backstop"]`
    is true (checked by the caller). §11 R1: `capacity_pct`/`status` are
    sysfs readings already in this tick's snapshot -- this function makes
    NO UPower call at all. §11 R2: `action_pct` is hwmon's own configured
    `backstop_action_pct`; if it is missing, this records "could not check"
    once (per process) and does nothing else -- R2's explicit requirement
    that there is no default that enables anything."""
    if boot_id is not None and db_store.has_event_kind_for_boot(backstop.KIND_BACKSTOP_HIBERNATE, boot_id):
        return  # already hibernated once this boot -- R-L1.5's hard cap.

    if action_pct is None:
        if not config_warning_state.warned_missing_action_pct:
            print("hwmon: backstop: could not check (no backstop_action_pct configured)", file=sys.stderr)
            config_warning_state.warned_missing_action_pct = True
        return

    decision = hibernate_backstop.evaluate(
        status=status, pct=capacity_pct, action_pct=action_pct, sleep_blocked=sleep_blocked
    )
    if decision.action == "none":
        return
    if decision.action == "refuse":
        _record_backstop_refused(
            db_store, boot_id=boot_id, pct=capacity_pct, status=status, reason=decision.reason, notify_fn=notify_fn
        )
        return

    # decision.action == "hibernate" -- §11 R4: only record success as
    # success. hibernate_fn() is called exactly once per attempt, and its
    # result decides which event kind gets recorded.
    success = hibernate_fn()
    if success:
        notify_fn("hwmon: hibernate backstop firing", f"pct={capacity_pct}")
        db_store.insert_event(
            events.EventRecord(
                ts_start=time.time(), ts_end=None, kind=backstop.KIND_BACKSTOP_HIBERNATE,
                last_pct=None if capacity_pct is None else int(capacity_pct), last_status=status,
                detail=None, boot_id=boot_id,
            )
        )
    else:
        _record_backstop_refused(
            db_store, boot_id=boot_id, pct=capacity_pct, status=status,
            reason="hibernate_call_failed", notify_fn=notify_fn,
        )


def _maybe_alert_config_mismatch(
    *,
    action_pct: float,
    upower_conf_reader: Callable[[], float | None],
    state: backstop.ConfigWarningState,
    notify_fn: Callable[[str, str], None],
) -> None:
    """R2's optional consistency alert: `backstop_action_pct` (hwmon config)
    vs `UPower.conf`'s `PercentageAction`. Alert-only -- never read by
    `_run_backstop_tick`'s trigger -- and a parse failure
    (`upower_conf_reader()` returning `None`) yields nothing to compare,
    never an alert (§11 R2: "parse failure must not disable the backstop",
    and by the same logic must not fabricate one)."""
    upower_conf_pct = upower_conf_reader()
    mismatched = backstop.is_config_mismatched(action_pct, upower_conf_pct)
    if mismatched and not state.alerted_config_mismatch:
        notify_fn(
            "hwmon: backstop_action_pct disagrees with UPower.conf",
            f"backstop_action_pct={action_pct} UPower.conf PercentageAction={upower_conf_pct}",
        )
        print(
            f"hwmon: backstop_action_pct ({action_pct}) disagrees with UPower.conf's "
            f"PercentageAction ({upower_conf_pct})",
            file=sys.stderr,
        )
        state.alerted_config_mismatch = True
    elif not mismatched:
        state.alerted_config_mismatch = False


def _run_upower_divergence_tick(
    db_store: store.Store,
    *,
    boot_id: str | None,
    sysfs_pct: float | None,
    upower_cache: backstop.UPowerCache,
    divergence_state: backstop.DivergenceState,
    now: float,
    log_state: _OncePerPoll,
    notify_fn: Callable[[str, str], None],
) -> dict:
    """§11 R3, a STANDING check independent of `hibernate_backstop` (runs
    every tick regardless of that flag): compares UPower's DisplayDevice
    reading against sysfs, returns the `recovery.upower` dict for this
    tick's snapshot, and -- once divergence has held for >= 60 s
    continuously -- records one `upower_divergent` event per boot plus a
    notification. Never raises into the caller.
    """
    reading = upower_cache.get()
    divergence = backstop.compute_upower_divergence(
        upower_pct=reading["pct"], sysfs_pct=sysfs_pct,
        energy_full=reading["energy_full"], energy_full_design=reading["energy_full_design"],
    )
    log_state.maybe_log(
        upower_cache.poll_count,
        divergence["state"] == "unknown",
        f"hwmon: upower divergence check: could not check (UPower read={reading!r}, sysfs={sysfs_pct!r})",
    )
    sustained, divergence_state.first_divergent_ts = backstop.divergence_sustained(
        divergence_state.first_divergent_ts, divergence["state"] == "divergent", now=now
    )
    if sustained:
        # Review item 5: `has_event_kind_for_boot(kind, None)` is a SQL
        # `boot_id = NULL` comparison, which is NEVER true -- so when
        # `boot_id` itself is unreadable, that dedupe silently does nothing
        # and this would otherwise fire every tick for as long as the
        # divergence stays sustained (this is what sent the reviewer a real
        # desktop notification). `notified_without_boot_id` is the
        # per-process fallback for exactly that case.
        already_recorded = (
            db_store.has_event_kind_for_boot(backstop.KIND_UPOWER_DIVERGENT, boot_id)
            if boot_id is not None
            else divergence_state.notified_without_boot_id
        )
        if not already_recorded:
            notify_fn(
                "hwmon: UPower reading diverged from sysfs",
                f"upower={reading['pct']!r} sysfs={sysfs_pct!r} -- restart upower, re-measure",
            )
            db_store.insert_event(
                events.EventRecord(
                    ts_start=now, ts_end=None, kind=backstop.KIND_UPOWER_DIVERGENT,
                    last_pct=None if sysfs_pct is None else int(sysfs_pct), last_status=None,
                    detail=json.dumps(divergence), boot_id=boot_id,
                )
            )
            if boot_id is None:
                divergence_state.notified_without_boot_id = True
    return divergence


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
    triage_runner: Callable[[events.EventRecord, list[events.BootInfo]], "triage.TriageReport"] = triage.run_triage,
    triage_notify_fn: Callable[[str, str], None] = triage.send_notification,
    recovery_cache: recovery.RecoveryCache | None = None,
    config_loader: Callable[[], dict] | None = None,
    critical_pct: float = critical_marker.DEFAULT_CRITICAL_PCT,
    sync_journal_fn: Callable[[], bool] = critical_marker.sync_journal,
    upower_cache: backstop.UPowerCache | None = None,
    hibernate_backstop: backstop.HibernateBackstop | None = None,
    hibernate_fn: Callable[[], bool] = backstop.call_hibernate,
    backstop_notify_fn: Callable[[str, str], None] = backstop.send_notification,
    upower_conf_reader: Callable[[], float | None] = backstop.read_upower_conf_percentage_action,
) -> int:
    """Run the collector loop. Returns the number of samples written.

    `iterations`, when set, stops the loop after that many samples instead
    of running forever — used by tests and by the foreground proof-run
    rather than any wall-clock sleep hack.

    `inhibitor_cache`, `list_boots_fn`, `journal_query_fn` default to the
    real busctl/journalctl calls; tests inject fakes so the whole loop can
    run with zero subprocesses. The deliverables-spec additions
    (`recovery_cache`, `config_loader`, `upower_cache`, `hibernate_backstop`,
    `hibernate_fn`, `*_notify_fn`) follow the same rule: every one of them
    defaults to the real thing, and every test that touches R-L1.4/R-L1.5/
    R-L3.1/R-L4.1 injects a fake instead -- in particular, **no test in this
    repo ever calls a real `hibernate_fn`**.

    `config_loader` defaults to a fresh `config.ConfigCache()` -- built
    HERE, not as a bare function default, since a dataclass instance as a
    function default would be one shared, mutable cache reused (and its TTL
    state corrupted) across every call to `run()` that doesn't override it.
    """
    if inhibitor_cache is None:
        inhibitor_cache = inhibitors.InhibitorCache()
    if recovery_cache is None:
        recovery_cache = recovery.RecoveryCache()
    if config_loader is None:
        config_loader = config.ConfigCache()
    if upower_cache is None:
        upower_cache = backstop.UPowerCache()
    if hibernate_backstop is None:
        hibernate_backstop = backstop.HibernateBackstop()
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    latest_path = state_dir / "latest.json"
    current_boot_id = sensors.read_boot_id(procfs_root)
    critical_marker_state = critical_marker.State()
    config_warning_state = backstop.ConfigWarningState()
    divergence_state = backstop.DivergenceState()
    divergence_log_state = _OncePerPoll()

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
                boot_id=current_boot_id,
                list_boots_fn=list_boots_fn,
                journal_query_fn=journal_query_fn,
                triage_runner=triage_runner,
                triage_notify_fn=triage_notify_fn,
            )
        last_aggregate = time.time()
        while not stop_requested:
            loop_start = time.time()
            try:
                power_guard = inhibitor_cache.get()

                # §11 R3 -- a STANDING check, independent of hibernate_backstop
                # (runs every tick; its own UPowerCache throttles the actual
                # busctl calls). Needs sysfs battery.pct BEFORE the snapshot
                # exists (it feeds `recovery`, an input to build_snapshot), so
                # it reads sysfs once here -- sensors.py readers are cheap and
                # this is one extra battery read among the ~110 sysfs files
                # already read per tick.
                sysfs_pct_for_divergence = sensors.read_battery(sysfs_root)["pct"]
                try:
                    divergence = _run_upower_divergence_tick(
                        st,
                        boot_id=current_boot_id,
                        sysfs_pct=sysfs_pct_for_divergence,
                        upower_cache=upower_cache,
                        divergence_state=divergence_state,
                        now=loop_start,
                        log_state=divergence_log_state,
                        notify_fn=backstop_notify_fn,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"hwmon: upower divergence check failed: {exc!r}", file=sys.stderr)
                    divergence = {"state": "unknown", "upower_pct": None, "sysfs_pct": sysfs_pct_for_divergence}

                recovery_state = dict(recovery_cache.get())
                recovery_state["upower"] = divergence
                snap, prev_state = snapshot.build_snapshot(
                    sysfs_root,
                    procfs_root,
                    prev_state,
                    now=loop_start,
                    run_root=run_root,
                    power_guard=power_guard,
                    recovery=recovery_state,
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

                # R-L1.4 -- cheap in the common case; see _maybe_log_critical_battery.
                try:
                    _maybe_log_critical_battery(
                        st,
                        boot_id=current_boot_id,
                        pct=snap["battery"]["pct"],
                        status=snap["battery"]["status"],
                        critical_pct=critical_pct,
                        state=critical_marker_state,
                        sync_fn=sync_journal_fn,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"hwmon: critical battery marker check failed: {exc!r}", file=sys.stderr)

                # R-L1.5 -- DISABLED BY DEFAULT; `config_loader` is cached
                # (SHOULD 2) but re-checked every tick so a live edit still
                # takes effect within roughly one cache TTL, no restart.
                cfg = config_loader()
                action_pct = cfg.get("backstop_action_pct")
                if cfg.get("hibernate_backstop"):
                    try:
                        _run_backstop_tick(
                            st,
                            boot_id=current_boot_id,
                            capacity_pct=snap["battery"]["pct"],
                            status=snap["battery"]["status"],
                            action_pct=action_pct,
                            hibernate_backstop=hibernate_backstop,
                            sleep_blocked=power_guard.get("sleep_blocked"),
                            hibernate_fn=hibernate_fn,
                            notify_fn=backstop_notify_fn,
                            config_warning_state=config_warning_state,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"hwmon: hibernate backstop check failed: {exc!r}", file=sys.stderr)
                else:
                    # Review item 6: don't leave the 20-sample counter (and
                    # the episode latch) frozen while disabled -- toggling
                    # the flag back on must start a fresh count, never
                    # resume stale state from before it was turned off.
                    hibernate_backstop.reset()

                # R2's optional consistency alert -- alert-only, never a
                # trigger source; a parse failure (`upower_conf_reader()`
                # returning None) must never disable the backstop, so this
                # runs in its own try/except and touches nothing above it.
                # Review item 4: gated on the config loader's OWN poll_count
                # (when it has one, i.e. the real `config.ConfigCache`) so
                # this only re-checks `UPower.conf` on an actual cache
                # refresh, not every 1s tick.
                config_poll_count = getattr(config_loader, "poll_count", None)
                due_for_check = (
                    config_poll_count is None or config_poll_count != config_warning_state.last_config_poll_count
                )
                if config_poll_count is not None:
                    config_warning_state.last_config_poll_count = config_poll_count
                if action_pct is not None and due_for_check:
                    try:
                        _maybe_alert_config_mismatch(
                            action_pct=action_pct,
                            upower_conf_reader=upower_conf_reader,
                            state=config_warning_state,
                            notify_fn=backstop_notify_fn,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"hwmon: backstop config-mismatch check failed: {exc!r}", file=sys.stderr)
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
