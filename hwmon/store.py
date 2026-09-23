"""SQLite persistence for hwmon: raw samples (24 h) and minute aggregates (30 d).

Schema and retention per spec A5:

- `raw(ts REAL PRIMARY KEY, <six headline columns>, snapshot TEXT)` — one row
  per tick, kept 24 h.
- `minute(ts_min INTEGER PRIMARY KEY, <metric>_min/avg/max for the six
  headline metrics)` — kept 30 days. "History beyond 24 h exists only for
  those six metrics" (A5) — the full snapshot JSON is not aggregated.

`Store.aggregate_and_prune(now=...)` always takes an explicit clock instead
of calling `time.time()` itself, so tests can drive retention with an
injected clock on a throwaway database rather than sleeping for real time
(required by acceptance check 8).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# The six headline metrics named in A5, in the fixed order used for both the
# `raw` columns and the `minute` metric_min/avg/max columns.
HEADLINE_METRICS: tuple[str, ...] = (
    "cpu_package_c",
    "fan_rpm",
    "battery_power_w",
    "battery_pct",
    "battery_temp_c",
    "cpu_usage_pct",
)

RAW_RETENTION_S = 24 * 3600
MINUTE_RETENTION_S = 30 * 24 * 3600


def _extract_headline(snapshot: dict) -> dict[str, float | int | None]:
    return {
        "cpu_package_c": snapshot["cpu"]["package_c"],
        "fan_rpm": snapshot["fan"]["rpm"],
        "battery_power_w": snapshot["battery"]["power_w"],
        "battery_pct": snapshot["battery"]["pct"],
        "battery_temp_c": snapshot["battery"]["temp_c"],
        "cpu_usage_pct": snapshot["cpu"]["usage_pct"],
    }


class Store:
    """Owns one SQLite connection. Not thread-safe; the daemon uses it from
    a single loop, and the CLI opens a short-lived read connection of its
    own."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw (
                ts REAL PRIMARY KEY,
                cpu_package_c REAL,
                fan_rpm REAL,
                battery_power_w REAL,
                battery_pct REAL,
                battery_temp_c REAL,
                cpu_usage_pct REAL,
                snapshot TEXT NOT NULL
            )
            """
        )
        minute_cols = ", ".join(f"{m}_min REAL, {m}_avg REAL, {m}_max REAL" for m in HEADLINE_METRICS)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS minute (ts_min INTEGER PRIMARY KEY, {minute_cols})"
        )
        self._conn.commit()

    def insert_raw(self, snapshot: dict) -> None:
        """One INSERT + commit per tick (A5)."""
        h = _extract_headline(snapshot)
        self._conn.execute(
            "INSERT OR REPLACE INTO raw "
            "(ts, cpu_package_c, fan_rpm, battery_power_w, battery_pct, battery_temp_c, cpu_usage_pct, snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot["ts"],
                h["cpu_package_c"],
                h["fan_rpm"],
                h["battery_power_w"],
                h["battery_pct"],
                h["battery_temp_c"],
                h["cpu_usage_pct"],
                json.dumps(snapshot),
            ),
        )
        self._conn.commit()

    def aggregate_and_prune(
        self,
        now: float,
        raw_retention_s: float = RAW_RETENTION_S,
        minute_retention_s: float = MINUTE_RETENTION_S,
    ) -> None:
        """Recompute minute aggregates from current raw rows, then prune both
        tables against `now`. Aggregation re-derives every bucket still
        present in `raw` on each call (cheap: raw is capped at ~86,400 rows)
        so a still-filling minute keeps updating until it ages out, and
        nothing here depends on wall-clock time beyond the `now` argument.
        """
        select_cols = ", ".join(
            f"MIN({m}) AS {m}_min, AVG({m}) AS {m}_avg, MAX({m}) AS {m}_max" for m in HEADLINE_METRICS
        )
        insert_cols = ", ".join(f"{m}_min, {m}_avg, {m}_max" for m in HEADLINE_METRICS)
        update_cols = ", ".join(
            f"{m}_min=excluded.{m}_min, {m}_avg=excluded.{m}_avg, {m}_max=excluded.{m}_max"
            for m in HEADLINE_METRICS
        )
        self._conn.execute(
            f"""
            INSERT INTO minute (ts_min, {insert_cols})
            SELECT CAST(ts / 60 AS INTEGER) * 60 AS ts_min, {select_cols}
            FROM raw
            GROUP BY ts_min
            ON CONFLICT(ts_min) DO UPDATE SET {update_cols}
            """
        )
        self._conn.execute("DELETE FROM raw WHERE ts < ?", (now - raw_retention_s,))
        self._conn.execute("DELETE FROM minute WHERE ts_min < ?", (now - minute_retention_s,))
        self._conn.commit()

    def raw_row_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM raw")
        return cur.fetchone()[0]

    def minute_row_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM minute")
        return cur.fetchone()[0]

    def history(self, metric: str, minutes: int, now: float) -> list[tuple[int, float | None, float | None, float | None]]:
        """Return `[(ts_min, min, avg, max), ...]` for `metric` over the last
        `minutes` minutes, oldest first."""
        if metric not in HEADLINE_METRICS:
            raise ValueError(f"unknown metric {metric!r}; choose from {HEADLINE_METRICS}")
        since = now - minutes * 60
        cur = self._conn.execute(
            f"SELECT ts_min, {metric}_min, {metric}_avg, {metric}_max FROM minute "
            "WHERE ts_min >= ? ORDER BY ts_min ASC",
            (since,),
        )
        return cur.fetchall()

    def peaks(self, now: float) -> dict[str, dict[str, float | None]]:
        """Highest recorded value per headline metric: `last_24h` (from raw,
        which retains exactly that window) and `all_time` (the max of raw's
        current max and every retained minute aggregate's max, i.e. the
        highest value seen since the daemon started keeping history, bounded
        by the 30-day minute retention)."""
        result: dict[str, dict[str, float | None]] = {}
        for m in HEADLINE_METRICS:
            raw_max = self._conn.execute(f"SELECT MAX({m}) FROM raw").fetchone()[0]
            minute_max = self._conn.execute(f"SELECT MAX({m}_max) FROM minute").fetchone()[0]
            candidates = [v for v in (raw_max, minute_max) if v is not None]
            result[m] = {
                "last_24h": raw_max,
                "all_time": max(candidates) if candidates else None,
            }
        return result

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
