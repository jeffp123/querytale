"""Generic-host attribution: learn which site owns a CDN or hosting hostname.

DNS logs carry no referrer, so ownership is recovered two ways:

1. Name in hostname: the owner's name sits inside the host (nytimes.map.fastly.net,
   img-s-msn-com.akamaized.net, gateway.ls-apple.com.akadns.net).
2. Learned co-occurrence: named roots seen on the same device from 10 s before to 2 s after
   each sighting. A root qualifies with 10+ sightings, presence in at least half of them, and
   20x more often than chance (lift). Per-customer CDN names such as d1a2b3.cloudfront.net are
   stable, so their owner can be learned from history.

Everything else stays unattributed. Attributions are inferences and are shown as such.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from querytale.domains import generic_suffix, is_generic
from querytale.store import Store

MIN_SIGHTINGS = 10
MIN_SUPPORT = 0.5
MIN_LIFT = 20.0
BEFORE_S = 10
AFTER_S = 2
BUCKET_S = 10  # equi-join bucket; must be >= BEFORE_S + AFTER_S - BUCKET_S to cover the window

SCHEMA = """
CREATE TABLE IF NOT EXISTS host_attribution (
    profile_id VARCHAR NOT NULL,
    generic_host VARCHAR NOT NULL,
    root VARCHAR NOT NULL,
    method VARCHAR NOT NULL,       -- name | cooccurrence
    support DOUBLE,                -- share of sightings with the owner nearby (cooccurrence)
    lift DOUBLE,
    sightings INTEGER,
    updated_at TIMESTAMP,
    PRIMARY KEY (profile_id, generic_host)
)
"""

TLDS = {
    "com", "net", "org", "io", "tv", "co", "gg", "us", "uk", "edu", "gov", "me", "app", "dev",
    "ai", "info", "biz", "ca", "de", "fr", "jp", "ly", "fm", "to", "cc", "xyz", "games",
}  # fmt: skip

# Hostname words too generic to name an owner.
STOP = {
    "www", "cdn", "edge", "edges", "global", "prod", "production", "origin", "static", "assets",
    "images", "image", "img", "media", "video", "videos", "content", "download", "downloads",
    "dualstack", "ssl", "secure", "api", "apis", "apps", "app", "map", "gslb", "akamai",
    "akamaized", "cloudfront", "fastly", "azure", "amazon", "amazonaws", "cloud", "cloudflare",
    "service", "services", "public", "web", "site", "live", "stream", "streaming", "files",
    "file", "data", "store", "mobile", "client", "clients", "auth", "login", "update", "updates",
    "config", "telemetry", "metrics", "logs", "events", "track", "tracking", "east", "west",
    "north", "south", "central", "europe", "asia", "test", "stage", "staging", "beta", "main",
    "default", "primary", "backup", "dns", "net", "com", "org", "sni", "tls", "http", "https",
    "ipv4", "ipv6", "user", "users", "accounts", "account", "shared", "share", "gateway",
}  # fmt: skip


@dataclass
class Attribution:
    host: str
    root: str
    method: str
    support: float | None = None
    lift: float | None = None
    sightings: int = 0


@dataclass
class AttributionStats:
    lookups: int
    hosts: int
    by_method: dict[str, int]  # name | cooccurrence | unattributed | too_rare -> lookups

    def share(self, method: str) -> float:
        return self.by_method.get(method, 0) / self.lookups if self.lookups else 0.0


def _first_label(root: str) -> str:
    return root.split(".", 1)[0]


def name_owner(host: str, label_index: dict[str, str], known_roots: set[str]) -> str | None:
    """The owner named inside a generic hostname, if any, restricted to roots seen locally."""
    suffix = generic_suffix(host)
    if suffix is None:
        return None
    prefix = host[: -len(suffix) - 1] if host != suffix else ""
    tokens = [t for t in re.split(r"[.\-]", prefix) if t]
    # An embedded domain, written with a dot or a dash before its TLD: ls-apple.com, msn-com.
    for a, b in zip(tokens, tokens[1:], strict=False):
        if b in TLDS and a not in STOP:
            candidate = f"{a}.{b}"
            if candidate in known_roots:
                return candidate
    # A word naming a known root: nytimes.map.fastly.net -> nytimes.com.
    for t in tokens:
        if len(t) >= 4 and t.isalpha() and t not in STOP and t in label_index:
            return label_index[t]
    return None


def _named_roots(store: Store, profile_id: str) -> tuple[dict[str, str], set[str]]:
    rows = store.con.execute(
        """
        SELECT root, count(*) FROM events
        WHERE profile_id = ? AND root IS NOT NULL AND NOT is_generic(domain)
        GROUP BY root
        """,
        [profile_id],
    ).fetchall()
    known = {r for r, _ in rows if not is_generic(r)}
    by_label: dict[str, tuple[int, str]] = {}
    for root, n in rows:
        if root in known:
            label = _first_label(root)
            if label not in by_label or n > by_label[label][0]:
                by_label[label] = (n, root)
    return {label: root for label, (_, root) in by_label.items()}, known


def _sightings(store: Store, profile_id: str) -> None:
    """Temp table of folded lookups (blocked included: a blocked client still co-occurs)."""
    store.con.execute(
        """
        CREATE OR REPLACE TEMP TABLE attr_lookups AS
        SELECT device_id, ts, domain, coalesce(root, domain) AS root, is_generic(domain) AS generic
        FROM (
            SELECT device_id, ts, domain, root,
                   ts - lag(ts) OVER (PARTITION BY device_id, domain ORDER BY ts) AS gap
            FROM events WHERE profile_id = $pid
        )
        WHERE gap IS NULL OR gap >= INTERVAL 1 SECOND
        """,
        {"pid": profile_id},
    )


def _cooccurrence(store: Store, hosts: list[str]) -> list[Attribution]:
    if not hosts:
        return []
    store.con.execute("CREATE OR REPLACE TEMP TABLE attr_hosts (host VARCHAR PRIMARY KEY)")
    store.con.executemany("INSERT INTO attr_hosts VALUES (?)", [(h,) for h in hosts])
    rows = store.con.execute(
        f"""
        WITH sight AS (
            SELECT l.device_id, l.ts, l.domain AS host,
                   unnest(range(
                       CAST(floor((epoch(l.ts) - {BEFORE_S}) / {BUCKET_S}) AS BIGINT),
                       CAST(floor((epoch(l.ts) + {AFTER_S}) / {BUCKET_S}) AS BIGINT) + 1
                   )) AS bucket
            FROM attr_lookups l JOIN attr_hosts h ON l.domain = h.host
        ),
        named AS (
            SELECT device_id, ts, root,
                   CAST(floor(epoch(ts) / {BUCKET_S}) AS BIGINT) AS bucket
            FROM attr_lookups WHERE NOT generic
        ),
        pairs AS (
            SELECT DISTINCT s.host, s.device_id, s.ts, n.root
            FROM sight s JOIN named n ON n.device_id = s.device_id AND n.bucket = s.bucket
            WHERE n.ts >= s.ts - INTERVAL {BEFORE_S} SECOND
              AND n.ts <= s.ts + INTERVAL {AFTER_S} SECOND
        ),
        support AS (SELECT host, root, count(*) AS k FROM pairs GROUP BY ALL),
        nsight AS (
            SELECT l.domain AS host, count(*) AS n
            FROM attr_lookups l JOIN attr_hosts h ON l.domain = h.host GROUP BY 1
        ),
        slots AS (
            SELECT root, count(DISTINCT (device_id, CAST(floor(epoch(ts) / 12) AS BIGINT))) AS b
            FROM attr_lookups WHERE NOT generic GROUP BY 1
        ),
        total AS (
            SELECT count(DISTINCT (device_id, CAST(floor(epoch(ts) / 12) AS BIGINT))) AS t
            FROM attr_lookups
        )
        SELECT host, root, n, k / n AS support, (k / n) / (b / t) AS lift
        FROM support JOIN nsight USING (host) JOIN slots USING (root), total
        WHERE n >= {MIN_SIGHTINGS} AND k / n >= {MIN_SUPPORT} AND (k / n) / (b / t) >= {MIN_LIFT}
        QUALIFY row_number() OVER (PARTITION BY host ORDER BY k DESC, lift DESC) = 1
        """
    ).fetchall()
    return [
        Attribution(host, root, "cooccurrence", round(support, 3), round(lift, 1), n)
        for host, root, n, support, lift in rows
    ]


def learn(store: Store, profile_id: str, now: datetime | None = None) -> list[Attribution]:
    """Relearn every generic host's owner from the full local history."""
    store.con.execute(SCHEMA)
    _sightings(store, profile_id)
    counts = dict(
        store.con.execute(
            "SELECT domain, count(*) FROM attr_lookups WHERE generic GROUP BY 1"
        ).fetchall()
    )
    label_index, known = _named_roots(store, profile_id)
    found: dict[str, Attribution] = {}
    for host, n in counts.items():
        if owner := name_owner(host, label_index, known):
            found[host] = Attribution(host, owner, "name", sightings=n)
    learnable = [h for h, n in counts.items() if h not in found and n >= MIN_SIGHTINGS]
    for a in _cooccurrence(store, learnable):
        found[a.host] = a

    store.con.execute("DELETE FROM host_attribution WHERE profile_id = ?", [profile_id])
    if found:
        store.con.executemany(
            "INSERT INTO host_attribution VALUES (?, ?, ?, ?, ?, ?, ?, coalesce(?, now()))",
            [
                (profile_id, a.host, a.root, a.method, a.support, a.lift, a.sightings, now)
                for a in found.values()
            ],
        )
    store.con.execute("DROP TABLE IF EXISTS attr_lookups")
    return list(found.values())


