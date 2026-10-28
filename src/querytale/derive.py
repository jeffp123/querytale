"""Derived tables, refreshed after every sync or import: domain_daily and minute_service.

minute_service counts lookups per minute, lane and service, with heartbeat and blocked lookups
kept in their own columns. A "lookup" folds repeats of the same hostname on the same device
less than FOLD apart: the router path logs A and HTTPS a few milliseconds apart instead of at
the identical timestamp, and counting both would inflate computer-lane rates.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from querytale.config import Config
from querytale.services import Service, fingerprint, load_services
from querytale.store import Store

FOLD = timedelta(seconds=1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_service (
    profile_id VARCHAR NOT NULL,
    minute TIMESTAMP NOT NULL,     -- UTC
    lane VARCHAR NOT NULL,
    service VARCHAR NOT NULL,
    n_allowed INTEGER,             -- activity: non-heartbeat, not blocked
    n_blocked INTEGER,             -- non-heartbeat, blocked (never activity)
    hb_allowed INTEGER,            -- heartbeats, not blocked
    hb_blocked INTEGER             -- heartbeats, blocked
)
"""

MATCH_COND = """
    (p.kind = 'domain' AND (l.domain = p.value OR ends_with(l.domain, '.' || p.value)))
    OR (p.kind = 'host' AND l.domain = p.value)
    OR (p.kind = 'regex' AND regexp_matches(l.domain, p.value))
"""

# Lookups (folded events) from $lo, with the events just before $lo as lag context.
LOOKUPS_SQL = f"""
SELECT * FROM (
    SELECT profile_id, ts, domain, device_id, lane, status,
           ts - lag(ts) OVER (PARTITION BY device_id, domain ORDER BY ts) AS gap
    FROM events
    WHERE profile_id = $pid AND ts >= $lo - INTERVAL {int(FOLD.total_seconds()) + 1} SECOND
)
WHERE ts >= $lo AND (gap IS NULL OR gap >= INTERVAL {int(FOLD.total_seconds())} SECOND)
"""


def _load_patterns(store: Store, services: dict[str, Service]) -> None:
    store.con.execute(
        "CREATE OR REPLACE TEMP TABLE svc_patterns "
        "(service VARCHAR, heartbeat BOOLEAN, kind VARCHAR, value VARCHAR)"
    )
    rows = [
        (s.id, hb, p.kind, p.value)
        for s in services.values()
        for hb, pats in ((False, s.patterns), (True, s.heartbeat))
        for p in pats
    ]
    if rows:
        store.con.executemany("INSERT INTO svc_patterns VALUES (?, ?, ?, ?)", rows)


def rebuild_minute_service(
    store: Store, profile_id: str, services: dict[str, Service], since: datetime | None
) -> None:
    store.con.execute(SCHEMA)
    lo = (since or datetime(1970, 1, 1)).replace(second=0, microsecond=0)
    _load_patterns(store, services)
    store.con.execute(
        "DELETE FROM minute_service WHERE profile_id = ? AND minute >= ?", [profile_id, lo]
    )
    store.con.execute(
        f"""
        INSERT INTO minute_service
        SELECT profile_id, date_trunc('minute', ts), lane, service,
               count(*) FILTER (WHERE NOT hb AND status <> 'blocked'),
               count(*) FILTER (WHERE NOT hb AND status = 'blocked'),
               count(*) FILTER (WHERE hb AND status <> 'blocked'),
               count(*) FILTER (WHERE hb AND status = 'blocked')
        FROM (
            SELECT l.profile_id, l.ts, l.domain, l.device_id, l.lane, l.status, p.service,
                   bool_or(p.heartbeat) AS hb
            FROM ({LOOKUPS_SQL}) l JOIN svc_patterns p ON {MATCH_COND}
            GROUP BY ALL
        )
        GROUP BY ALL
        """,
        {"pid": profile_id, "lo": lo},
    )
    store.con.execute(
        "INSERT OR REPLACE INTO meta VALUES (?, ?)",
        [f"minute_service:{profile_id}", fingerprint(services)],
    )


def ensure_minute_service(store: Store, profile_id: str, services: dict[str, Service]) -> bool:
    """Rebuild everything when service definitions changed since the last build."""
    store.con.execute(SCHEMA)
    row = store.con.execute(
        "SELECT value FROM meta WHERE key = ?", [f"minute_service:{profile_id}"]
    ).fetchone()
    if row and row[0] == fingerprint(services):
        return False
    rebuild_minute_service(store, profile_id, services, None)
    return True


def refresh(store: Store, cfg: Config, profile_id: str, since: datetime | None) -> None:
    """Bring every derived table up to date after new events from `since` on."""
    store.rebuild_domain_daily(profile_id, since, cfg.tz())
    services = load_services()
    if not ensure_minute_service(store, profile_id, services):
        rebuild_minute_service(store, profile_id, services, since)
