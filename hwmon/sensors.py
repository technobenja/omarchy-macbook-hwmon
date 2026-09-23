"""Stateless sysfs / procfs readers for the hwmon collector.

Every public reader takes a ``sysfs_root`` and/or ``procfs_root`` (a
:class:`pathlib.Path`, normally ``Path("/sys")`` / ``Path("/proc")``) so
tests can substitute a fixture tree for the real filesystem — see
``specs/spec.md`` A2: "All readers take a sysfs_root / procfs_root
parameter so fixture trees can stand in for the real ones."

A missing or unreadable file always yields ``None`` for that field; it
never raises. Rate-based readers (CPU usage, disk, net) are pure functions
over an explicit "previous sample" so the caller (``snapshot.py``) owns the
only bit of state in the whole pipeline.

Two things ARE memoized across calls, purely as a performance optimization
(measured: an uncached tick costs ~22ms of CPU on this machine's Haswell-ULT
core, mostly `open()`/`read()` syscall overhead across ~110 small sysfs
files, which blew the < 1% CPU budget in A6/acceptance check 6):

- ``find_hwmon_by_name`` — which hwmon class entry is "coretemp" / "applesmc"
  does not change while the daemon runs, so the directory scan + name-file
  reads only need to happen once per (sysfs_root, name).
- the SMC/coretemp ``tempN_label`` -> index mapping — sensor labels are
  fixed hardware strings; only the paired ``tempN_input`` needs a fresh read
  every tick.

Both caches are keyed on the exact ``sysfs_root`` Path, so distinct test
trees (each built under its own ``tempfile.mkdtemp()``) never collide, and
``clear_discovery_cache()`` is available for any test that deliberately
mutates a tree it has already discovered.
"""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path

# --- A3: invalid-sensor filter -------------------------------------------------
#
# Corrected 2026-09-23 (spec A3): -40C -> 0C. Live on this machine, SMC
# sensor TH0F -- known junk since it originally read -43C -- drifted to
# -34.75C, which the old -40C cutoff no longer caught (it started showing up
# in `temps` as a real reading). Nothing inside a running laptop is below
# freezing, so 0C is still a deliberately generous floor, not a tight one.

INVALID_MIN_C = 0.0
INVALID_MAX_C = 130.0

# --- low-level file readers -----------------------------------------------------
#
# These use raw os.open/os.read/os.close (accepting either a Path or a plain
# str) rather than Path.read_text(): on the ~60 tiny one-line files read
# every tick, pathlib's object/str-conversion overhead (__fspath__, drive
# parsing, TextIOWrapper construction...) measurably outweighed the actual
# syscall cost -- profiling this daemon showed it eating a double-digit
# percentage of a tick's CPU time before this change.


