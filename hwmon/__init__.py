"""hwmon — hardware telemetry collector and CLI for a MacBook Pro 11,1 under omarchy.

Package layout (see specs/spec.md for the full contract):

- ``sensors``  — stateless sysfs/procfs readers, parameterized by ``sysfs_root``
  and ``procfs_root`` so tests can point them at fixture trees.
- ``snapshot`` — assembles the readers into the ``latest.json`` contract
  (see A2 in the spec / ``tests/fixtures/latest.example.json``), validates
  its shape, and writes it atomically.
- ``store``    — SQLite persistence: raw samples (24 h) and minute
  aggregates (30 days).
- ``daemon``   — the 1 Hz collector loop (``hwmon daemon``).
- ``inhibitors``, ``events`` — v3: the sleep-block guard (A13) and
  power-loss event classification (A17).
- ``config``, ``critical_marker``, ``backstop``, ``triage``, ``recovery`` —
  the omarchy-power-loss-recovery deliverables spec's hwmon slice: the
  critical-battery journal marker (R-L1.4), the disabled-by-default
  hibernate backstop (R-L1.5), post-crash triage (R-L3.1), and the `/home`
  snapshot-age reading (R-L4.1, schema 3's ``recovery`` key).

Python standard library only — no third-party dependencies (notably no
``psutil``, which is not installed on this machine).
"""

from __future__ import annotations

__version__ = "1.3.0"
__all__ = ["__version__"]
