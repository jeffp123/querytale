"""Detector A: sustained use of known services, from minute_service.

Only allowed, non-heartbeat lookups can make a session. Heartbeats (allowed or blocked) become
"app running" intervals instead, with cadence measured separately for each because a blocked
client backs off: Steam checks in every minute when allowed, roughly every 5 when blocked.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from querytale.services import CONFIDENCES, Service
from querytale.store import Store

MINUTE = timedelta(minutes=1)
SMOOTHING = 3  # minutes in the rolling sum compared against min_rate
HIGH_DURATION = timedelta(minutes=15)


@dataclass
class Session:
    service: Service
    lane: str
    start: datetime  # UTC, naive
    end: datetime  # exclusive
    lookups: int
    peak: int  # highest rolling 3-minute sum
    mean: float  # mean rolling sum over active minutes
    confidence: str
    devices: list[str] = field(default_factory=list)
    top_hosts: list[tuple[str, int]] = field(default_factory=list)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def category(self) -> str:
        return self.service.category


@dataclass
class Running:
    service: Service
    lane: str
    start: datetime
    end: datetime
    blocked: bool
    lookups: int


@dataclass
class Overlap:
    a: Session
    b: Session
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass
class Gap:
    lane: str
    start: datetime
    end: datetime


def _cap(confidence: str, cap: str) -> str:
    return min(confidence, cap, key=CONFIDENCES.index)


def sessions_from_counts(counts: dict[datetime, int], service: Service, lane: str) -> list[Session]:
    """Gaps-and-islands over minutes whose rolling 3-minute sum reaches min_rate."""
    if not counts:
        return []
    rolling: dict[datetime, int] = {}
    for m in counts:
        for k in range(SMOOTHING):
            t = m + k * MINUTE
            if t not in rolling:
                rolling[t] = sum(counts.get(t - j * MINUTE, 0) for j in range(SMOOTHING))
    active = sorted(t for t, v in rolling.items() if v >= service.min_rate)

    islands: list[list[datetime]] = []
    for t in active:
        if islands and t - islands[-1][-1] <= service.max_gap:
            islands[-1].append(t)
        else:
            islands.append([t])

    out = []
    for island in islands:
        # Trim smoothing tails: the session runs from its first to its last real lookup.
        span = [m for m in counts if island[0] - (SMOOTHING - 1) * MINUTE <= m <= island[-1]]
        start, end = min(span), max(span) + MINUTE
        mean = sum(rolling[t] for t in island) / len(island)
        if end - start < service.min_duration:
            confidence = "low"
        elif end - start >= HIGH_DURATION and mean >= 2 * service.min_rate:
            confidence = "high"
        else:
            confidence = "medium"
        if lane == "phone":
            confidence = _cap(confidence, service.phone_max_confidence)
        out.append(
            Session(
                service=service,
                lane=lane,
                start=start,
                end=end,
                lookups=sum(counts[m] for m in span),
                peak=max(rolling[t] for t in island),
                mean=round(mean, 1),
                confidence=confidence,
            )
        )
    return out


def running_from_minutes(
    minutes: dict[datetime, int], service: Service, lane: str, blocked: bool
) -> list[Running]:
    """Join heartbeat minutes into intervals, tolerating gaps of a few typical cadences."""
    if not minutes:
        return []
    ordered = sorted(minutes)
    gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False)]
    cadence = median(gaps) if len(gaps) >= 3 else 5 * MINUTE
    tolerance = min(max(cadence * 2.5, 3 * MINUTE), 45 * MINUTE)
    out: list[Running] = []
    for m in ordered:
        if out and m - (out[-1].end - MINUTE) <= tolerance:
            out[-1].end = m + MINUTE
            out[-1].lookups += minutes[m]
        else:
            out.append(Running(service, lane, m, m + MINUTE, blocked, minutes[m]))
    return out


def find_overlaps(sessions: list[Session]) -> list[Overlap]:
    shown = sorted((s for s in sessions if s.confidence != "low"), key=lambda s: s.start)
    out = []
    for i, a in enumerate(shown):
        for b in shown[i + 1 :]:
            if b.start >= a.end:
                break
            if a.category != b.category:
                out.append(Overlap(a, b, max(a.start, b.start), min(a.end, b.end)))
    return out


@dataclass
class Analysis:
    start: datetime
    end: datetime
    sessions: list[Session]
    running: list[Running]
    overlaps: list[Overlap]
    gaps: list[Gap]

    def visible_sessions(self, include_low: bool = False) -> list[Session]:
        return [s for s in self.sessions if include_low or s.confidence != "low"]

    def active_time(self) -> dict[str, timedelta]:
        """Minutes covered by at least one medium/high session, per category."""
        minutes: dict[str, set[datetime]] = defaultdict(set)
        for s in self.visible_sessions():
            t = max(s.start, self.start)
            while t < min(s.end, self.end):
                minutes[s.category].add(t)
                t += MINUTE
        return {c: len(ms) * MINUTE for c, ms in sorted(minutes.items())}


def analyze(
    store: Store,
    profile_id: str,
    services: dict[str, Service],
    start: datetime,
    end: datetime,
    *,
    gap_threshold: timedelta = timedelta(hours=1),
    details: bool = True,
) -> Analysis:
    # Read a margin before the window so sessions already running at its start are whole.
    margin = max((s.max_gap for s in services.values()), default=MINUTE) + SMOOTHING * MINUTE
    rows = store.con.execute(
        """
        SELECT minute, lane, service, n_allowed, hb_allowed, hb_blocked
        FROM minute_service
        WHERE profile_id = ? AND minute >= ? AND minute < ?
        """,
        [profile_id, start - margin, end],
    ).fetchall()
    activity: dict[tuple[str, str], dict[datetime, int]] = defaultdict(dict)
    hb: dict[tuple[str, str, bool], dict[datetime, int]] = defaultdict(dict)
    for minute, lane, sid, n, hb_ok, hb_blocked in rows:
        if sid not in services:
            continue
        if n:
            activity[sid, lane][minute] = n
        if hb_ok:
            hb[sid, lane, False][minute] = hb_ok
        if hb_blocked:
            hb[sid, lane, True][minute] = hb_blocked

    sessions = [
        s
        for (sid, lane), counts in activity.items()
        for s in sessions_from_counts(counts, services[sid], lane)
        if s.end > start
    ]
    sessions.sort(key=lambda s: (s.start, s.service.id))
    running = [
        r
        for (sid, lane, blocked), minutes in hb.items()
        for r in running_from_minutes(minutes, services[sid], lane, blocked)
        if r.end > start
    ]
    running.sort(key=lambda r: (r.service.id, r.lane, r.start))
    if details:
        for s in sessions:
            _add_details(store, profile_id, s)
    return Analysis(
        start, end, sessions, running, find_overlaps(sessions),
        silent_gaps(store, profile_id, start, end, gap_threshold),
    )  # fmt: skip


def _add_details(store: Store, profile_id: str, s: Session) -> None:
    rows = store.con.execute(
        """
        SELECT domain, coalesce(device_name, device_id), count(*)
        FROM events
        WHERE profile_id = ? AND lane = ? AND ts >= ? AND ts < ? AND status <> 'blocked'
        GROUP BY ALL
        """,
        [profile_id, s.lane, s.start, s.end],
    ).fetchall()
    hosts: dict[str, int] = defaultdict(int)
    devices: dict[str, int] = defaultdict(int)
    for domain, device, n in rows:
        if s.service.matches(domain) and not s.service.is_heartbeat(domain):
            hosts[domain] += n
            devices[device or "?"] += n
    s.top_hosts = sorted(hosts.items(), key=lambda kv: -kv[1])[:5]
    s.devices = [d for d, _ in sorted(devices.items(), key=lambda kv: -kv[1])]


def silent_gaps(
    store: Store, profile_id: str, start: datetime, end: datetime, threshold: timedelta
) -> list[Gap]:
    """Stretches with no lookups at all on a lane: device off, asleep, or not using NextDNS."""
    rows = store.con.execute(
        """
        WITH bounds AS (
            SELECT lane, ts FROM events WHERE profile_id = $pid AND ts >= $start AND ts < $end
            UNION ALL SELECT lane, $start FROM (SELECT DISTINCT lane FROM events
                                               WHERE profile_id = $pid)
            UNION ALL SELECT lane, $end FROM (SELECT DISTINCT lane FROM events
                                             WHERE profile_id = $pid)
        )
        SELECT lane, prev, ts FROM (
            SELECT lane, ts, lag(ts) OVER (PARTITION BY lane ORDER BY ts) AS prev FROM bounds
        )
        WHERE ts - prev >= $threshold
        ORDER BY prev
        """,
        {"pid": profile_id, "start": start, "end": end, "threshold": threshold},
    ).fetchall()
    return [Gap(lane, a, b) for lane, a, b in rows]
