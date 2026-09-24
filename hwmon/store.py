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
import sys
import time
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
#: A17: "kept 30 days" -- same tier as `minute`.
EVENTS_RETENTION_S = 30 * 24 * 3600


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
        # A16: fan.target_rpm as a `raw` column, for `hwmon fancurve`'s
        # avg-target-per-bin. Migrated onto a pre-v3 DB with ADD COLUMN
        # (old rows read back NULL, per A16) rather than recreating `raw`.
        self._migrate_add_column("raw", "fan_target_rpm", "REAL")
        # A17/B2: `UNIQUE(ts_start)` is the single dedupe key shared by the
        # daemon-start check, the one-time backfill, and `hwmon events`'s
        # own live re-scan.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                ts_start REAL NOT NULL,
                ts_end REAL,
                kind TEXT NOT NULL,
                last_pct INTEGER,
                last_status TEXT,
                detail TEXT,
                boot_id TEXT,
                UNIQUE(ts_start)
            )
            """
        )
        # Deliverables SPEC.md: a tiny generic key/value table for small
        # persisted counters that don't warrant their own table -- currently
        # just the journal-query failure streak behind `journal_unavailable`
        # (R-L3.1). Deliberately NOT reused for anything `events`-shaped
        # (per-boot markers use `events` + `has_event_kind_for_boot` below,
        # matching A17/B2's existing dedupe idiom instead of inventing a
        # second one).
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._conn.commit()

    def _migrate_add_column(self, table: str, column: str, sql_type: str) -> None:
        existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def insert_raw(self, snapshot: dict) -> None:
        """One INSERT + commit per tick (A5). `fan_target_rpm` (A16) comes
        straight from the snapshot's `fan.target_rpm`, defaulting to NULL on
        a pre-v3 snapshot that doesn't carry the key at all."""
        h = _extract_headline(snapshot)
        fan_target_rpm = snapshot.get("fan", {}).get("target_rpm")
        self._conn.execute(
            "INSERT OR REPLACE INTO raw "
            "(ts, cpu_package_c, fan_rpm, battery_power_w, battery_pct, battery_temp_c, "
            "cpu_usage_pct, fan_target_rpm, snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot["ts"],
                h["cpu_package_c"],
                h["fan_rpm"],
                h["battery_power_w"],
                h["battery_pct"],
                h["battery_temp_c"],
                h["cpu_usage_pct"],
                fan_target_rpm,
                json.dumps(snapshot),
            ),
        )
        self._conn.commit()

    def aggregate_and_prune(
        self,
        now: float,
        raw_retention_s: float = RAW_RETENTION_S,
        minute_retention_s: float = MINUTE_RETENTION_S,
        events_retention_s: float = EVENTS_RETENTION_S,
    ) -> None:
        """Recompute minute aggregates from current raw rows, then prune both
        tables against `now`. Aggregation re-derives every bucket still
        present in `raw` on each call (cheap: raw is capped at ~86,400 rows)
        so a still-filling minute keeps updating until it ages out, and
        nothing here depends on wall-clock time beyond the `now` argument.

        Also prunes `events` older than `events_retention_s` (A17: "kept 30
        days") -- events are written independently (`insert_event`), never
        derived from `raw`/`minute`, so pruning them here is just retention,
        not aggregation.
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
        self._conn.execute("DELETE FROM events WHERE ts_start < ?", (now - events_retention_s,))
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

    # --- A16: hwmon fancurve ------------------------------------------------------

    def fancurve_raw(self, since: float) -> list[tuple[float | None, float | None, float | None, str]]:
        """`(cpu_package_c, fan_rpm, fan_target_rpm, snapshot)` for every raw
        row at or after `since`, oldest first. A16 reads `raw` only (pairing
        temp with fan RPM needs the per-second rows, which `minute` doesn't
        keep) -- so this is bounded by the 24 h raw retention, same as the
        `--hours <= 24` CLI limit."""
        cur = self._conn.execute(
            "SELECT cpu_package_c, fan_rpm, fan_target_rpm, snapshot FROM raw "
            "WHERE ts >= ? ORDER BY ts ASC",
            (since,),
        )
        return cur.fetchall()

    # --- A17/B1/B2: power-loss events ----------------------------------------------

    def insert_event(self, record) -> bool:
        """`INSERT OR IGNORE`, keyed on `UNIQUE(ts_start)` -- B2's shared
        dedupe key across the daemon-start check, the backfill, and
        `hwmon events`'s own scan. Returns True iff a new row was actually
        inserted (False on a duplicate `ts_start`)."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO events "
            "(ts_start, ts_end, kind, last_pct, last_status, detail, boot_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.ts_start,
                record.ts_end,
                record.kind,
                record.last_pct,
                record.last_status,
                record.detail,
                record.boot_id,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def existing_event_ts_starts(self) -> set[float]:
        cur = self._conn.execute("SELECT ts_start FROM events")
        return {row[0] for row in cur.fetchall()}

    def has_event_kind_for_boot(self, kind: str, boot_id: str) -> bool:
        """Deliverables SPEC.md: the per-boot dedupe used by the critical-
        battery marker (R-L1.4) and the hibernate backstop (R-L1.5) --
        "has THIS kind already been recorded for THIS boot?" -- the same
        `events` table A17/B2 already uses, just queried by `(kind,
        boot_id)` instead of `ts_start`."""
        cur = self._conn.execute("SELECT 1 FROM events WHERE kind = ? AND boot_id = ? LIMIT 1", (kind, boot_id))
        return cur.fetchone() is not None

    # --- generic small persisted counters (meta) --------------------------------------

    def get_meta_int(self, key: str, default: int = 0) -> int:
        cur = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        if row is None:
            return default
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return default

    def set_meta_int(self, key: str, value: int) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()

    def list_events(
        self, days: float, now: float
    ) -> list[tuple[float, float | None, str, int | None, str | None, str | None, str | None]]:
        """`(ts_start, ts_end, kind, last_pct, last_status, detail, boot_id)`
        for every recorded event in the last `days` days, oldest first."""
        since = now - days * 86400
        cur = self._conn.execute(
            "SELECT ts_start, ts_end, kind, last_pct, last_status, detail, boot_id FROM events "
            "WHERE ts_start >= ? ORDER BY ts_start ASC",
            (since,),
        )
        return cur.fetchall()

    #: S2 (advisor): the startup events scan is worth a warning once it
    #: gets slow enough to notice -- 1 s is the threshold the advisor
    #: measured against (unoptimized: ~0.26 s @ 9k rows, extrapolating to
    #: ~2.5 s @ 86k, the full 24 h raw table).
    _SLOW_SCAN_WARN_S = 1.0

    def raw_points_for_events(self) -> list[tuple[float, float | None, str | None]]:
        """`(ts, battery_pct, battery_status)` for every retained raw row,
        oldest first -- needed for A17/B1's classification, which needs
        both `pct` and `status` and `raw`'s own `battery_pct` column
        doesn't carry `status`. Only called at daemon start, in the
        backfill, and by `hwmon events` -- never once per tick.

        **S2 (advisor, measured):** the original form ran `json.loads` on
        every retained snapshot in Python -- 0.26 s at ~9k rows,
        extrapolating to ~2.5 s at the full 24 h / ~86,400-row raw table.
        `json_extract` is SQLite's own (JSON1, compiled into stdlib
        `sqlite3`) reader, done once per row inside the query itself --
        measured 0.062 s at ~9k rows, no per-row Python JSON parsing at
        all. A slow scan (something json_extract itself doesn't expect,
        e.g. a very large or WAL-checkpoint-contended database) still logs
        a line rather than silently taking however long it takes.
        """
        start = time.perf_counter()
        cur = self._conn.execute(
            "SELECT ts, battery_pct, json_extract(snapshot, '$.battery.status') FROM raw ORDER BY ts ASC"
        )
        points = cur.fetchall()
        elapsed = time.perf_counter() - start
        if elapsed > self._SLOW_SCAN_WARN_S:
            print(
                f"hwmon: raw_points_for_events scan took {elapsed:.2f}s for {len(points)} rows "
                "(> 1s warning threshold)",
                file=sys.stderr,
            )
        return points

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