def read_text(path: Path | str) -> str | None:
    """Read a sysfs/procfs file as stripped text, or ``None`` on any failure.

    Some sysfs attributes (measured live on this machine: two of the 31 SMC
    temp sensors, intermittently) fail not at open() but at read() time --
    the kernel driver returns -EIO/-ENODATA when it briefly can't reach the
    hardware. That must be just as non-fatal as a missing file.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        data = os.read(fd, 4096)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return data.decode().strip()
    except UnicodeDecodeError:
        return None


def read_int(path: Path | str) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


# --- hwmon discovery -------------------------------------------------------------


_LABEL_RE = re.compile(r"^temp(\d+)_label$")


@functools.lru_cache(maxsize=32)
def find_hwmon_by_name(sysfs_root: Path, name: str) -> Path | None:
    """Return the directory holding a hwmon device's attribute files.

    Discovery is by the device's ``name`` file, never by index — hwmon
    numbering is not stable across boots (spec A2 "Discovery rules").

    Some drivers (e.g. ``coretemp``) publish ``name`` and the ``tempN_*``
    attribute files directly under ``/sys/class/hwmon/hwmonN/``. Others
    (e.g. ``applesmc`` on this machine) publish an empty ``hwmonN/`` with no
    ``name`` file at all — the real ``name`` and attribute files live on the
    parent platform device, reachable via ``hwmonN/device/`` (measured
    2026-09-23: ``hwmon2`` has no ``name`` file; ``hwmon2/device/name`` ==
    ``"applesmc"``, and ``hwmon2/device/fan1_input`` etc. exist there).
    Both layouts are checked.

    Memoized per (sysfs_root, name) — see the module docstring.
    """
    hwmon_class = sysfs_root / "class" / "hwmon"
    if not hwmon_class.is_dir():
        return None
    for entry in sorted(hwmon_class.iterdir()):
        if read_text(entry / "name") == name:
            return entry
        if read_text(entry / "device" / "name") == name:
            return entry / "device"
    return None


@functools.lru_cache(maxsize=32)
def _label_index(base: Path) -> tuple[tuple[str, str], ...]:
    """One-time ``(input_path, label)`` enumeration of a hwmon device's
    ``tempN_label`` files, cached per `base` — see the module docstring.

    The paired ``tempN_input`` path is precomputed as a plain string (not a
    Path) so the per-tick hot loop in read_cpu_temps/read_smc_temps never
    constructs a Path object at all -- just os.open()s a ready-made string.
    """
    result: list[tuple[str, str]] = []
    for label_file in sorted(base.glob("temp*_label")):
        match = _LABEL_RE.match(label_file.name)
        if not match:
            continue
        label = read_text(label_file)
        if label is not None:
            idx = match.group(1)
            result.append((f"{base}/temp{idx}_input", label))
    return tuple(result)


def clear_discovery_cache() -> None:
    """Drop the memoized hwmon discovery / label-index results. Only needed
    by tests that deliberately mutate a sysfs tree after it has already been
    discovered once under the same path; the daemon never needs this."""
    find_hwmon_by_name.cache_clear()
    _label_index.cache_clear()


def power_supply_path(sysfs_root: Path, name: str) -> Path:
    return sysfs_root / "class" / "power_supply" / name


# --- battery / AC ------------------------------------------------------------


def read_battery(sysfs_root: Path) -> dict:
    base = power_supply_path(sysfs_root, "BAT0")
    pct = read_int(base / "capacity")
    status = read_text(base / "status")
    voltage_now = read_int(base / "voltage_now")  # microvolts
    current_now = read_int(base / "current_now")  # microamps, unsigned magnitude
    temp_deci_c = read_int(base / "temp")  # deci-degrees C
    cycles = read_int(base / "cycle_count")
    charge_now = read_int(base / "charge_now")  # microamp-hours
    charge_full = read_int(base / "charge_full")
    charge_design = read_int(base / "charge_full_design")

    voltage_v = None if voltage_now is None else round(voltage_now / 1_000_000, 3)
    current_a = None if current_now is None else round(current_now / 1_000_000, 3)
    temp_c = None if temp_deci_c is None else round(temp_deci_c / 10, 1)
    charge_now_ah = None if charge_now is None else round(charge_now / 1_000_000, 3)
    charge_full_ah = None if charge_full is None else round(charge_full / 1_000_000, 3)
    charge_design_ah = None if charge_design is None else round(charge_design / 1_000_000, 3)

    health_pct = None
    if charge_full is not None and charge_design:
        health_pct = round(charge_full / charge_design * 100, 2)

    # A4: power_w is signed by status, not by the (unsigned) current_now magnitude.
    power_w = None
    if voltage_now is not None and current_now is not None and status is not None:
        magnitude = round((voltage_now / 1_000_000) * (current_now / 1_000_000), 2)
        if status == "Discharging":
            power_w = -magnitude
        elif status == "Charging":
            power_w = magnitude
        else:
            power_w = 0.0

    return {
        "pct": pct,
        "status": status,
        "power_w": power_w,
        "voltage_v": voltage_v,
        "current_a": current_a,
        "temp_c": temp_c,
        "cycles": cycles,
        "health_pct": health_pct,
        "charge_now_ah": charge_now_ah,
        "charge_full_ah": charge_full_ah,
        "charge_design_ah": charge_design_ah,
    }


def read_ac(sysfs_root: Path) -> dict:
    base = power_supply_path(sysfs_root, "ADP1")
    online = read_int(base / "online")
    return {"online": None if online is None else bool(online)}


# --- CPU temps (coretemp) -----------------------------------------------------


def read_cpu_temps(sysfs_root: Path) -> tuple[float | None, dict[str, float]]:
    """Return (package_c, {core_label: temp_c}) from the coretemp hwmon device."""
    base = find_hwmon_by_name(sysfs_root, "coretemp")
    package_c: float | None = None
    cores_c: dict[str, float] = {}
    if base is None:
        return package_c, cores_c
    for input_path, label in _label_index(base):
        raw = read_int(input_path)
        value = None if raw is None else round(raw / 1000, 3)
        if label == "Package id 0":
            package_c = value
        elif label.startswith("Core") and value is not None:
            cores_c[label] = value
    return package_c, cores_c


# --- fan -----------------------------------------------------------------------


def read_fan(sysfs_root: Path) -> dict:
    base = find_hwmon_by_name(sysfs_root, "applesmc")
    if base is None:
        return {"label": None, "rpm": None, "min_rpm": None, "max_rpm": None, "manual": None}
    label = read_text(base / "fan1_label")
    if label is not None:
        label = label.strip()
    rpm = read_int(base / "fan1_input")
    min_rpm = read_int(base / "fan1_min")
    max_rpm = read_int(base / "fan1_max")
    manual_raw = read_int(base / "fan1_manual")
    manual = None if manual_raw is None else bool(manual_raw)
    return {"label": label, "rpm": rpm, "min_rpm": min_rpm, "max_rpm": max_rpm, "manual": manual}


# --- SMC temps + A3 invalid filter ---------------------------------------------


def read_smc_temps(sysfs_root: Path) -> tuple[dict[str, float], list[str]]:
    """Return (valid {label: temp_c}, sorted [invalid labels]) per A3.

    A reading <= 0C or >= 130C is invalid and lands in the invalid list,
    never in the valid dict.
    """
    base = find_hwmon_by_name(sysfs_root, "applesmc")
    temps: dict[str, float] = {}
    invalid: list[str] = []
    if base is None:
        return temps, invalid
    for input_path, label in _label_index(base):
        raw = read_int(input_path)
        if raw is None:
            continue
        value = raw / 1000
        if value <= INVALID_MIN_C or value >= INVALID_MAX_C:
            invalid.append(label)
        else:
            temps[label] = round(value, 3)
    return temps, sorted(invalid)


# --- load average ----------------------------------------------------------------


def read_loadavg(procfs_root: Path) -> list[float | None]:
    text = read_text(procfs_root / "loadavg")
    if text is None:
        return [None, None, None]
    parts = text.split()
    try:
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except (IndexError, ValueError):
        return [None, None, None]


# --- CPU usage (rate over /proc/stat jiffies) ------------------------------------


@dataclass(frozen=True)
class CpuTimes:
    total: int
    idle: int


def read_cpu_times(procfs_root: Path) -> dict[str, CpuTimes] | None:
    """Parse /proc/stat's `cpu` / `cpuN` lines into total/idle jiffie counters."""
    text = read_text(procfs_root / "stat")
    if text is None:
        return None
    times: dict[str, CpuTimes] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("cpu"):
            continue
        name = parts[0]
        if name != "cpu" and not name[3:].isdigit():
            continue
        try:
            nums = [int(x) for x in parts[1:]]
        except ValueError:
            continue
        if len(nums) < 4:
            continue
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        times[name] = CpuTimes(total=sum(nums), idle=idle)
    return times


