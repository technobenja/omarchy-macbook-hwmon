"""Builders for fake sysfs/procfs trees, used by the tests instead of a
committed fixture tree — real hwmon sysfs layouts are naturally reproducible
from a handful of small helpers, and building them in code keeps the tricky
cases (a hwmon dir with no `name` file, deci-degree battery temps, trailing
whitespace in `fan1_label`) explicit at the call site of each test.

The layouts mirror what was measured on this machine (omarchy, 2026-09-23):
- `coretemp` publishes `name` and `tempN_*` directly under its
  `/sys/class/hwmon/hwmonN/` entry.
- `applesmc` publishes an *empty* `hwmonN/` (no `name` file at all) whose
  `device` entry (real dir here, a symlink on real hardware — `sensors.py`
  doesn't care which) holds `name`, `fan1_*` and `tempN_*`.
"""

from __future__ import annotations

from pathlib import Path


def hwmon_class_dir(sysfs_root: Path) -> Path:
    d = sysfs_root / "class" / "hwmon"
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_coretemp(
    sysfs_root: Path,
    index: int,
    *,
    package_milli_c: int | None = 59000,
    cores_milli_c: dict[str, int] | None = None,
) -> Path:
    """`/sys/class/hwmon/hwmonN/` with `name`, `tempN_label`, `tempN_input`
    directly inside it (coretemp's real layout)."""
    if cores_milli_c is None:
        cores_milli_c = {"Core 0": 58000, "Core 1": 59000}
    base = hwmon_class_dir(sysfs_root) / f"hwmon{index}"
    base.mkdir(parents=True, exist_ok=True)
    (base / "name").write_text("coretemp\n")
    n = 1
    if package_milli_c is not None:
        (base / f"temp{n}_label").write_text("Package id 0\n")
        (base / f"temp{n}_input").write_text(f"{package_milli_c}\n")
        n += 1
    for label, milli_c in cores_milli_c.items():
        (base / f"temp{n}_label").write_text(f"{label}\n")
        (base / f"temp{n}_input").write_text(f"{milli_c}\n")
        n += 1
    return base


def add_applesmc(
    sysfs_root: Path,
    index: int,
    *,
    fan_label: str = "Right Side  ",
    fan_rpm: int | None = 1292,
    fan_min: int | None = 1299,
    fan_max: int | None = 6199,
    fan_manual: int | None = 0,
    fan_output: int | None = None,
    temps_milli_c: dict[str, int] | None = None,
    name_file_on_class_entry: bool = False,
) -> Path:
    """`/sys/class/hwmon/hwmonN/` with NO `name` file (matches real
    hardware); the actual attributes live under `hwmonN/device/`, which
    `find_hwmon_by_name` also checks. Returns the `device` dir (where the
    attribute files live), mirroring what `find_hwmon_by_name` returns.

    Set `name_file_on_class_entry=True` to instead put `name` directly on
    the class entry (the coretemp-style layout) — used to prove discovery
    also works that way, in case a future driver changes shape.
    """
    if temps_milli_c is None:
        temps_milli_c = {"TA0P": 41250, "TB0T": 35250}
    class_entry = hwmon_class_dir(sysfs_root) / f"hwmon{index}"
    class_entry.mkdir(parents=True, exist_ok=True)
    device = class_entry / "device"
    device.mkdir(parents=True, exist_ok=True)

    name_target = class_entry if name_file_on_class_entry else device
    (name_target / "name").write_text("applesmc\n")

    if fan_label is not None:
        (device / "fan1_label").write_text(f"{fan_label}\n")
    if fan_rpm is not None:
        (device / "fan1_input").write_text(f"{fan_rpm}\n")
    if fan_min is not None:
        (device / "fan1_min").write_text(f"{fan_min}\n")
    if fan_max is not None:
        (device / "fan1_max").write_text(f"{fan_max}\n")
    if fan_manual is not None:
        (device / "fan1_manual").write_text(f"{fan_manual}\n")
    if fan_output is not None:
        (device / "fan1_output").write_text(f"{fan_output}\n")

    n = 1
    for label, milli_c in temps_milli_c.items():
        (device / f"temp{n}_label").write_text(f"{label}\n")
        (device / f"temp{n}_input").write_text(f"{milli_c}\n")
        n += 1

    return device


def add_bare_hwmon_no_name(sysfs_root: Path, index: int) -> Path:
    """A hwmon class entry with no `name` file anywhere reachable — must be
    silently skipped by discovery, never raise."""
    base = hwmon_class_dir(sysfs_root) / f"hwmon{index}"
    base.mkdir(parents=True, exist_ok=True)
    (base / "device").mkdir(parents=True, exist_ok=True)
    return base


def add_power_supply(sysfs_root: Path, name: str, fields: dict[str, str]) -> Path:
    base = sysfs_root / "class" / "power_supply" / name
    base.mkdir(parents=True, exist_ok=True)
    for key, value in fields.items():
        (base / key).write_text(f"{value}\n")
    return base


