"""The local DuckDB store.

Both sources (API pages and CSV exports) land in the same temporary `staging` table, one row
per raw log line, and are folded into `events` by the same SQL. That guarantees identical
normalization: rows sharing (timestamp, domain, device) collapse into one lookup event that keeps
the set of query types seen, and the natural key drops overlaps between sources.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from querytale import paths
from querytale.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key VARCHAR PRIMARY KEY,
    value VARCHAR
);
CREATE TABLE IF NOT EXISTS profiles (
    id VARCHAR PRIMARY KEY,
    nextdns_name VARCHAR,
    label VARCHAR
);
CREATE TABLE IF NOT EXISTS events (
    profile_id VARCHAR NOT NULL,
    ts TIMESTAMP NOT NULL,          -- UTC
    domain VARCHAR NOT NULL,
    root VARCHAR,
    qtypes VARCHAR[],
    status VARCHAR,                -- default | allowed | blocked | relayed | error
    reasons VARCHAR[],
    tracker VARCHAR,               -- API only; NULL for CSV rows
    matched_name VARCHAR,
    device_id VARCHAR NOT NULL,    -- '' when the log line had no device
    device_name VARCHAR,
    device_model VARCHAR,
    local_ip VARCHAR,
    client VARCHAR,
    lane VARCHAR,                  -- phone | computer
    PRIMARY KEY (profile_id, ts, domain, device_id)
);
CREATE TABLE IF NOT EXISTS sync_state (
    profile_id VARCHAR PRIMARY KEY,
    high_water_ts TIMESTAMP,
    backfilled_from TIMESTAMP,
    backfill_target TIMESTAMP,     -- set while a deeper backfill is in progress
    backfill_progress TIMESTAMP,
    last_run_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS domain_daily (
    profile_id VARCHAR NOT NULL,
    day DATE NOT NULL,             -- local day
    root VARCHAR NOT NULL,
    n INTEGER,                     -- allowed (non-blocked) lookup events
    n_blocked INTEGER,
    first_seen_ts TIMESTAMP,
    last_seen_ts TIMESTAMP,
    PRIMARY KEY (profile_id, day, root)
);
"""

STAGING = """
CREATE OR REPLACE TEMP TABLE staging (
    ts TIMESTAMP,
    domain VARCHAR,
    qtype VARCHAR,
    status VARCHAR,
    reasons VARCHAR,               -- comma-separated reason ids
    root VARCHAR,
    tracker VARCHAR,
    matched_name VARCHAR,
    device_id VARCHAR,
    device_name VARCHAR,
    device_model VARCHAR,
    local_ip VARCHAR,
    client VARCHAR
)
"""

CSV_COLUMNS = (
    "timestamp", "domain", "query_type", "dnssec", "protocol", "client_ip", "status", "reasons",
    "destination_country", "root_domain", "device_id", "device_name", "device_model",
    "device_local_ip", "matched_name", "client_name",
)  # fmt: skip

PHONE_MODEL_RE = r"(?i)iphone|android|pixel|galaxy|phone"

LANE_SQL = f"""
CASE
    WHEN list_contains($phone, device_id) OR list_contains($phone, device_name) THEN 'phone'
    WHEN list_contains($computer, device_id) OR list_contains($computer, device_name)
        THEN 'computer'
    WHEN client = 'apple-profile'
        OR regexp_matches(coalesce(device_model, ''), '{PHONE_MODEL_RE}') THEN 'phone'
    ELSE 'computer'
END
"""

STATUS_PRIORITY = """
CASE status WHEN 'blocked' THEN 3 WHEN 'allowed' THEN 2 WHEN 'relayed' THEN 1 ELSE 0 END
"""

