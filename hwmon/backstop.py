"""R-L1.5 -- hwmon as a hibernate backstop, amended by the deliverables
spec's advisor findings (`omarchy-power-loss-recovery/SPEC.md` §10 A4/A5,
**superseded by §11 M1 / §11 R** -- read those, not §10 A4, for the current
design).

DISABLED BY DEFAULT: the deliverables spec explicitly gates any automatic
hibernate action on a supervised round-trip test (R-L1.1) that has NOT
happened yet, so this whole module is inert unless
`~/.config/hwmon/config.json` holds `{"hibernate_backstop": true}`
(`config.load_config`, cached by `config.ConfigCache`) -- checked fresh by
the caller (`daemon.py`) roughly every `ConfigCache.ttl_s` seconds, so
flipping the file live takes effect quickly with no restart, and flipping
it back off stops any further action just as quickly.
**No test in this repo ever calls a real hibernate** -- every test injects a
fake in place of `call_hibernate`.

**§11 M1 -- measured 2026-09-23 19:43 PDT: UPower's percentage is wrong by
~10x.** In the same minute, sysfs read `capacity=33` while `upower -i
battery_BAT0` reported `percentage=3.27161` -- `energy-full` (722.698 Wh) is
~10x `energy-full-design` (72.576 Wh), a stale/garbage `charge_full` UPower
is holding onto (cause unproven; `restart upower` would probably clear it,
not run). Two hours earlier the two scales agreed within 2 points (49 vs
47.3), so this is not always true -- it is a live UPower reliability fault,
not a fixed offset to correct for.

**§11 R1 -- BLOCKER: the backstop reads sysfs `capacity`/`status`, NEVER
UPower.** A backstop must not share a failure mode with the primary
mechanism (UPower's own critical action, R-L1.2) it exists to back up: a
backstop that reads UPower's number fails exactly when UPower's number is
wrong, which is precisely the scenario the backstop is supposed to catch.
`HibernateBackstop.evaluate()` therefore takes the caller's already-read
sysfs `battery.pct`/`battery.status` (the same fields `critical_marker.py`
reads) as `pct`/`status` -- this module makes NO percentage-reading busctl
call of its own for the trigger, ever.

**§11 R2 -- the threshold is hwmon config, `backstop_action_pct`, with NO
default that enables anything.** `PercentageAction` is not even a real
D-Bus property (measured: `busctl get-property ... PercentageAction` ->
"No such property" -- UPower's only related surface is the `GetCriticalAction`
method). `read_percentage_action` (§10 A4) and its tests are deleted: they
exercised a property that cannot exist. If `hibernate_backstop` is true but
`backstop_action_pct` is absent, the backstop records "could not check"
(once) and does not act -- see `daemon.py`'s `_run_backstop_tick`. A
best-effort, alert-only consistency check (`read_upower_conf_percentage_action`,
below) parses `/etc/UPower/UPower.conf` + `UPower.conf.d/*.conf` (later
files win) and flags a mismatch against `backstop_action_pct` -- it is
NEVER a second trigger source, and a parse failure must never disable the
backstop (a missing/unreadable UPower.conf just means nothing to compare).

**§11 R3 -- UPower is still watched, just never trusted alone.** A
low-rate standing check (`UPowerCache` + `compute_upower_divergence`)
compares UPower's `Percentage` against sysfs `capacity` (diverges past 5
points) and `EnergyFull`/`EnergyFullDesign` (diverges past a 1.2x ratio --
measured 9.96x live). Read from the actual battery device
(`/org/freedesktop/UPower/devices/battery_BAT0`, discovered cheaply via
`EnumerateDevices` rather than hard-coded), NOT `DisplayDevice`: measured
live, `DisplayDevice`'s `EnergyFullDesign` is `0.0` -- it does not mirror
the real battery for that property, even though its `Percentage` does.
`daemon.py` surfaces this every tick as `recovery.upower` (schema 3) and,
once it has held for 60s continuously, records one `upower_divergent`
event per boot + a notification. This is purely informational/alerting --
it never feeds back into the backstop's own trigger (that would recreate
the exact common-mode failure R1 forbids).

**Round-2 review fixes (2026-09-23, same day, before ship):** `_dbus_get_property`
was unwrapping the OUTER variant only -- `Properties.Get`'s single "v"
out-parameter is itself a variant, so `busctl -j`'s JSON nests it as
`{"type": "v", "data": [{"type": <real type>, "data": <real value>}]}`.
Measured live: `PreparingForSleep` -> `{"type":"v","data":[{"type":"b","data":false}]}`;
`EnergyFullDesign` (on `DisplayDevice`, the fixture that led to finding
this device is wrong) -> `{"type":"v","data":[{"type":"d","data":0.0}]}`.
The old code returned `data[0]` -- the wrapper dict itself -- which is
never a `bool`/`float`, so `read_preparing_for_sleep()` and every
`Percentage`/`Energy*` reader returned `None` on a perfectly healthy bus.
`CanHibernate` was unaffected: it is a plain METHOD call (`"s"` return),
never wrapped in a `Properties.Get` variant. Also this round: `UPowerCache`
now issues one `Properties.GetAll` per device instead of three separate
`Get` calls (NIT); the energy-ratio's zero-denominator case now yields
`"unknown"` for that signal rather than silently falling through to `"ok"`;
`backstop_action_pct` validation moved to `config.py` (bool/inf/NaN/
out-of-(0,100] all rejected, with one warning); the config-mismatch alert
and the no-`boot_id` divergence notification are both now rate-limited
properly (see `daemon.py`); and `HibernateBackstop.reset()` was added so
toggling `hibernate_backstop` off and back on restarts the 20-sample count
rather than resuming stale state.

**§11 R4 -- `backstop_hibernate` is recorded only on a SUCCESSFUL
`hibernate_fn()`.** A failed logind call records `backstop_refused`
(`reason="hibernate_call_failed"`) instead and does NOT consume the
per-boot `backstop_hibernate` cap -- a later episode may still try again
(wired in `daemon.py`, since this module only decides, it doesn't call
`hibernate_fn` itself).

Refuses -- recording `backstop_refused`, once per boot -- when a `block`
sleep inhibitor is present (the existing A13 `power_guard`), when logind's
`CanHibernate` is not exactly `"yes"`, or when `PreparingForSleep` itself
could not be read (`None` -- SHOULD 1: an unreadable guard must REFUSE, not
proceed as if it were clear). A refusal does NOT permanently rule out a
later hibernate attempt in the same boot, since the blocking condition can
clear; `backstop_hibernate` alone is capped at most once EVER per boot
(checked against the `events` table, A17/B2's dedupe mechanism).

"No per-sample retries" (the spec's own words) is read here as: at most one
ATTEMPT (fire or refuse) per continuous low-battery episode. The
20-consecutive-sample counter must fall back below the trigger and rise
past it again -- a fresh episode -- before this module tries a second time,
even if the first attempt in the previous episode was a refusal.
"""

