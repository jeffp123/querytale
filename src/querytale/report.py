"""The HTML report: one self-contained file per run, built from the same detector results.

Everything (ECharts, CSS, data) is inlined, and a Content-Security-Policy blocks every network
fetch, so an opened report cannot phone home. Times are embedded as local wall-clock
milliseconds and rendered in UTC mode, so the report shows the configured time zone whatever
the viewing browser's zone is.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

from querytale import __version__, attribution, domains, paths
from querytale.derive import LOOKUPS_SQL, MATCH_COND, _load_patterns
from querytale.novelty import Episode, Unusual
from querytale.services import Service
from querytale.sessions import Analysis, Session
from querytale.store import Store

# Fixed category order = fixed categorical color slots; anything else is "other" (gray).
CATEGORY_ORDER = ("games", "video", "school", "social", "ai")
CATEGORY_NAMES = {"games": "Games", "video": "Video", "school": "School", "social": "Social",
                  "ai": "AI", "other": "Other"}  # fmt: skip

MAX_EVENTS_PER_ITEM = 5_000  # raw rows embedded per session or episode
MAX_EMBED_EVENTS = 200_000  # above this many events in the window, embed aggregates only
HEATMAP_MIN_DAYS = 6.5
NEARBY = timedelta(seconds=30)

CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src 'none'; connect-src 'none'; form-action 'none'; "
    "base-uri 'none'"
)


@dataclass
class ReportContext:
    label: str
    window_label: str
    start: datetime  # UTC, naive
    end: datetime
    tz: str
    last_sync: datetime | None


def category_slot(category: str) -> str:
    return category if category in CATEGORY_ORDER else "other"


def bucket_minutes(span: timedelta) -> int:
    if span <= timedelta(hours=26):
        return 1
    if span <= timedelta(days=3, hours=2):
        return 5
    return 15


class _Clock:
    def __init__(self, tz: str) -> None:
        self.zone = ZoneInfo(tz)

    def ms(self, ts: datetime) -> int:
        """UTC naive -> local wall-clock epoch milliseconds (rendered with useUTC)."""
        local = ts.replace(tzinfo=UTC).astimezone(self.zone).replace(tzinfo=UTC)
        return int(local.timestamp() * 1000)

    def text(self, ts: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return ts.replace(tzinfo=UTC).astimezone(self.zone).strftime(fmt)


def _service_regex(service: Service) -> str:
    parts = []
    for p in service.patterns:
        if p.kind == "regex":
            parts.append(p.value)
        else:
            parts.append(re.escape(p.value) + ("$" if p.kind == "host" else ""))
    return "|".join(parts)


def _query_cmd(ctx: ReportContext, clock: _Clock, start, end, match: str, lane: str | None):
    lane_arg = f" --lane {lane}" if lane else ""
    return (
        f"querytale query {ctx.label} --from '{clock.text(start)}' "
        f"--to '{clock.text(end + timedelta(minutes=1))}' --match '{match}'{lane_arg} --no-sync"
    )


def _event_rows(store: Store, clock: _Clock, sql: str, params: list) -> list[list]:
    rows = store.con.execute(sql, params).fetchall()
    return [
        [clock.text(ts, "%a %H:%M:%S"), lane, device or "", domain, qtypes or "", status]
        for ts, lane, device, domain, qtypes, status in rows
    ]


EVENT_COLUMNS = ["time", "lane", "device", "domain", "types", "status"]


def _session_events(store, profile_id, clock, s: Session, owners) -> list[list] | None:
    rows = store.con.execute(
        """
        SELECT ts, lane, coalesce(device_name, device_id), domain,
               array_to_string(qtypes, ' '), status
        FROM events
        WHERE profile_id = ? AND lane = ? AND ts >= ? AND ts < ? AND status <> 'blocked'
        ORDER BY ts
        """,
        [profile_id, s.lane, s.start, s.end],
    ).fetchall()
    keep = [
        r for r in rows
        if not s.service.is_heartbeat(r[3])
        and (s.service.matches(r[3]) or (r[3] in owners and s.service.owns(owners[r[3]])))
    ]  # fmt: skip
    if len(keep) > MAX_EVENTS_PER_ITEM:
        return None
    return [
        [clock.text(ts, "%a %H:%M:%S"), lane, dev or "", dom, qt or "", st]
        for ts, lane, dev, dom, qt, st in keep
    ]


def _episode_events(store, profile_id, clock, e: Episode) -> list[list] | None:
    keys = sorted({b.key for b in e.bursts})
    (n,) = store.con.execute(
        """
        SELECT count(*) FROM events ev
        LEFT JOIN host_attribution a ON a.profile_id = ev.profile_id AND a.generic_host = ev.domain
        WHERE ev.profile_id = ? AND ev.lane = ? AND ev.ts >= ? AND ev.ts <= ?
          AND ev.status <> 'blocked'
          AND list_contains(?, coalesce(a.root, site_key(ev.domain, ev.root)))
        """,
        [profile_id, e.lane, e.start, e.end, keys],
    ).fetchone()
    if n > MAX_EVENTS_PER_ITEM:
        return None
    return _event_rows(
        store,
        clock,
        """
        SELECT ev.ts, ev.lane, coalesce(ev.device_name, ev.device_id), ev.domain,
               array_to_string(ev.qtypes, ' '), ev.status
        FROM events ev
        LEFT JOIN host_attribution a ON a.profile_id = ev.profile_id AND a.generic_host = ev.domain
        WHERE ev.profile_id = ? AND ev.lane = ? AND ev.ts >= ? AND ev.ts <= ?
          AND ev.status <> 'blocked'
          AND list_contains(?, coalesce(a.root, site_key(ev.domain, ev.root)))
        ORDER BY ev.ts
        """,
        [profile_id, e.lane, e.start, e.end, keys],
    )


def _nearby_named(store, profile_id, e: Episode) -> list[list]:
    """Named lookups within 30 s of unattributed generic hosts: context, not a claim."""
    hosts = sorted({h for b in e.bursts if domains.is_generic(b.key) for h, _ in b.hosts})
    if not hosts:
        return []
    rows = store.con.execute(
        """
        WITH s AS (
            SELECT device_id, ts FROM events
            WHERE profile_id = $pid AND lane = $lane AND ts >= $lo AND ts <= $hi
              AND list_contains($hosts, domain)
        )
        SELECT coalesce(n.root, n.domain) AS root, count(DISTINCT (s.device_id, s.ts)) AS k
        FROM s JOIN events n
          ON n.profile_id = $pid AND n.device_id = s.device_id
         AND n.ts BETWEEN s.ts - INTERVAL 30 SECOND AND s.ts + INTERVAL 30 SECOND
        WHERE NOT is_generic(n.domain)
        GROUP BY 1 ORDER BY k DESC LIMIT 15
        """,
        {"pid": profile_id, "lane": e.lane, "lo": e.start, "hi": e.end, "hosts": hosts},
    ).fetchall()
    return [[root, k] for root, k in rows]


def _frequency(store, profile_id, services, ctx: ReportContext, clock: _Clock, minutes: int):
    _load_patterns(store, services)
    store.con.execute("CREATE OR REPLACE TEMP TABLE svc_cat (service VARCHAR, cat VARCHAR, r INT)")
    store.con.executemany(
        "INSERT INTO svc_cat VALUES (?, ?, ?)",
        [
            (
                s.id,
                category_slot(s.category),
                CATEGORY_ORDER.index(s.category) if s.category in CATEGORY_ORDER else 99,
            )
            for s in services.values()
        ],  # fmt: skip
    )
    rows = store.con.execute(
        f"""
        WITH l AS (SELECT * FROM ({LOOKUPS_SQL}) WHERE ts < $hi AND status <> 'blocked'),
        tagged AS (
            SELECT l.ts, l.domain, l.device_id, l.lane,
                   arg_min(c.cat, c.r) FILTER (WHERE NOT p.heartbeat) AS cat,
                   bool_or(p.heartbeat) AS hb  -- a heartbeat match wins, as in minute_service
            FROM l
            LEFT JOIN svc_patterns p ON {MATCH_COND}
            LEFT JOIN svc_cat c ON c.service = p.service
            GROUP BY ALL
        )
        SELECT time_bucket(INTERVAL '{minutes} minutes', ts) AS b, lane,
               CASE WHEN coalesce(hb, false) THEN 'heartbeat' ELSE coalesce(cat, 'other') END,
               count(*)
        FROM tagged GROUP BY ALL ORDER BY b
        """,
        {"pid": profile_id, "lo": ctx.start, "hi": ctx.end},
    ).fetchall()
    first = ctx.start.replace(second=0, microsecond=0)
    first -= timedelta(minutes=first.minute % minutes) if minutes > 1 else timedelta(0)
    buckets = []
    t = first
    while t < ctx.end:
        buckets.append(t)
        t += timedelta(minutes=minutes)
    index = {b: i for i, b in enumerate(buckets)}
    series_names = [*CATEGORY_ORDER, "other", "heartbeat"]
    series: dict[str, dict[str, list[int]]] = {
        lane: {name: [0] * len(buckets) for name in series_names} for lane in ("phone", "computer")
    }
    for b, lane, name, n in rows:
        if b in index and lane in series:
            series[lane][name][index[b]] += n

    top_rows = store.con.execute(
        f"""
        WITH c AS (
            SELECT time_bucket(INTERVAL '{minutes} minutes', ts) AS b, domain, count(*) AS n
            FROM events
            WHERE profile_id = ? AND ts >= ? AND ts < ? AND status <> 'blocked'
            GROUP BY ALL
        )
        SELECT b, domain, n FROM c
        QUALIFY row_number() OVER (PARTITION BY b ORDER BY n DESC, domain) <= 8
        ORDER BY b, n DESC
        """,
        [profile_id, ctx.start, ctx.end],
    ).fetchall()
    top: list[list] = [[] for _ in buckets]
    for b, domain, n in top_rows:
        if b in index:
            top[index[b]].append([domain, n])
    return {
        "minutes": minutes,
        "buckets": [clock.ms(b) for b in buckets],
        "series": series,
        "top": top,
    }


def _heatmap(analysis: Analysis, ctx: ReportContext, clock: _Clock) -> dict | None:
    if ctx.end - ctx.start < timedelta(days=HEATMAP_MIN_DAYS):
        return None
    zone = ZoneInfo(ctx.tz)
    first_day = ctx.start.replace(tzinfo=UTC).astimezone(zone).date()
    last_day = (ctx.end - timedelta(seconds=1)).replace(tzinfo=UTC).astimezone(zone).date()
    days = [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]
    cells: dict[str, Counter] = defaultdict(Counter)
    for s in analysis.visible_sessions():
        t = max(s.start, ctx.start)
        while t < min(s.end, ctx.end):
            local = t.replace(tzinfo=UTC).astimezone(zone)
            cells[category_slot(s.category)][(local.date() - first_day).days, local.hour] += 1
            t += timedelta(minutes=1)
    return {
        "days": [d.strftime("%a %b %-d") for d in days],
        "grids": {
            cat: [[d, h, n] for (d, h), n in sorted(counter.items())]
            for cat, counter in cells.items()
        },
    }


def build_data(
    store: Store,
    profile_id: str,
    services: dict[str, Service],
    ctx: ReportContext,
    analysis: Analysis,
    unusual: Unusual,
    *,
    include_low: bool = True,
) -> dict:
    clock = _Clock(ctx.tz)
    owners = attribution.owners(store, profile_id)
    (n_events,) = store.con.execute(
        "SELECT count(*) FROM events WHERE profile_id = ? AND ts >= ? AND ts < ?",
        [profile_id, ctx.start, ctx.end],
    ).fetchone()
    embed_raw = n_events <= MAX_EMBED_EVENTS

    rows: dict[str, dict] = {}

    def row_for(service: Service, lane: str) -> str:
        rid = f"{service.id}|{lane}"
        rows.setdefault(rid, {"id": rid, "name": service.name, "category": service.category,
                              "slot": category_slot(service.category), "lane": lane})  # fmt: skip
        return rid

    sessions = []
    for i, s in enumerate(analysis.sessions):
        if s.confidence == "low" and not include_low:
            continue
        events = _session_events(store, profile_id, clock, s, owners) if embed_raw else None
        sessions.append({
            "id": f"s{i}", "row": row_for(s.service, s.lane), "start": clock.ms(s.start),
            "end": clock.ms(s.end), "startText": clock.text(s.start, "%a %b %-d %H:%M"),
            "endText": clock.text(s.end, "%H:%M"),
            "minutes": round(s.duration.total_seconds() / 60),
            "confidence": s.confidence, "lookups": s.lookups, "mean": s.mean, "peak": s.peak,
            "devices": s.devices, "minRate": s.service.min_rate,
            "hosts": [[h, n, h in s.inferred] for h, n in s.top_hosts],
            "events": events,
            "query": _query_cmd(ctx, clock, s.start, s.end, _service_regex(s.service), s.lane),
        })  # fmt: skip

    running = [
        {"row": row_for(r.service, r.lane), "start": clock.ms(max(r.start, ctx.start)),
         "end": clock.ms(min(r.end, ctx.end)), "blocked": r.blocked, "lookups": r.lookups,
         "text": f"{clock.text(r.start, '%a %H:%M')}–{clock.text(r.end, '%H:%M')}"}
        for r in analysis.running
    ]  # fmt: skip

    episodes = []
    for i, e in enumerate(unusual.episodes):
        rid = f"unusual|{e.lane}"
        rows.setdefault(rid, {"id": rid, "name": "Unusual sites", "category": "unusual",
                              "slot": "unusual", "lane": e.lane})  # fmt: skip
        names = {b.key for b in e.bursts} | {h for b in e.bursts for h, _ in b.hosts}
        keys = "|".join(re.escape(n) for n in sorted(names))
        episodes.append({
            "id": f"e{i}", "row": rid, "lane": e.lane, "title": e.title(),
            "start": clock.ms(e.start), "end": clock.ms(e.end + timedelta(minutes=1)),
            "startText": clock.text(e.start, "%a %b %-d %H:%M"),
            "endText": clock.text(e.end, "%H:%M"), "lookups": e.lookups,
            "bursts": [
                {"key": b.key, "category": b.category, "source": b.category_source,
                 "lookups": b.lookups, "daysSeen": b.days_seen, "inferred": b.inferred,
                 "startText": clock.text(b.start, "%a %H:%M"),
                 "endText": clock.text(b.end, "%H:%M"),
                 "hosts": b.hosts[:10]}
                for b in e.bursts
            ],
            "nearby": _nearby_named(store, profile_id, e),
            "events": _episode_events(store, profile_id, clock, e) if embed_raw else None,
            "query": _query_cmd(ctx, clock, e.start, e.end, keys, e.lane),
        })  # fmt: skip

    def row_order(r: dict) -> tuple:
        cat = r["category"]
        rank = (
            CATEGORY_ORDER.index(cat) if cat in CATEGORY_ORDER else (99 if cat == "unusual" else 50)
        )
        return (rank, cat, r["name"], r["lane"] != "computer")

    return {
        "meta": {
            "label": ctx.label,
            "window": ctx.window_label,
            "start": clock.ms(ctx.start),
            "end": clock.ms(ctx.end),
            "startText": clock.text(ctx.start, "%a %b %-d %H:%M"),
            "endText": clock.text(ctx.end, "%a %b %-d %H:%M"),
            "generated": clock.text(datetime.now(UTC).replace(tzinfo=None), "%a %b %-d %H:%M"),
            "lastSync": clock.text(ctx.last_sync, "%a %b %-d %H:%M") if ctx.last_sync else None,
            "tz": ctx.tz,
            "version": __version__,
            "events": n_events,
            "embedRaw": embed_raw,
            "warmingUp": unusual.warming_up,
        },  # fmt: skip
        "categories": [{"id": c, "name": CATEGORY_NAMES[c]} for c in (*CATEGORY_ORDER, "other")],
        "active": {
            category_slot(c): round(d.total_seconds() / 60)
            for c, d in analysis.active_time().items()
        },
        "rows": sorted(rows.values(), key=row_order),
        "sessions": sessions,
        "running": running,
        "overlaps": [
            {
                "start": clock.ms(o.start),
                "end": clock.ms(o.end),
                "a": o.a.service.name,
                "b": o.b.service.name,
                "text": f"{clock.text(o.start, '%a %H:%M')}–{clock.text(o.end, '%H:%M')}",
                "minutes": round(o.duration.total_seconds() / 60),
            }
            for o in analysis.overlaps
        ],  # fmt: skip
        "gaps": [
            {
                "lane": g.lane,
                "start": clock.ms(g.start),
                "end": clock.ms(g.end),
                "text": f"{clock.text(g.start, '%a %H:%M')}–{clock.text(g.end, '%a %H:%M')}",
                "minutes": round((g.end - g.start).total_seconds() / 60),
            }
            for g in analysis.gaps
        ],  # fmt: skip
        "episodes": episodes,
        "appActivity": [
            {
                "key": b.key,
                "lane": b.lane,
                "lookups": b.lookups,
                "text": clock.text(b.start, "%a %H:%M"),
            }
            for b in unusual.app_activity
        ],  # fmt: skip
        "frequency": _frequency(
            store, profile_id, services, ctx, clock, bucket_minutes(ctx.end - ctx.start)
        ),  # fmt: skip
        "heatmap": _heatmap(analysis, ctx, clock),
        "eventColumns": EVENT_COLUMNS,
    }


def _script_json(data: dict) -> str:
    """JSON that is safe inside a <script> element."""
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_html(data: dict) -> str:
    pkg = resources.files("querytale")
    template = (pkg / "templates" / "report.html").read_text()
    echarts = (pkg / "vendor" / "echarts-5.6.0.min.js").read_text()
    title = f"{data['meta']['label']} · {data['meta']['window']}"
    html = template.replace("__CSP__", CSP).replace("__TITLE__", _html_escape(title))
    html = html.replace("__DATA__", _script_json(data))
    # Inline last: the library text must not be scanned for placeholders.
    return html.replace("/*__ECHARTS__*/", echarts.replace("</script", "<\\/script"))


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def report_path(label: str, ctx: ReportContext, clock_tz: str) -> Path:
    """reports/<label>/<date>_<time>_<window>.html, never overwritten."""
    now = datetime.now(ZoneInfo(clock_tz))
    window = re.sub(r"[^a-z0-9]+", "-", ctx.window_label.lower()).strip("-")
    folder = paths.ensure_private_dir(paths.home() / "reports" / label)
    base = folder / f"{now:%Y-%m-%d_%H%M%S}_{window}"
    path, n = base.with_suffix(".html"), 1
    while path.exists():
        n += 1
        path = Path(f"{base}-{n}.html")
    return path


def write_report(html: str, path: Path) -> Path:
    paths.ensure_private_dir(path.parent)
    path.write_text(html)
    paths.make_private(path)
    return path