MERGE_SQL = f"""
INSERT INTO events BY NAME
SELECT *, {LANE_SQL} AS lane FROM (
    SELECT
        $profile_id AS profile_id,
        ts,
        domain,
        device_id,
        max(nullif(root, '')) AS root,
        list_sort(list_distinct(list(nullif(qtype, '')))) AS qtypes,
        arg_max(status, {STATUS_PRIORITY}) AS status,
        list_sort(list_distinct(list_filter(
            flatten(list(string_split(coalesce(reasons, ''), ','))), lambda r: r <> ''
        ))) AS reasons,
        max(nullif(tracker, '')) AS tracker,
        max(nullif(matched_name, '')) AS matched_name,
        max(nullif(device_name, '')) AS device_name,
        max(nullif(device_model, '')) AS device_model,
        max(nullif(local_ip, '')) AS local_ip,
        max(nullif(client, '')) AS client
    FROM staging
    WHERE ts IS NOT NULL AND domain IS NOT NULL AND domain <> ''
    GROUP BY ts, domain, device_id
)
"""

# A triplet split across two API pages arrives in two merges; fold the query types together.
ON_CONFLICT_UPDATE = """
ON CONFLICT DO UPDATE SET
    qtypes = list_sort(list_distinct(list_concat(qtypes, EXCLUDED.qtypes))),
    tracker = coalesce(EXCLUDED.tracker, tracker)
"""
ON_CONFLICT_NOTHING = "ON CONFLICT DO NOTHING"


@dataclass
class MergeResult:
    added: int
    first_ts: datetime | None
    last_ts: datetime | None


@dataclass
class SyncState:
    profile_id: str
    high_water_ts: datetime | None = None
    backfilled_from: datetime | None = None
    backfill_target: datetime | None = None
    backfill_progress: datetime | None = None
    last_run_at: datetime | None = None


