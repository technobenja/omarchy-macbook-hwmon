"""Local hwmon configuration -- read-only, optional, and small on purpose.

Two keys, both for `backstop.py` (R-L1.5 of the deliverables spec,
`omarchy-power-loss-recovery/SPEC.md`): `hibernate_backstop` (bool, default
`false`) and `backstop_action_pct` (float, default `None`, §11 R2 -- there
is NO default that enables anything, since `PercentageAction` turned out not
to even be a real D-Bus property to fall back to). The spec explicitly gates
any automatic hibernate action on a supervised round-trip test (R-L1.1)
that has NOT happened yet, so the backstop must default to fully inert
until a human sets BOTH keys here -- never the other way around.

`$XDG_CONFIG_HOME/hwmon/config.json`, falling back to `~/.config/hwmon/config.json`
(mirrors `daemon.default_state_dir`'s own `$XDG_RUNTIME_DIR` / fallback
convention), e.g.:

    {"hibernate_backstop": true, "backstop_action_pct": 8}

Any read failure -- missing file, unreadable, invalid JSON, wrong type for a
known key -- yields the all-defaults config, never an exception: a
misconfigured or absent file must fail SAFE (backstop off / no threshold),
not crash the daemon and not silently turn a safety-relevant feature on.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

#: The only keys this module currently knows about. Unknown keys in the
#: file are ignored, not an error -- forward compatibility for a config
#: file a future hwmon feature also wants to read.
DEFAULTS: dict = {"hibernate_backstop": False, "backstop_action_pct": None}

DEFAULT_CACHE_TTL_S = 10.0


def default_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "hwmon" / "config.json"


def load_config(config_path: Path | None = None) -> dict:
    """Return the effective config: `DEFAULTS` overridden by whatever valid
    keys `config_path` (default: `default_config_path()`) holds. Never
    raises -- called every daemon tick (so flipping the file live takes
    effect without a restart), and a read hiccup on a config file must be as
    harmless as a missing one.
    """
    path = config_path if config_path is not None else default_config_path()
    config = dict(DEFAULTS)
    try:
        text = path.read_text()
    except OSError:
        return config
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return config
    if not isinstance(data, dict):
        return config
    hibernate_backstop = data.get("hibernate_backstop")
    if isinstance(hibernate_backstop, bool):
        config["hibernate_backstop"] = hibernate_backstop
    if "backstop_action_pct" in data:
        config["backstop_action_pct"] = _validate_action_pct(data["backstop_action_pct"])
    return config


def _validate_action_pct(action_pct: object) -> float | None:
    """Round-2 review, BLOCKER: a battery-percentage threshold that is
    `Infinity`/`NaN`/negative/zero/over 100 is not a config typo to shrug
    off, it is a value that would make the trigger comparison
    (`in_danger_zone`) nonsensical or permanently true/false -- so this
    validates the RANGE, not just the JSON type. Rejected values yield
    `None` (never a default that enables anything, §11 R2) plus ONE warning
    line naming what was rejected and why."""
    if isinstance(action_pct, bool):
        _warn_rejected_action_pct(action_pct, "must be a number, not a bool")
        return None
    if not isinstance(action_pct, (int, float)):
        _warn_rejected_action_pct(action_pct, "must be a number")
        return None
    value = float(action_pct)
    if math.isnan(value) or math.isinf(value):
        _warn_rejected_action_pct(action_pct, "must be finite")
        return None
    if not (0 < value <= 100):
        _warn_rejected_action_pct(action_pct, "must satisfy 0 < backstop_action_pct <= 100")
        return None
    return value


def _warn_rejected_action_pct(action_pct: object, reason: str) -> None:
    print(f"hwmon: config: rejecting backstop_action_pct={action_pct!r}: {reason}", file=sys.stderr)


@dataclass
class ConfigCache:
    """SHOULD 2 (review): `config.load_config()` reads and JSON-parses a
    file, so calling it every 1 s tick (as `daemon.py` originally did, so a
    live edit takes effect within one tick) is needless per-tick I/O.
    Caches at most once per `ttl_s` (default 10 s) -- mirrors
    `inhibitors.InhibitorCache` exactly (monotonic clock, injectable
    `load_fn`/`clock`, `poll_count` for tests). Implements `__call__` so an
    instance is a drop-in replacement for the plain `load_config` function
    `daemon.run`'s `config_loader` parameter expects.
    """

    ttl_s: float = DEFAULT_CACHE_TTL_S
    config_path: Path | None = None
    load_fn: Callable[[Path | None], dict] = load_config
    clock: Callable[[], float] = time.monotonic
    poll_count: int = field(default=0, init=False)
    _last_poll_ts: float | None = field(default=None, init=False, repr=False)
    _last_result: dict = field(default_factory=lambda: dict(DEFAULTS), init=False, repr=False)

    def get(self) -> dict:
        now = self.clock()
        stale = self._last_poll_ts is None or (now - self._last_poll_ts) >= self.ttl_s
        if stale:
            self._last_result = self.load_fn(self.config_path)
            self._last_poll_ts = now
            self.poll_count += 1
        return self._last_result

    def __call__(self) -> dict:
        return self.get()
