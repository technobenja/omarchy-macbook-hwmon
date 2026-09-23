"""Shared subprocess helper for the v3 collector calls (``busctl``,
``journalctl``) that read live system state the way ``sensors.py`` reads
sysfs/procfs files: a failure of any kind yields ``None``, never an
exception, and every call carries an explicit timeout so a hung external
process can never block the 1 s collector loop.

Both call sites that use this (``inhibitors.py`` for A13/S1,
``events.py`` for A17/B1) are throttled by their *callers*, not by this
module -- see ``inhibitors.InhibitorCache`` (at most every 10 s) and
``daemon.py`` (journal queries only at daemon start / backfill / the
``hwmon events`` CLI command, never once per tick).
"""

from __future__ import annotations

import subprocess


def run(cmd: list[str], *, timeout: float) -> str | None:
    """Run `cmd`, returning its stdout text on a zero exit, or ``None`` on
    ANY failure: the binary is missing, it times out, it exits non-zero, or
    anything else goes wrong. Never raises.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout
