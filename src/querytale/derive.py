"""Derived tables, refreshed after every sync or import: domain_daily, host_attribution and
minute_service.

minute_service counts lookups per minute, lane and service, with heartbeat and blocked lookups
kept in their own columns. A "lookup" folds repeats of the same hostname on the same device
less than FOLD apart: the router path logs A and HTTPS a few milliseconds apart instead of at
the identical timestamp, and counting both would inflate computer-lane rates.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from querytale import attribution
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

# A generic host attributed to an owner counts toward the owner's service (never as heartbeat).
MATCH_COND = """
    (p.kind = 'domain' AND (l.domain = p.value OR ends_with(l.domain, '.' || p.value)))
    OR (p.kind = 'host' AND l.domain = p.value)
    OR (p.kind = 'regex' AND regexp_matches(l.domain, p.value))
    OR (l.owner IS NOT NULL AND NOT p.heartbeat AND p.kind = 'domain'
        AND (l.owner = p.value OR ends_with(l.owner, '.' || p.value)))
"""

DOMAIN_DAILY_VERSION = "site-key-1"

# Lookups (folded events) from $lo, with the events just before $lo as lag context.
LOOKUPS_SQL = f"""
SELECT f.*, a.root AS owner FROM (
    SELECT profile_id, ts, domain, device_id, lane, status,
           ts - lag(ts) OVER (PARTITION BY device_id, domain ORDER BY ts) AS gap
    FROM events
    WHERE profile_id = $pid AND ts >= $lo - INTERVAL {int(FOLD.total_seconds()) + 1} SECOND
) f
LEFT JOIN host_attribution a ON a.profile_id = f.profile_id AND a.generic_host = f.domain
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
    store.con.execute(attribution.SCHEMA)
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
        [f"minute_service:{profile_id}", _fingerprint(store, profile_id, services)],
    )


def _fingerprint(store: Store, profile_id: str, services: dict[str, Service]) -> str:
    """Services plus only the attributions that feed a service: most relearns change nothing."""
    relevant = sorted(
        f"{host}={root}"
        for host, root in attribution.owners(store, profile_id).items()
        if any(s.owns(root) for s in services.values())
    )
    digest = hashlib.sha256(",".join(relevant).encode()).hexdigest()[:16]
    return f"{fingerprint(services)}/{digest}"


def _meta(store: Store, key: str) -> str | None:
    row = store.con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def _set_meta(store: Store, key: str, value: str) -> None:
    store.con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])


def ensure_minute_service(store: Store, profile_id: str, services: dict[str, Service]) -> bool:
    """Rebuild everything when service definitions or attributions changed since the last build."""
    store.con.execute(SCHEMA)
    if _meta(store, f"minute_service:{profile_id}") == _fingerprint(store, profile_id, services):
        return False
    rebuild_minute_service(store, profile_id, services, None)
    return True


def ensure(store: Store, cfg: Config, profile_id: str, services: dict[str, Service]) -> None:
    """Make derived tables current before analysis (cheap when nothing changed)."""
    if _meta(store, f"domain_daily:{profile_id}") != DOMAIN_DAILY_VERSION:
        store.rebuild_domain_daily(profile_id, None, cfg.tz())
        _set_meta(store, f"domain_daily:{profile_id}", DOMAIN_DAILY_VERSION)
    if _meta(store, f"attribution:{profile_id}") is None:
        attribution.learn(store, profile_id)
        _set_meta(store, f"attribution:{profile_id}", "learned")
    ensure_minute_service(store, profile_id, services)


def refresh(store: Store, cfg: Config, profile_id: str, since: datetime | None) -> None:
    """Bring every derived table up to date after new events from `since` on."""
    if _meta(store, f"domain_daily:{profile_id}") != DOMAIN_DAILY_VERSION:
        since = None  # keying changed: rebuild every day, not just the new ones
    store.rebuild_domain_daily(profile_id, since, cfg.tz())
    _set_meta(store, f"domain_daily:{profile_id}", DOMAIN_DAILY_VERSION)
    attribution.learn(store, profile_id)  # relearned from the full history each time
    _set_meta(store, f"attribution:{profile_id}", "learned")
    services = load_services()
    if not ensure_minute_service(store, profile_id, services):
        rebuild_minute_service(store, profile_id, services, since)


def prune(store: Store, cutoff: datetime) -> None:
    """Delete everything older than `cutoff` (UTC) and relearn from what remains."""
    store.con.execute(SCHEMA)
    store.con.execute(attribution.SCHEMA)
    store.con.execute("DELETE FROM events WHERE ts < ?", [cutoff])
    store.con.execute("DELETE FROM minute_service WHERE minute < ?", [cutoff])
    store.con.execute("DELETE FROM domain_daily WHERE last_seen_ts < ?", [cutoff])
    for (profile_id,) in store.con.execute("SELECT DISTINCT profile_id FROM events").fetchall():
        attribution.learn(store, profile_id)
    store.con.execute("CHECKPOINT")