def cpu_usage_pct(prev: CpuTimes | None, cur: CpuTimes | None) -> float | None:
    """Percent busy between two /proc/stat samples; None on the first sample."""
    if prev is None or cur is None:
        return None
    dt = cur.total - prev.total
    if dt <= 0:
        return None
    idle_delta = cur.idle - prev.idle
    pct = (dt - idle_delta) / dt * 100
    return round(max(0.0, min(100.0, pct)), 1)


def read_core_freq_mhz(sysfs_root: Path, cpu_index: int) -> float | None:
    raw = read_int(
        sysfs_root / "devices" / "system" / "cpu" / f"cpu{cpu_index}" / "cpufreq" / "scaling_cur_freq"
    )
    return None if raw is None else round(raw / 1000, 1)


# --- memory ------------------------------------------------------------------


def read_mem(procfs_root: Path) -> dict:
    text = read_text(procfs_root / "meminfo")
    empty = {
        "mem_used_bytes": None,
        "mem_total_bytes": None,
        "swap_used_bytes": None,
        "swap_total_bytes": None,
    }
    if text is None:
        return empty
    kv: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        rest = rest.strip().split()
        if not rest:
            continue
        try:
            kv[key.strip()] = int(rest[0]) * 1024  # values are in kB
        except ValueError:
            continue
    mem_total = kv.get("MemTotal")
    mem_avail = kv.get("MemAvailable")
    swap_total = kv.get("SwapTotal")
    swap_free = kv.get("SwapFree")
    mem_used = None if mem_total is None or mem_avail is None else mem_total - mem_avail
    swap_used = None if swap_total is None or swap_free is None else swap_total - swap_free
    return {
        "mem_used_bytes": mem_used,
        "mem_total_bytes": mem_total,
        "swap_used_bytes": swap_used,
        "swap_total_bytes": swap_total,
    }