def utc_naive(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


class Store:
    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self.con.execute(SCHEMA)

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        path = path or paths.db_path()
        paths.ensure_private_dir(path.parent)
        store = cls(duckdb.connect(str(path)))
        paths.make_private(path)
        return store

    @classmethod
    def memory(cls) -> Store:
        return cls(duckdb.connect(":memory:"))

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Profiles and lanes ----------------------------------------------------------------

    def sync_profiles(self, cfg: Config) -> None:
        for p in cfg.profiles.values():
            self.con.execute(
                "INSERT OR REPLACE INTO profiles VALUES (?, ?, ?)", [p.id, p.nextdns_name, p.label]
            )
        self.relane_if_changed(cfg)

    @staticmethod
    def lane_params(cfg: Config) -> dict:
        return {"phone": list(cfg.phone_devices), "computer": list(cfg.computer_devices)}

    def relane_if_changed(self, cfg: Config) -> bool:
        """Recompute every event's lane when the lane overrides in config change."""
        rules = json.dumps(self.lane_params(cfg), sort_keys=True)
        row = self.con.execute("SELECT value FROM meta WHERE key = 'lane_rules'").fetchone()
        if row and row[0] == rules:
            return False
        self.con.execute(f"UPDATE events SET lane = {LANE_SQL}", self.lane_params(cfg))
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('lane_rules', ?)", [rules])
        return True

    # Staging and merging ---------------------------------------------------------------

    def reset_staging(self) -> None:
        self.con.execute(STAGING)

    def stage_rows(self, rows: Sequence[tuple]) -> None:
        """Stage normalized rows, in the column order of the staging table."""
        if rows:
            self.con.executemany(
                "INSERT INTO staging VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )

    def stage_csv(self, path: Path) -> int:
        """Stage a NextDNS CSV export. Returns the number of raw rows read."""
        source = (
            f"read_csv('{str(path).replace(chr(39), chr(39) * 2)}', header = true, "
            "all_varchar = true, quote = '\"', escape = '\"')"
        )
        cols = [d[0] for d in self.con.execute(f"SELECT * FROM {source} LIMIT 0").description]
        missing = [c for c in ("timestamp", "domain", "root_domain", "device_id") if c not in cols]
        if missing:
            raise ValueError(f"{path} does not look like a NextDNS export (missing {missing})")

        def col(name: str) -> str:
            return f'"{name}"' if name in cols else "NULL"

        self.con.execute(f"""
            INSERT INTO staging
            SELECT
                CAST(rtrim("timestamp", 'Z') AS TIMESTAMP),
                domain,
                {col("query_type")},
                coalesce(nullif({col("status")}, ''), 'default'),
                {col("reasons")},
                root_domain,
                NULL,
                {col("matched_name")},
                coalesce(device_id, ''),
                {col("device_name")},
                {col("device_model")},
                {col("device_local_ip")},
                {col("client_name")}
            FROM {source}
        """)
        return self.con.execute("SELECT count(*) FROM staging").fetchone()[0]

    def merge_staging(self, profile_id: str, cfg: Config, *, update: bool = True) -> MergeResult:
        before = self.count_events(profile_id)
        first, last = self.con.execute("SELECT min(ts), max(ts) FROM staging").fetchone()
        params = {"profile_id": profile_id, **self.lane_params(cfg)}
        conflict = ON_CONFLICT_UPDATE if update else ON_CONFLICT_NOTHING
        self.con.execute(MERGE_SQL + conflict, params)
        return MergeResult(self.count_events(profile_id) - before, first, last)

    # Reads -----------------------------------------------------------------------------

    def count_events(self, profile_id: str) -> int:
        return self.con.execute(
            "SELECT count(*) FROM events WHERE profile_id = ?", [profile_id]
        ).fetchone()[0]

    def event_range(self, profile_id: str) -> tuple[datetime | None, datetime | None]:
        return self.con.execute(
            "SELECT min(ts), max(ts) FROM events WHERE profile_id = ?", [profile_id]
        ).fetchone()

    def query_events(
        self,
        profile_id: str,
        start: datetime,
        end: datetime,
        *,
        match: str | None = None,
        include_blocked: bool = False,
        lane: str | None = None,
        limit: int | None = None,
    ) -> list[tuple]:
        sql = """
            SELECT ts, lane, coalesce(device_name, device_id) AS device, domain, root,
                   array_to_string(qtypes, ' ') AS qtypes, status,
                   array_to_string(reasons, ' ') AS reasons, tracker
            FROM events
            WHERE profile_id = $pid AND ts >= $start AND ts < $end
        """
        params: dict = {"pid": profile_id, "start": utc_naive(start), "end": utc_naive(end)}
        if match:
            sql += " AND regexp_matches(domain, $match)"
            params["match"] = match
        if not include_blocked:
            sql += " AND status <> 'blocked'"
        if lane:
            sql += " AND lane = $lane"
            params["lane"] = lane
        sql += " ORDER BY ts, domain"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.con.execute(sql, params).fetchall()

    # Sync state ------------------------------------------------------------------------

    def get_sync_state(self, profile_id: str) -> SyncState:
        row = self.con.execute(
            "SELECT * FROM sync_state WHERE profile_id = ?", [profile_id]
        ).fetchone()
        return SyncState(*row) if row else SyncState(profile_id)

    def put_sync_state(self, state: SyncState) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO sync_state VALUES (?, ?, ?, ?, ?, ?)",
            [
                state.profile_id,
                state.high_water_ts,
                state.backfilled_from,
                state.backfill_target,
                state.backfill_progress,
                state.last_run_at,
            ],
        )

    # Derived tables --------------------------------------------------------------------

    def rebuild_domain_daily(self, profile_id: str, since: datetime | None, tz: str) -> None:
        """Recompute domain_daily for every local day touching `since` and later."""
        zone = ZoneInfo(tz)
        if since is None:
            first_day = date.min
            lower = datetime.min
        else:
            first_day = since.replace(tzinfo=UTC).astimezone(zone).date()
            local_midnight = datetime.combine(first_day, datetime.min.time(), zone)
            lower = utc_naive(local_midnight) - timedelta(hours=1)  # DST slack
        self.con.execute(
            "DELETE FROM domain_daily WHERE profile_id = ? AND day >= ?", [profile_id, first_day]
        )
        self.con.execute(
            """
            INSERT INTO domain_daily
            SELECT profile_id, day, root,
                   count(*) FILTER (WHERE status <> 'blocked'),
                   count(*) FILTER (WHERE status = 'blocked'),
                   min(ts), max(ts)
            FROM (
                SELECT profile_id, status, ts, coalesce(root, domain) AS root,
                       CAST(timezone($tz, timezone('UTC', ts)) AS DATE) AS day
                FROM events
                WHERE profile_id = $pid AND ts >= $lower
            )
            WHERE day >= $first_day
            GROUP BY profile_id, day, root
            """,
            {"tz": tz, "pid": profile_id, "lower": lower, "first_day": first_day},
        )