def owners(store: Store, profile_id: str) -> dict[str, str]:
    store.con.execute(SCHEMA)
    return dict(
        store.con.execute(
            "SELECT generic_host, root FROM host_attribution WHERE profile_id = ?", [profile_id]
        ).fetchall()
    )


def stats(store: Store, profile_id: str) -> AttributionStats:
    """Share of generic-host lookup events by how (or whether) their owner is known."""
    store.con.execute(SCHEMA)
    rows = store.con.execute(
        """
        WITH g AS (
            SELECT domain, count(*) AS n FROM events
            WHERE profile_id = $pid AND is_generic(domain) GROUP BY 1
        ),
        sightings AS (
            SELECT domain, count(*) AS s FROM (
                SELECT domain, ts - lag(ts) OVER (PARTITION BY device_id, domain ORDER BY ts) gap
                FROM events WHERE profile_id = $pid AND is_generic(domain)
            ) WHERE gap IS NULL OR gap >= INTERVAL 1 SECOND GROUP BY 1
        )
        SELECT coalesce(a.method,
                        CASE WHEN s.s < $min THEN 'too_rare' ELSE 'unattributed' END),
               count(*), sum(g.n)
        FROM g JOIN sightings s USING (domain)
        LEFT JOIN host_attribution a ON a.profile_id = $pid AND a.generic_host = g.domain
        GROUP BY 1
        """,
        {"pid": profile_id, "min": MIN_SIGHTINGS},
    ).fetchall()
    by_method = Counter({m: int(n) for m, _, n in rows})
    return AttributionStats(
        lookups=sum(by_method.values()), hosts=sum(h for _, h, _ in rows), by_method=by_method
    )
