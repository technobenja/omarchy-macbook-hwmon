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

Python standard library only — no third-party dependencies (notably no
``psutil``, which is not installed on this machine).
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
