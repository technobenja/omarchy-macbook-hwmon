"""Build the `latest.json` snapshot (A2), validate its shape, write it atomically.

`tests/fixtures/latest.example.json` is the contract between this package
and the QML bar widget. Rather than reading that file off disk at runtime
(fragile once the package is copied to an install layout that may not carry
`tests/`), the exact same content is embedded here as `_REFERENCE_SNAPSHOT`.
A test (`tests/test_snapshot_shape.py`) asserts byte-for-value equality
between this constant and the committed fixture, so the two can never drift
without a test going red. `validate_shape()` uses this embedded reference by
default, which is what makes the shape check runnable both live (the daemon
calls it on every sample) and in tests (which may also pass an explicit
`reference` loaded straight from the fixture file).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import sensors

# v3 delta, M8: schema 1 -> 2 (A13 power_guard, A14 fan.target_rpm/control,
# A15 cpu.throttle). The widget treats any schema other than the current one
# as not-live (A9), so the collector and the bar widget ship together.
SCHEMA_VERSION = 2
DEFAULT_DISK_DEVICE = "sda"
DEFAULT_RUN_ROOT = Path("/run")

#: A13: the null placeholder used when the caller doesn't supply a live
#: `power_guard` reading (e.g. a test building a snapshot with no busctl
#: involved at all). `blockers` is always a list, never null (S7).
_NULL_POWER_GUARD: dict = {"sleep_blocked": None, "blockers": []}

# Keys that must always be present and non-null (everything else is nullable
# per A2: "Every leaf is nullable (null = could not read) except schema and ts").
_NON_NULLABLE = frozenset({"schema", "ts"})

# The exact content of tests/fixtures/latest.example.json, kept in sync by
# tests/test_snapshot_shape.py. Do not hand-edit this without also updating
# (or checking against) the fixture — they are asserted equal.
_REFERENCE_SNAPSHOT: dict = {
    "schema": 2,
    "ts": 1790179200.0,
    "battery": {
        "pct": 81,
        "status": "Discharging",
        "power_w": -15.63,
        "voltage_v": 11.247,
        "current_a": 1.39,
        "temp_c": 34.5,
        "cycles": 3,
        "health_pct": 104.84,
        "charge_now_ah": 5.435,
        "charge_full_ah": 6.71,
        "charge_design_ah": 6.4,
    },
    "ac": {"online": False},
    "power_guard": {
        "sleep_blocked": True,
        "blockers": [
            {"who": "omarchy-update", "why": "Omarchy update in progress"},
        ],
    },
    "cpu": {
        "package_c": 59.0,
        "cores_c": {"Core 0": 58.0, "Core 1": 59.0},
        "load": [0.82, 0.61, 0.55],
        "usage_pct": 12.4,
        "per_core": [
            {"usage_pct": 14.0, "freq_mhz": 1900.0},
            {"usage_pct": 9.5, "freq_mhz": 1400.0},
            {"usage_pct": 16.2, "freq_mhz": 2100.0},
            {"usage_pct": 9.9, "freq_mhz": 1300.0},
        ],
        "throttle": {"core_count": 3, "package_count": 3, "recent": False},
    },
    "fan": {
        "label": "Right Side",
        "rpm": 1292,
        "min_rpm": 1299,
        "max_rpm": 6199,
        "manual": True,
        "target_rpm": 2272,
        "control": "mbpfan",
    },
    "temps": {
        "TC1C": 61.0,
        "TCGC": 63.0,
        "TCSA": 63.0,
        "TCXC": 63.25,
        "TH0A": 43.5,
        "TH0B": 42.5,
        "TH0V": 41.5,
        "TA0P": 41.25,
        "TH0a": 43.5,
        "TH0b": 42.5,
        "TM0P": 47.25,
        "TPCD": 62.0,
        "Th1H": 46.5,
        "Ts0P": 30.0,
        "TB0T": 35.25,
        "Ts0S": 38.75,
        "Ts1S": 37.0,
        "TB1T": 29.25,
        "TB2T": 33.0,
        "TBXT": 35.25,
        "TC0C": 62.0,
        "TC0E": 66.25,
        "TC0F": 68.25,
        "TC0P": 58.0,
    },
    "sensors_invalid": ["TH0C", "TH0F", "TH0R", "TH0c", "THSP", "TMLB", "TW0P"],
    "system": {
        "mem_used_bytes": 3221225472,
        "mem_total_bytes": 8266911744,
        "swap_used_bytes": 0,
        "swap_total_bytes": 4133486592,
        "disk": {"device": "sda", "read_bps": 0.0, "write_bps": 40960.0},
        "net": {"iface": "wlp3s0", "rx_bps": 1520.0, "tx_bps": 880.0},
    },
}


@dataclass(frozen=True)
class SampleState:
    """Everything the next sample needs to compute rates. Opaque to callers."""

    ts: float
    cpu_times: dict[str, sensors.CpuTimes] | None
    disk_raw: tuple[int, int] | None
    net_raw: tuple[int, int] | None
    net_iface: str | None
    # A15/S7: cpu.throttle.recent's cross-tick state -- the last-seen counts
    # (to detect an increase) and the last time either one increased.
    throttle_core_count: int | None = None
    throttle_package_count: int | None = None
    throttle_last_increase_ts: float | None = None


def build_snapshot(
    sysfs_root: Path,
    procfs_root: Path,
    prev: SampleState | None,
    *,
    now: float | None = None,
    disk_device: str = DEFAULT_DISK_DEVICE,
    run_root: Path = DEFAULT_RUN_ROOT,
    power_guard: dict | None = None,
) -> tuple[dict, SampleState]:
    """Read every sensor and assemble one A2 snapshot.

    Returns (snapshot, new_state); pass `new_state` back in as `prev` on the
    next call so rate metrics (cpu.usage_pct, per_core usage, disk/net bps,
    cpu.throttle.recent) have something to diff against. `prev=None` (the
    first sample) yields `null` for every rate field, per spec.

    `power_guard` (A13) is a precomputed `{sleep_blocked, blockers}` dict --
    this function does not shell out to `busctl` itself (that belongs to
    `inhibitors.InhibitorCache`, which owns the "at most every 10 s" cache
    across ticks; a pure per-sample builder has no place to keep that
    state). Omitting it (the default) yields the A13 null placeholder,
    which is what every test that doesn't care about power_guard gets.
    """
    ts = time.time() if now is None else now

    dt: float | None = None
    if prev is not None:
        candidate_dt = ts - prev.ts
        if candidate_dt > 0:
            dt = candidate_dt

    battery = sensors.read_battery(sysfs_root)
    ac = sensors.read_ac(sysfs_root)
    package_c, cores_c = sensors.read_cpu_temps(sysfs_root)
    load = sensors.read_loadavg(procfs_root)
    fan = sensors.read_fan(sysfs_root, run_root=run_root, procfs_root=procfs_root)
    temps, invalid = sensors.read_smc_temps(sysfs_root)
    mem = sensors.read_mem(procfs_root)

    throttle_raw = sensors.read_cpu_throttle(sysfs_root)
    cur_core_count = throttle_raw["core_count"]
    cur_package_count = throttle_raw["package_count"]
    prev_core_count = prev.throttle_core_count if prev is not None else None
    prev_package_count = prev.throttle_package_count if prev is not None else None
    prev_last_increase_ts = prev.throttle_last_increase_ts if prev is not None else None
    throttle_recent, throttle_last_increase_ts = sensors.compute_throttle_recent(
        prev_core_count,
        prev_package_count,
        prev_last_increase_ts,
        cur_core_count,
        cur_package_count,
        now=ts,
    )

    cpu_times = sensors.read_cpu_times(procfs_root)
    usage_pct = None
    per_core: list[dict] = []
    if cpu_times is not None:
        prev_times = prev.cpu_times if prev is not None else None
        overall_prev = prev_times.get("cpu") if prev_times else None
        usage_pct = sensors.cpu_usage_pct(overall_prev, cpu_times.get("cpu"))
        i = 0
        while f"cpu{i}" in cpu_times:
            core_prev = prev_times.get(f"cpu{i}") if prev_times else None
            core_usage = sensors.cpu_usage_pct(core_prev, cpu_times[f"cpu{i}"])
            freq = sensors.read_core_freq_mhz(sysfs_root, i)
            per_core.append({"usage_pct": core_usage, "freq_mhz": freq})
            i += 1

    disk_raw = sensors.read_disk_raw(procfs_root, disk_device)
    read_bps, write_bps = sensors.disk_rate(
        prev.disk_raw if prev is not None else None, disk_raw, dt
    )

    iface = sensors.read_default_iface(procfs_root)
    net: dict | None
    net_raw: tuple[int, int] | None = None
    if iface is None:
        net = None
    else:
        net_raw = sensors.read_netdev_raw(procfs_root, iface)
        rx_bps = tx_bps = None
        same_iface = prev is not None and prev.net_iface == iface
        if same_iface:
            rx_bps, tx_bps = sensors.net_rate(prev.net_raw, net_raw, dt)
        net = {"iface": iface, "rx_bps": rx_bps, "tx_bps": tx_bps}

    snapshot = {
        "schema": SCHEMA_VERSION,
        "ts": ts,
        "battery": battery,
        "ac": ac,
        "power_guard": dict(power_guard) if power_guard is not None else dict(_NULL_POWER_GUARD),
        "cpu": {
            "package_c": package_c,
            "cores_c": cores_c,
            "load": load,
            "usage_pct": usage_pct,
            "per_core": per_core,
            "throttle": {
                "core_count": cur_core_count,
                "package_count": cur_package_count,
                "recent": throttle_recent,
            },
        },
        "fan": fan,
        "temps": temps,
        "sensors_invalid": invalid,
        "system": {
            **mem,
            "disk": {"device": disk_device, "read_bps": read_bps, "write_bps": write_bps},
            "net": net,
        },
    }
    new_state = SampleState(
        ts=ts,
        cpu_times=cpu_times,
        disk_raw=disk_raw,
        net_raw=net_raw,
        net_iface=iface,
        throttle_core_count=cur_core_count,
        throttle_package_count=cur_package_count,
        throttle_last_increase_ts=throttle_last_increase_ts,
    )
    return snapshot, new_state


# --- shape validation (acceptance check 0) ---------------------------------------

# `temps` and `cpu.cores_c` are label -> float maps where the *label set* is
# data (which SMC/coretemp sensors currently read as valid, per A3), not
# schema. Every other dict in the snapshot has a fixed, spec-defined key set
# and is still checked exactly.
#
# Corrected 2026-09-23 (spec A3 dated note): before this, these two were
# checked like any other dict -- an exact key match against the fixture.
# Live symptom: SMC sensor TH0F drifted from -43C (invalid under the old A3
# cutoff) to -34.75C (a real float reading, not in the committed fixture's
# `temps`), which the exact-key check flagged as "unexpected key" every
# single tick forever after. A3's own invalid-sensor filter already decides
# which labels are present each tick; validate_shape must not re-decide it.
_DYNAMIC_LABEL_MAPS = frozenset({"temps", "cpu.cores_c"})


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def validate_shape(candidate: dict, reference: dict | None = None) -> list[str]:
    """Compare `candidate`'s key set and leaf types against `reference`.

    Returns a list of human-readable error strings; empty means the shape
    matches. Defaults to the embedded fixture-equivalent reference so the
    daemon can call this on every live sample with no extra file
    dependency; tests may pass the fixture loaded straight from disk.
    """
    if reference is None:
        reference = _REFERENCE_SNAPSHOT
    errors: list[str] = []
    _walk(candidate, reference, "", errors)
    return errors


def _walk(candidate, reference, path: str, errors: list[str]) -> None:
    label = path or "<root>"
    if isinstance(reference, dict):
        if not isinstance(candidate, dict):
            errors.append(f"{label}: expected object, got {_type_name(candidate)}")
            return
        if path in _DYNAMIC_LABEL_MAPS:
            # Structural check only: every key is a label (a string) and
            # every value matches the map's element type (float, nullable)
            # -- the key SET itself is not compared to `reference` at all.
            elem_template = next(iter(reference.values()), 0.0)
            for key, value in candidate.items():
                child_path = f"{path}.{key}" if path else str(key)
                if not isinstance(key, str):
                    errors.append(f"{child_path}: label key must be a string, got {_type_name(key)}")
                    continue
                _walk(value, elem_template, child_path, errors)
            return
        for key in reference:
            child_path = f"{path}.{key}" if path else key
            if key not in candidate:
                errors.append(f"{child_path}: missing key")
                continue
            _walk(candidate[key], reference[key], child_path, errors)
        for key in candidate:
            if key not in reference:
                child_path = f"{path}.{key}" if path else key
                errors.append(f"{child_path}: unexpected key")
        return

    if isinstance(reference, list):
        if not isinstance(candidate, list):
            errors.append(f"{label}: expected array, got {_type_name(candidate)}")
            return
        if path == "cpu.load" and len(candidate) != 3:
            errors.append(f"{label}: expected 3 elements, got {len(candidate)}")
        if reference:
            template = reference[0]
            for i, item in enumerate(candidate):
                _walk(item, template, f"{path}[{i}]", errors)
        return

    # scalar leaf
    nullable = path not in _NON_NULLABLE
    if candidate is None:
        if not nullable:
            errors.append(f"{label}: must not be null")
        return
    if _type_name(candidate) != _type_name(reference):
        errors.append(f"{label}: expected {_type_name(reference)}, got {_type_name(candidate)}")


# --- atomic write ------------------------------------------------------------


def write_atomic(path: Path, snapshot: dict) -> None:
    """Write `snapshot` as JSON to `path` via a tmp file + rename (A1/A6)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snapshot, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