from __future__ import annotations

import configparser
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import procutil

REQUIRED_CONSECUTIVE = 20
MARGIN_PCT = 2.0
DEFAULT_TIMEOUT_S = 1.0
DEFAULT_CACHE_TTL_S = 10.0

#: §11 R3: the two independent divergence signals.
DIVERGENCE_PCT_THRESHOLD = 5.0
DIVERGENCE_ENERGY_RATIO_THRESHOLD = 1.2
DIVERGENCE_SUSTAIN_S = 60.0

KIND_BACKSTOP_HIBERNATE = "backstop_hibernate"
KIND_BACKSTOP_REFUSED = "backstop_refused"
KIND_UPOWER_DIVERGENT = "upower_divergent"

_DISPLAY_DEVICE_PATH = "/org/freedesktop/UPower/devices/DisplayDevice"
_LOGIND_PATH = "/org/freedesktop/login1"
_UPOWER_DEVICE_IFACE = "org.freedesktop.UPower.Device"

#: Fallback only, used when `EnumerateDevices` itself fails -- measured live
#: 2026-09-23 as this machine's one real battery object path. `find_battery_device_path`
#: is the preferred path; this constant exists so a busctl hiccup degrades to
#: "probably still right" rather than "no reading at all".
DEFAULT_BATTERY_DEVICE_PATH = "/org/freedesktop/UPower/devices/battery_BAT0"

