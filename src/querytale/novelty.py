"""Detector B: rare sites that show up in clusters.

A site (root domain, or the learned owner of a generic host, or the generic host itself) is
rare on a day when it was seen on at most 3 of the prior 30 days. A rare site bursts when it
has 8+ allowed lookups inside a 30-minute window. Bursts on the same lane less than 15 minutes
apart merge into one episode, which is how half an hour of hopping between small browser-game
sites surfaces even when each site alone is small.

Ad and analytics SDK bursts are demoted, not dropped: a new SDK usually means a new app was
opened, so they collect in a separate "new app activity" list.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from querytale import domains
from querytale.services import Service
from querytale.store import Store

RARE_MAX_DAYS = 3
BASELINE_DAYS = 30
BURST_MIN = 8
BURST_WINDOW = timedelta(minutes=30)
EPISODE_GAP = timedelta(minutes=15)


@dataclass
class Burst:
    key: str
    lane: str
    start: datetime  # UTC, naive: first lookup
    end: datetime  # last lookup
    lookups: int
    hosts: list[tuple[str, int]]
    days_seen: int  # days seen in the 30 before
    category: str = "unknown"
    category_source: str = ""
    tracker: bool = False
    inferred: bool = False  # counted through a learned generic-host owner


@dataclass
class Episode:
    lane: str
    bursts: list[Burst] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return min(b.start for b in self.bursts)

    @property
    def end(self) -> datetime:
        return max(b.end for b in self.bursts)

    @property
    def lookups(self) -> int:
        return sum(b.lookups for b in self.bursts)

    @property
    def main_category(self) -> str:
        weights: Counter[str] = Counter()
        for b in self.bursts:
            if b.category != "unknown":
                weights[b.category] += b.lookups
        return weights.most_common(1)[0][0] if weights else "unknown"

    def title(self) -> str:
        n = len(self.bursts)
        if n == 1:
            b = self.bursts[0]
            return f"1 rare site: {b.key} ({b.category})"
        main = self.main_category
        what = "uncategorized" if main == "unknown" else f"mostly {main}"
        return f"{n} rare sites, {what}"


@dataclass
class Unusual:
    episodes: list[Episode]
    app_activity: list[Burst]  # tracker bursts, collapsed in output
    warming_up: bool  # less than 30 days of history before the window
    history_from: datetime | None


CANDIDATES_SQL = f"""
WITH lk AS (
    SELECT f.*, a.root AS owner FROM (
        SELECT ts, domain, root, device_id, lane, status, tracker,
               ts - lag(ts) OVER (PARTITION BY device_id, domain ORDER BY ts) AS gap
        FROM events
        WHERE profile_id = $pid AND ts >= $lo - INTERVAL 2 SECOND AND ts < $hi
    ) f
    LEFT JOIN host_attribution a ON a.profile_id = $pid AND a.generic_host = f.domain
    WHERE ts >= $lo AND (gap IS NULL OR gap >= INTERVAL 1 SECOND) AND status <> 'blocked'
),
keyed AS (
    SELECT *, coalesce(owner, site_key(domain, root)) AS key,
           CAST(timezone($tz, timezone('UTC', ts)) AS DATE) AS day
    FROM lk
),
seen AS (
    SELECT c.key, c.day, count(d.day) AS days_seen
    FROM (SELECT DISTINCT key, day FROM keyed) c
    LEFT JOIN domain_daily d
        ON d.profile_id = $pid AND d.root = c.key AND d.n > 0
       AND d.day >= c.day - {BASELINE_DAYS} AND d.day < c.day
    GROUP BY ALL
)
SELECT k.ts, k.domain, k.key, k.lane, k.tracker, k.owner IS NOT NULL AS inferred, s.days_seen
FROM keyed k JOIN seen s USING (key, day)
WHERE s.days_seen <= $rare
ORDER BY k.lane, k.key, k.ts
"""


def bursts_from_times(times: list[datetime]) -> list[tuple[int, int]]:
    """Index ranges [i, j] of lookups belonging to some 30-minute window with 8+ lookups."""
    marked = [False] * len(times)
    j = 0
    for i in range(len(times)):
        j = max(j, i)
        while j + 1 < len(times) and times[j + 1] - times[i] <= BURST_WINDOW:
            j += 1
        if j - i + 1 >= BURST_MIN:
            for k in range(i, j + 1):
                marked[k] = True
    runs: list[tuple[int, int]] = []
    for i, m in enumerate(marked):
        if not m:
            continue
        if runs and runs[-1][1] == i - 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs


def _reason_categories(store: Store, profile_id: str, keys: set[str]) -> dict[str, str]:
    if not keys:
        return {}
    rows = store.con.execute(
        """
        SELECT DISTINCT site_key(domain, root) AS key, reasons FROM events
        WHERE profile_id = ? AND len(reasons) > 0 AND list_contains(?, site_key(domain, root))
        """,
        [profile_id, sorted(keys)],
    ).fetchall()
    out: dict[str, str] = {}
    for key, reasons in rows:
        if key not in out and (category := domains.reason_category(reasons)):
            out[key] = category
    return out


def find_unusual(
    store: Store,
    profile_id: str,
    services: dict[str, Service],
    start: datetime,
    end: datetime,
    tz: str,
    labels: dict[str, str] | None = None,
) -> Unusual:
    labels = domains.load_labels() if labels is None else labels
    rows = store.con.execute(
        CANDIDATES_SQL,
        {"pid": profile_id, "lo": start - BURST_WINDOW, "hi": end, "tz": tz,
         "rare": RARE_MAX_DAYS},
    ).fetchall()  # fmt: skip

    def owned_by_service(host: str, key: str) -> bool:
        return any(s.matches(host) or s.owns(key) for s in services.values())

    groups: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for ts, host, key, lane, tracker, inferred, days_seen in rows:
        if not owned_by_service(host, key):
            groups[lane, key].append((ts, host, tracker, inferred, days_seen))

    bursts: list[Burst] = []
    for (lane, key), lookups in groups.items():
        for i, j in bursts_from_times([lk[0] for lk in lookups]):
            part = lookups[i : j + 1]
            b = Burst(
                key=key,
                lane=lane,
                start=part[0][0],
                end=part[-1][0],
                lookups=len(part),
                hosts=Counter(lk[1] for lk in part).most_common(),
                days_seen=min(lk[4] for lk in part),
                tracker=any(lk[2] for lk in part),
                inferred=any(lk[3] for lk in part),
            )
            if b.end >= start and b.start < end:
                bursts.append(b)

    reasons = _reason_categories(store, profile_id, {b.key for b in bursts})
    kept = []
    for b in bursts:
        b.category, b.category_source = domains.classify(b.key, labels, reasons)
        if b.category in (domains.IGNORE, domains.SYSTEM):
            continue
        b.tracker = (
            b.tracker
            or b.category == "ads"
            or domains.is_tracker(b.key)
            or any(domains.is_tracker(h) for h, _ in b.hosts)
        )
        kept.append(b)

    episodes: list[Episode] = []
    for b in sorted((b for b in kept if not b.tracker), key=lambda b: (b.lane, b.start)):
        last = episodes[-1] if episodes else None
        if last and last.lane == b.lane and b.start - last.end <= EPISODE_GAP:
            last.bursts.append(b)
        else:
            episodes.append(Episode(b.lane, [b]))
    episodes.sort(key=lambda e: e.start)

    (history_from,) = store.con.execute(
        "SELECT min(ts) FROM events WHERE profile_id = ?", [profile_id]
    ).fetchone()
    warming_up = history_from is None or history_from > start - timedelta(days=BASELINE_DAYS)
    app_activity = sorted((b for b in kept if b.tracker), key=lambda b: b.start)
    return Unusual(episodes, app_activity, warming_up, history_from)