def add_cpu_freq(sysfs_root: Path, cpu_index: int, khz: int) -> Path:
    base = sysfs_root / "devices" / "system" / "cpu" / f"cpu{cpu_index}" / "cpufreq"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "scaling_cur_freq"
    path.write_text(f"{khz}\n")
    return path


def write_loadavg(procfs_root: Path, one: float, five: float, fifteen: float) -> None:
    procfs_root.mkdir(parents=True, exist_ok=True)
    (procfs_root / "loadavg").write_text(f"{one} {five} {fifteen} 1/775 273299\n")


def write_stat(procfs_root: Path, cpu_lines: dict[str, list[int]]) -> None:
    """`cpu_lines`: {"cpu": [user,nice,system,idle,iowait,irq,softirq,steal], "cpu0": [...], ...}"""
    procfs_root.mkdir(parents=True, exist_ok=True)
    lines = [f"{name} " + " ".join(str(v) for v in values) for name, values in cpu_lines.items()]
    lines.append("intr 0")
    lines.append("ctxt 0")
    (procfs_root / "stat").write_text("\n".join(lines) + "\n")


def write_meminfo(procfs_root: Path, kv_kb: dict[str, int]) -> None:
    procfs_root.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}:{' ' * 8}{value} kB" for key, value in kv_kb.items()]
    (procfs_root / "meminfo").write_text("\n".join(lines) + "\n")


def write_diskstats(procfs_root: Path, device: str, sectors_read: int, sectors_written: int) -> None:
    procfs_root.mkdir(parents=True, exist_ok=True)
    # major minor name reads reads_merged sectors_read ms_reading writes writes_merged sectors_written ms_writing ...
    line = f"   8       0 {device} 1 0 {sectors_read} 0 1 0 {sectors_written} 0 0 0 0 0 0 0"
    (procfs_root / "diskstats").write_text(line + "\n")


def write_netdev(procfs_root: Path, iface: str, rx_bytes: int, tx_bytes: int) -> None:
    procfs_root.mkdir(parents=True, exist_ok=True)
    header = "Inter-|   Receive                                                |  Transmit\n"
    header += " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
    rx_fields = [str(rx_bytes)] + ["0"] * 7
    tx_fields = [str(tx_bytes)] + ["0"] * 7
    line = f"{iface}: " + " ".join(rx_fields) + " " + " ".join(tx_fields) + "\n"
    (procfs_root / "net").mkdir(parents=True, exist_ok=True)
    (procfs_root / "net" / "dev").write_text(header + line)


def write_route(procfs_root: Path, default_iface: str | None) -> None:
    (procfs_root / "net").mkdir(parents=True, exist_ok=True)
    lines = ["Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"]
    if default_iface is not None:
        lines.append(f"{default_iface}\t00000000\t0104000A\t0003\t0\t0\t600\t00000000\t0\t0\t0")
    (procfs_root / "net" / "route").write_text("\n".join(lines) + "\n")


# --- A14: fan control mode (mbpfan pidfile + /proc/<pid>/comm) -------------------


def write_mbpfan_pid(run_root: Path, pid: int) -> None:
    """`/run/mbpfan.pid` -- what `sensors.read_fan_control` looks for to
    tell mbpfan-controlled from a bare manual override (A14)."""
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "mbpfan.pid").write_text(f"{pid}\n")


def write_proc_comm(procfs_root: Path, pid: int, comm: str) -> None:
    """`/proc/<pid>/comm` for a fake "live process" -- the second half of
    A14's mbpfan-vs-manual test (a stale pidfile naming a dead/reused pid
    must NOT read as `"mbpfan"`)."""
    pid_dir = procfs_root / str(pid)
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "comm").write_text(f"{comm}\n")


# --- A15: CPU thermal throttle counters + topology --------------------------------


def add_cpu_throttle(
    sysfs_root: Path,
    cpu_index: int,
    *,
    core_id: int | None = None,
    core_throttle_count: int | None = None,
    package_throttle_count: int | None = None,
) -> Path:
    """`/sys/devices/system/cpu/cpuN/{topology/core_id,
    thermal_throttle/{core,package}_throttle_count}` (A15/S3)."""
    cpu_dir = sysfs_root / "devices" / "system" / "cpu" / f"cpu{cpu_index}"
    if core_id is not None:
        topo = cpu_dir / "topology"
        topo.mkdir(parents=True, exist_ok=True)
        (topo / "core_id").write_text(f"{core_id}\n")
    if core_throttle_count is not None or package_throttle_count is not None:
        throttle = cpu_dir / "thermal_throttle"
        throttle.mkdir(parents=True, exist_ok=True)
        if core_throttle_count is not None:
            (throttle / "core_throttle_count").write_text(f"{core_throttle_count}\n")
        if package_throttle_count is not None:
            (throttle / "package_throttle_count").write_text(f"{package_throttle_count}\n")
    return cpu_dir