DEFAULT_UPOWER_CONF_PATH = Path("/etc/UPower/UPower.conf")
DEFAULT_UPOWER_CONF_D_DIR = Path("/etc/UPower/UPower.conf.d")

# --- subprocess wrappers (busctl, the same JSON shape as inhibitors.py's ListInhibitors) --


def _dbus_get_property(service: str, path: str, iface: str, prop: str, timeout: float) -> object | None:
    """`busctl -j call ... org.freedesktop.DBus.Properties Get IFACE PROP`.

    Round-2 review, BLOCKER: `Properties.Get`'s single out-parameter has
    D-Bus type `"v"` (variant) -- `busctl -j`'s JSON represents a variant as
    a NESTED `{"type": <real type>, "data": <real value>}` object, not the
    bare value. So `parsed["data"][0]` (the pattern `inhibitors.list_inhibitors`
    uses, correctly, for its OWN call -- `ListInhibitors` returns a
    concrete `a(ssssuu)`, never a variant) is only the outer wrapper here;
    this needs a SECOND unwrap. Measured live 2026-09-23:
    `busctl --system -j call org.freedesktop.login1 /org/freedesktop/login1
    org.freedesktop.DBus.Properties Get ss org.freedesktop.login1.Manager
    PreparingForSleep` -> `{"type":"v","data":[{"type":"b","data":false}]}`.
    Before this fix, every property read through this function silently
    returned `None` on a perfectly healthy D-Bus -- proven by
    `test_backstop.py`'s live-shape tests, which fail if the second unwrap
    is removed.
    """
    out = procutil.run(
        [
            "busctl", "--system", "-j", "call", service, path,
            "org.freedesktop.DBus.Properties", "Get", "ss", iface, prop,
        ],
        timeout=timeout,
    )
    if out is None:
        return None
    try:
        parsed = json.loads(out)
        variant = parsed["data"][0]
        return variant["data"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def _dbus_get_all_properties(service: str, path: str, iface: str, timeout: float) -> dict | None:
    """`busctl -j call ... org.freedesktop.DBus.Properties GetAll IFACE` --
    one call for every property on `iface`, instead of one `Get` per
    property (NIT, review). `GetAll`'s single out-parameter has type
    `"a{sv}"`: a dict of name -> variant, so each VALUE also needs the same
    `{"type", "data"}` unwrap `_dbus_get_property` does. Measured live
    2026-09-23 on `battery_BAT0`: `EnergyFull` -> `722.698...`,
    `EnergyFullDesign` -> `72.576`, `Percentage` -> `6.1525...` -- among ~30
    other properties this function also returns but its only two callers
    (`read_battery_upower_properties`) pick three keys out of."""
    out = procutil.run(
        ["busctl", "--system", "-j", "call", service, path, "org.freedesktop.DBus.Properties", "GetAll", "s", iface],
        timeout=timeout,
    )
    if out is None:
        return None
    try:
        parsed = json.loads(out)
        raw = parsed["data"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    result: dict = {}
    for key, variant in raw.items():
        if isinstance(variant, dict) and "data" in variant:
            result[key] = variant["data"]
    return result


def find_battery_device_path(timeout: float = DEFAULT_TIMEOUT_S) -> str | None:
    """§11 R3, review item 3: the actual battery object path, discovered
    cheaply via UPower's own `EnumerateDevices` method (one extra busctl
    call, cached at the same TTL as everything else `UPowerCache` reads) --
    never hard-coded, since a future machine's battery need not be named
    `BAT0`. Picks the first path containing `/battery_`; this machine's
    `EnumerateDevices` (measured) returns exactly
    `["/org/freedesktop/UPower/devices/battery_BAT0",
    "/org/freedesktop/UPower/devices/line_power_ADP1"]`. `None` on any
    failure -- the caller falls back to `DEFAULT_BATTERY_DEVICE_PATH`."""
    out = procutil.run(
        ["busctl", "--system", "-j", "call", "org.freedesktop.UPower", "/org/freedesktop/UPower",
         "org.freedesktop.UPower", "EnumerateDevices"],
        timeout=timeout,
    )
    if out is None:
        return None
    try:
        parsed = json.loads(out)
        paths = parsed["data"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(paths, list):
        return None
    for path in paths:
        if isinstance(path, str) and "/battery_" in path:
            return path
    return None


def read_battery_upower_properties(timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """§11 R3, review item 3: `{pct, energy_full, energy_full_design}` from
    the REAL battery device, via one `GetAll` call -- never `DisplayDevice`,
    whose `EnergyFullDesign` was measured live to be `0.0` (it does not
    mirror the real battery for that property, even though its `Percentage`
    does). Any read failure (device not found, busctl error, wrong type for
    a property) yields `None` for that field, never a crash and never a
    stale value presented as current."""
    device_path = find_battery_device_path(timeout=timeout) or DEFAULT_BATTERY_DEVICE_PATH
    props = _dbus_get_all_properties("org.freedesktop.UPower", device_path, _UPOWER_DEVICE_IFACE, timeout)
    if props is None:
        return {"pct": None, "energy_full": None, "energy_full_design": None}

    def _as_float(key: str) -> float | None:
        value = props.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    return {
        "pct": _as_float("Percentage"),
        "energy_full": _as_float("EnergyFull"),
        "energy_full_design": _as_float("EnergyFullDesign"),
    }


def read_preparing_for_sleep(timeout: float = DEFAULT_TIMEOUT_S) -> bool | None:
    value = _dbus_get_property(
        "org.freedesktop.login1", _LOGIND_PATH, "org.freedesktop.login1.Manager", "PreparingForSleep", timeout
    )
    return value if isinstance(value, bool) else None


def read_can_hibernate(timeout: float = DEFAULT_TIMEOUT_S) -> str | None:
    """`CanHibernate()` is a METHOD, not a property (unlike the reads
    above) -- same `busctl -j call` verb and `data[0]` unwrap, no
    `Properties.Get` wrapper needed."""
    out = procutil.run(
        [
            "busctl", "--system", "-j", "call", "org.freedesktop.login1", _LOGIND_PATH,
            "org.freedesktop.login1.Manager", "CanHibernate",
        ],
        timeout=timeout,
    )
    if out is None:
        return None
    try:
        parsed = json.loads(out)
        value = parsed["data"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    return value if isinstance(value, str) else None


def call_hibernate(timeout: float = 5.0) -> bool:
    """The real, irreversible logind `Hibernate(false)` call ("false" =
    not interactive). **No test in this repo calls this** -- every test
    injects a fake `hibernate_fn` in `daemon.py`'s caller instead."""
    out = procutil.run(
        [
            "busctl", "--system", "call", "org.freedesktop.login1", _LOGIND_PATH,
            "org.freedesktop.login1.Manager", "Hibernate", "b", "false",
        ],
        timeout=timeout,
    )
    return out is not None


def send_notification(summary: str, body: str = "", timeout: float = 2.0) -> None:
    """`notify-send`, skipped entirely (not attempted at all) if the binary
    isn't on PATH -- the task requirement's "skip if absent". A missing
    DBus session (binary present, nothing to notify) is left to fail
    silently via `procutil.run`'s own None-on-any-failure contract, same as
    every other best-effort call in this module."""
    if shutil.which("notify-send") is None:
        return
    procutil.run(["notify-send", summary, body], timeout=timeout)


# --- UPower.conf consistency alert (§11 R2, optional, never a trigger source) -----


def _read_percentage_action_from_file(path: Path) -> float | None:
    parser = configparser.ConfigParser()
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section("UPower"):
        return None
    raw = parser["UPower"].get("PercentageAction")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def read_upower_conf_percentage_action(
    conf_path: Path = DEFAULT_UPOWER_CONF_PATH, conf_d_dir: Path = DEFAULT_UPOWER_CONF_D_DIR
) -> float | None:
    """§11 R2: `UPower.conf`'s `PercentageAction`, with `conf.d/*.conf`
    drop-ins applied in sorted (lexicographic -- `NN-name.conf`) order, LATER
    files winning. Any single file missing, unreadable or unparseable is
    simply skipped -- a total parse failure yields `None`, which the caller
    treats as "nothing to compare" (alert-only; never disables the
    backstop, §11 R2)."""
    result = _read_percentage_action_from_file(conf_path)
    try:
        drop_ins = sorted(conf_d_dir.glob("*.conf")) if conf_d_dir.is_dir() else []
    except OSError:
        drop_ins = []
    for path in drop_ins:
        value = _read_percentage_action_from_file(path)
        if value is not None:
            result = value
    return result


def is_config_mismatched(configured_pct: float | None, upower_conf_pct: float | None, tolerance: float = 0.01) -> bool:
    """§11 R2's alert condition. Either value missing -> nothing to alert
    on (a parse failure must never manufacture a mismatch)."""
    if configured_pct is None or upower_conf_pct is None:
        return False
    return abs(configured_pct - upower_conf_pct) > tolerance


# --- pure decision logic (the backstop trigger, §11 R1) -----------------------


def in_danger_zone(pct: float | None, action_pct: float | None, margin: float = MARGIN_PCT) -> bool:
    """§11 R1: `pct <= action_pct - margin`, where `pct` is sysfs
    `battery.capacity` and `action_pct` is hwmon's OWN configured
    `backstop_action_pct` -- never a UPower reading. Either value missing ->
    not in the danger zone (never a crash, never a guess)."""
    if pct is None or action_pct is None:
        return False
    return pct <= (action_pct - margin)


@dataclass(frozen=True)
class Decision:
    """One tick's outcome. `daemon.py` turns `action` into DB writes and the
    real hibernate/notify calls; this class carries no side effects of its
    own, so it is trivial to assert against in tests."""

    action: str  # "none" | "hibernate" | "refuse"
    reason: str | None = None  # populated only for "refuse"


@dataclass
class HibernateBackstop:
    """Cross-tick state for R-L1.5: the 20-consecutive-sample counter and
    the one-attempt-per-episode latch (module docstring). Call `evaluate()`
    once per tick with this tick's `status`/`pct` (sysfs, §11 R1) and
    `action_pct` (hwmon config, §11 R2) and `sleep_blocked` (from the
    already-cached A13 `power_guard`); `preparing_for_sleep_fn` and
    `can_hibernate_fn` are called AT MOST ONCE PER EPISODE, lazily, only
    once the 20-sample threshold is actually reached -- not every tick,
    since both are real `busctl` subprocess calls and the common case (not
    near the threshold) must cost nothing at all.
    """

    required_consecutive: int = REQUIRED_CONSECUTIVE
    margin: float = MARGIN_PCT
    preparing_for_sleep_fn: Callable[[], bool | None] = read_preparing_for_sleep
    can_hibernate_fn: Callable[[], str | None] = read_can_hibernate
    consecutive: int = field(default=0, init=False)
    attempted_this_episode: bool = field(default=False, init=False)

    def evaluate(
        self,
        *,
        status: str | None,
        pct: float | None,
        action_pct: float | None,
        sleep_blocked: bool | None,
    ) -> Decision:
        danger = status == "Discharging" and in_danger_zone(pct, action_pct, self.margin)
        if not danger:
            # Episode over (or never started) -- reset both counters so the
            # NEXT episode gets its own fresh attempt.
            self.consecutive = 0
            self.attempted_this_episode = False
            return Decision(action="none")

        self.consecutive += 1
        if self.consecutive < self.required_consecutive or self.attempted_this_episode:
            return Decision(action="none")

        # Threshold just reached (or held) for the first time this episode.
        preparing = self.preparing_for_sleep_fn()
        if preparing is None:
            # SHOULD 1: could not check -> REFUSE, never proceed as if clear.
            self.attempted_this_episode = True
            return Decision(action="refuse", reason="preparing_for_sleep_unknown")
        if preparing:
            # Transient (UPower or someone else is already mid-sleep):
            # retry the very next tick rather than waiting for a whole new
            # episode, and don't count this as an attempt.
            return Decision(action="none")

        self.attempted_this_episode = True
        if sleep_blocked is True:
            return Decision(action="refuse", reason="sleep_blocked")
        if sleep_blocked is None:
            return Decision(action="refuse", reason="sleep_blocked_unknown")
        can_hibernate = self.can_hibernate_fn()
        if can_hibernate != "yes":
            return Decision(action="refuse", reason=f"can_hibernate={can_hibernate!r}")
        return Decision(action="hibernate")

    def reset(self) -> None:
        """Review item 6: called by `daemon.py` every tick `hibernate_backstop`
        is OFF, so the 20-sample counter and episode latch don't sit frozen
        while disabled -- without this, toggling the flag off then back on
        could resume from stale state (e.g. already at 19/20, or an episode
        already marked "attempted") instead of starting a fresh count."""
        self.consecutive = 0
        self.attempted_this_episode = False


@dataclass
class ConfigWarningState:
    """R2: "record could-not-check once" for a missing `backstop_action_pct`
    -- once per PROCESS (a config problem is not a per-tick transient, so
    there is no per-poll cadence to rate-limit against the way §11 R3's
    UPower reads are). Also holds the optional UPower.conf consistency
    alert's own one-shot latch (`alerted_config_mismatch`), which resets
    once the mismatch clears so a LATER mismatch (a different edit to
    either file) can alert again.

    `last_config_poll_count` (review item 4): the mismatch check reads a
    FILE (`UPower.conf`), which is cheap, but re-parsing it every 1s tick
    is still needless I/O for a value that only changes when
    `config.ConfigCache` itself refreshes (every `ttl_s`, default 10s).
    `daemon.py` only runs the mismatch check when this differs from the
    config loader's own `poll_count`."""

    warned_missing_action_pct: bool = field(default=False, init=False)
    alerted_config_mismatch: bool = field(default=False, init=False)
    last_config_poll_count: int | None = field(default=None, init=False)


# --- §11 R3: the UPower divergence standing check (never feeds the trigger) -------


@dataclass
class UPowerCache:
    """Caches `{pct, energy_full, energy_full_design}` together, at most
    once per `ttl_s` -- mirrors `inhibitors.InhibitorCache` exactly
    (monotonic clock, injectable `read_fn`/`clock`, `poll_count` for tests).
    §11 R3 only: none of these three values ever reaches
    `HibernateBackstop.evaluate()`. One `read_fn` call (NIT, review: `GetAll`
    instead of three `Get`s) rather than three separately-injectable
    functions -- `read_battery_upower_properties` is already the single
    source of truth for the shape `{pct, energy_full, energy_full_design}`.
    """

    ttl_s: float = DEFAULT_CACHE_TTL_S
    read_fn: Callable[[], dict] = read_battery_upower_properties
    clock: Callable[[], float] = time.monotonic
    poll_count: int = field(default=0, init=False)
    _last_poll_ts: float | None = field(default=None, init=False, repr=False)
    _last_result: dict = field(
        default_factory=lambda: {"pct": None, "energy_full": None, "energy_full_design": None},
        init=False,
        repr=False,
    )

    def get(self) -> dict:
        now = self.clock()
        stale = self._last_poll_ts is None or (now - self._last_poll_ts) >= self.ttl_s
        if stale:
            self._last_result = dict(self.read_fn())
            self._last_poll_ts = now
            self.poll_count += 1
        return dict(self._last_result)


def compute_upower_divergence(
    *,
    upower_pct: float | None,
    sysfs_pct: float | None,
    energy_full: float | None = None,
    energy_full_design: float | None = None,
    pct_threshold: float = DIVERGENCE_PCT_THRESHOLD,
    energy_ratio_threshold: float = DIVERGENCE_ENERGY_RATIO_THRESHOLD,
) -> dict:
    """§11 M1/R3: `{state, upower_pct, sysfs_pct}` for the snapshot's
    `recovery.upower` key -- the INSTANTANEOUS reading (one sample), unlike
    the 60s-sustained `upower_divergent` EVENT (`divergence_sustained`,
    below). `unknown` when either percentage is missing (a busctl/sysfs read
    failure -- three states, not two, `feedback_absent_is_not_zero`).
    `divergent` when the percentages differ by more than `pct_threshold`
    points, OR `energy_full / energy_full_design > energy_ratio_threshold`
    (measured live 2026-09-23: 9.96x on `battery_BAT0`) -- the pct signal is
    checked FIRST and short-circuits to `divergent` on its own, since it is
    the cheaper, always-available signal.

    Review item 3: a `energy_full_design` of exactly `0` (measured live on
    `DisplayDevice`, which is why R3 no longer reads that device at all) is
    an UNDEFINED ratio, not a healthy one -- when the pct signal alone says
    `ok` but the energy ratio can't be computed because its denominator is
    zero, the result is `unknown` for that signal (and therefore overall),
    never a silent `ok`. Positive controls from the same session: 33 vs 3.27
    -> `divergent`; 49 vs 47.3 -> `ok`.
    """
    if upower_pct is None or sysfs_pct is None:
        return {"state": "unknown", "upower_pct": upower_pct, "sysfs_pct": sysfs_pct}
    if abs(upower_pct - sysfs_pct) > pct_threshold:
        return {"state": "divergent", "upower_pct": upower_pct, "sysfs_pct": sysfs_pct}
    if energy_full is not None and energy_full_design is not None:
        if energy_full_design == 0:
            return {"state": "unknown", "upower_pct": upower_pct, "sysfs_pct": sysfs_pct}
        if (energy_full / energy_full_design) > energy_ratio_threshold:
            return {"state": "divergent", "upower_pct": upower_pct, "sysfs_pct": sysfs_pct}
    return {"state": "ok", "upower_pct": upower_pct, "sysfs_pct": sysfs_pct}


@dataclass
class DivergenceState:
    """Cross-tick holder for `divergence_sustained`'s `first_divergent_ts`
    -- `daemon.py` threads this the same way `snapshot.SampleState` threads
    `throttle_last_increase_ts`.

    `notified_without_boot_id` (review item 5): the `events` table's
    per-boot dedupe (`has_event_kind_for_boot`) is a SQL `boot_id = ?`
    comparison, which never matches when `boot_id IS NULL` (`x = NULL` is
    never true in SQL) -- so when `sensors.read_boot_id` itself fails, that
    dedupe silently does nothing and `daemon.py` would otherwise notify and
    insert an event on EVERY tick for as long as the divergence stays
    sustained. This flag is the per-process fallback for exactly that case
    (mirrors `critical_marker.State.checked_this_process`); it does not
    reset when the divergence clears, matching the per-boot event's own
    "once ever" semantics."""

    first_divergent_ts: float | None = field(default=None, init=False)
    notified_without_boot_id: bool = field(default=False, init=False)


def divergence_sustained(
    prev_first_divergent_ts: float | None, divergent: bool, now: float, sustain_s: float = DIVERGENCE_SUSTAIN_S
) -> tuple[bool, float | None]:
    """Mirrors `sensors.compute_throttle_recent`'s state-threading shape:
    returns `(sustained, new_first_divergent_ts)` -- `sustained` is True
    once `divergent` has held continuously (every call in between also
    `True`) for >= `sustain_s`. The caller threads `new_first_divergent_ts`
    back in as `prev_first_divergent_ts` on the next call; a single
    non-divergent reading resets the streak."""
    if not divergent:
        return False, None
    first_ts = prev_first_divergent_ts if prev_first_divergent_ts is not None else now
    return (now - first_ts) >= sustain_s, first_ts