# --- disk (rate over /proc/diskstats sectors) ------------------------------------

_SECTOR_BYTES = 512


def read_disk_raw(procfs_root: Path, device: str = "sda") -> tuple[int, int] | None:
    """Return (sectors_read, sectors_written) for `device`, or None if absent."""
    text = read_text(procfs_root / "diskstats")
    if text is None:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or parts[2] != device:
            continue
        try:
            return int(parts[5]), int(parts[9])
        except ValueError:
            return None
    return None


def disk_rate(
    prev: tuple[int, int] | None, cur: tuple[int, int] | None, dt: float | None
) -> tuple[float | None, float | None]:
    if prev is None or cur is None or not dt or dt <= 0:
        return None, None
    read_bps = round((cur[0] - prev[0]) * _SECTOR_BYTES / dt, 1)
    write_bps = round((cur[1] - prev[1]) * _SECTOR_BYTES / dt, 1)
    return read_bps, write_bps


# --- net (rate over /proc/net/dev bytes, default-route iface) -------------------


def read_default_iface(procfs_root: Path) -> str | None:
    text = read_text(procfs_root / "net" / "route")
    if text is None:
        return None
    lines = text.splitlines()[1:]  # skip header
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        iface, dest = parts[0], parts[1]
        if dest == "00000000":
            return iface
    return None


def read_netdev_raw(procfs_root: Path, iface: str) -> tuple[int, int] | None:
    """Return (rx_bytes, tx_bytes) for `iface` from /proc/net/dev, or None."""
    text = read_text(procfs_root / "net" / "dev")
    if text is None:
        return None
    for line in text.splitlines()[2:]:  # skip the two header lines
        name, sep, rest = line.partition(":")
        if not sep or name.strip() != iface:
            continue
        fields = rest.split()
        if len(fields) < 9:
            return None
        try:
            return int(fields[0]), int(fields[8])
        except ValueError:
            return None
    return None


def net_rate(
    prev: tuple[int, int] | None, cur: tuple[int, int] | None, dt: float | None
) -> tuple[float | None, float | None]:
    if prev is None or cur is None or not dt or dt <= 0:
        return None, None
    rx_bps = round((cur[0] - prev[0]) / dt, 1)
    tx_bps = round((cur[1] - prev[1]) / dt, 1)
    return rx_bps, tx_bps
