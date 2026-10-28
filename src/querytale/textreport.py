"""Terminal rendering for `summary`, `calibrate` and `services`."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from querytale.quiet import QuietInterval
from querytale.services import Service
from querytale.sessions import Analysis, Session

CONFIDENCE_STYLE = {"high": "bold", "medium": "", "low": "dim"}
BAR = "█"


def fmt_duration(d: timedelta) -> str:
    minutes = round(d.total_seconds() / 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


class Clock:
    """Formats UTC timestamps in the user's zone."""

    def __init__(self, tz: str) -> None:
        self.zone = ZoneInfo(tz)

    def local(self, ts: datetime) -> datetime:
        return ts.replace(tzinfo=UTC).astimezone(self.zone)

    def at(self, ts: datetime) -> str:
        return f"{self.local(ts):%a %H:%M}"

    def span(self, a: datetime, b: datetime) -> str:
        la, lb = self.local(a), self.local(b)
        return (
            f"{la:%a %H:%M}–{lb:%H:%M}"
            if la.date() == lb.date()
            else f"{la:%a %H:%M}–{lb:%a %H:%M}"
        )


def render_summary(
    console: Console,
    label: str,
    window_label: str,
    analysis: Analysis,
    clock: Clock,
    last_sync: datetime | None,
    *,
    include_low: bool = False,
) -> None:
    a = analysis
    synced = clock.at(last_sync) if last_sync else "never (local data only)"
    console.print(
        f"[bold]{label}[/bold] · {window_label} ({clock.span(a.start, a.end)}) · last sync {synced}"
    )

    active = a.active_time()
    parts = [f"{cat} [bold]{fmt_duration(d)}[/bold]" for cat, d in active.items()]
    console.print(
        "Active time: " + (" · ".join(parts) if parts else "no sessions")
        + f"   Overlaps: [bold]{len(a.overlaps)}[/bold]"
    )  # fmt: skip

    sessions = a.visible_sessions(include_low)
    if sessions:
        table = Table(title="Sessions", title_justify="left", show_edge=False)
        for col in ("When", "Length", "Service", "Lane", "Confidence", "Rate", "Top hosts"):
            table.add_column(col, no_wrap=col != "Top hosts")
        for s in sessions:
            style = CONFIDENCE_STYLE[s.confidence]
            hosts = escape(", ".join(h for h, _ in s.top_hosts[:3]))
            table.add_row(
                clock.span(s.start, s.end), fmt_duration(s.duration),
                escape(s.service.name), s.lane, s.confidence, f"{s.mean:g}/3m", hosts,
                style=style,
            )  # fmt: skip
        console.print(table)
    hidden = len(a.sessions) - len(sessions)
    if hidden:
        console.print(f"[dim]{hidden} low-confidence bursts hidden (--show-low to list)[/dim]")

    if a.running:
        console.print("\n[bold]App running[/bold] (heartbeats, not use)")
        by_app: dict[tuple[str, str], list[str]] = defaultdict(list)
        for r in a.running:
            text = clock.span(max(r.start, a.start), min(r.end, a.end))
            by_app[r.service.name, r.lane].append(
                f"[dim]{text} blocked[/dim]" if r.blocked else text
            )
        for (name, lane), spans in by_app.items():
            console.print(f"  {escape(name)} ({lane}): " + ", ".join(spans))

    if a.overlaps:
        console.print("\n[bold]Overlaps[/bold]")
        for o in a.overlaps:
            console.print(
                f"  {escape(o.a.service.name)} × {escape(o.b.service.name)}: "
                f"{clock.span(o.start, o.end)} ({fmt_duration(o.duration)})"
            )

    if a.gaps:
        console.print(
            "\n[bold]Silent stretches[/bold] (no lookups: off, asleep, or not on NextDNS)"
        )
        for g in a.gaps:
            console.print(
                f"  {g.lane}: {clock.span(g.start, g.end)} ({fmt_duration(g.end - g.start)})"
            )


def _histogram(console: Console, title: str, values: list[int], mark: int | None = None) -> None:
    buckets = [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 9), (10, 19), (20, 49), (50, 10**9)]
    counts = Counter()
    for v in values:
        for lo, hi in buckets:
            if lo <= v <= hi:
                counts[lo, hi] += 1
                break
    total = max(len(values), 1)
    peak = max(counts.values(), default=1)
    table = Table(title=title, title_justify="left", show_edge=False, show_header=False)
    table.add_column(justify="right")
    table.add_column(justify="right")
    table.add_column(justify="right")
    table.add_column()
    for lo, hi in buckets:
        n = counts[lo, hi]
        name = str(lo) if lo == hi else (f"{lo}+" if hi > 10**6 else f"{lo}–{hi}")
        flag = " ◀ min_rate" if mark is not None and lo <= mark <= hi else ""
        table.add_row(name, f"{n:,}", f"{100 * n / total:.1f}%", BAR * round(30 * n / peak) + flag)
    console.print(table)


def render_calibrate(
    console: Console,
    service: Service,
    rows: list[tuple],
    analysis: Analysis,
    quiet: list[QuietInterval],
    clock: Clock,
    window_label: str,
) -> None:
    """rows: (minute, lane, n_allowed, n_blocked, hb_allowed, hb_blocked) for this service."""
    console.print(
        f"[bold]{escape(service.name)}[/bold] ({escape(service.id)}) · {window_label} · "
        f"min_rate {service.min_rate}/3m · min_duration {fmt_duration(service.min_duration)} · "
        f"max_gap {fmt_duration(service.max_gap)}"
        + ("" if service.verified else " · [yellow]unverified[/yellow]")
    )
    lanes = sorted({r[1] for r in rows})
    for lane in lanes:
        mine = [r for r in rows if r[1] == lane]
        console.print(f"\n[bold underline]{lane}[/bold underline]")
        activity = {r[0]: r[2] for r in mine if r[2]}
        _histogram(
            console,
            f"Activity lookups per minute ({len(mine):,} minutes with any "
            f"{escape(service.name)} lookup)",
            [r[2] for r in mine],
        )
        rolling = []
        for m in activity:
            rolling.append(sum(activity.get(m - timedelta(minutes=j), 0) for j in range(3)))
        _histogram(console, "Rolling 3-minute sum at minutes with activity", rolling,
                   service.min_rate)  # fmt: skip
        for col, what in ((4, "allowed"), (5, "blocked")):
            beats = sorted(r[0] for r in mine if r[col])
            if not beats:
                continue
            gaps = [(b - a).total_seconds() / 60 for a, b in zip(beats, beats[1:], strict=False)]
            cadence = f", median gap {median(gaps):.0f} min" if gaps else ""
            ones = sum(1 for r in mine if r[col] == 1)
            console.print(
                f"Heartbeats ({what}): {len(beats):,} minutes, {100 * ones / len(beats):.0f}% "
                f"with exactly 1 lookup{cadence}"
            )

    found = [s for s in analysis.sessions if s.service.id == service.id]
    by_conf = Counter(s.confidence for s in found)
    console.print(
        f"\nSessions: high {by_conf['high']}, medium {by_conf['medium']}, "
        f"low {by_conf['low']} (hidden by default)"
    )
    if quiet:
        inside = _in_quiet(found, quiet)
        console.print(
            f"Quiet windows in range: {len(quiet)}; medium/high sessions inside them: "
            f"[bold]{len(inside)}[/bold]"
        )
        for s, q in inside:
            console.print(
                f"  [yellow]{escape(q.name)}[/yellow] {clock.span(s.start, s.end)} {s.lane} "
                f"{fmt_duration(s.duration)}, {s.mean:g}/3m, {s.confidence}: "
                "tighten thresholds, or a real exception to look at"
            )
    else:
        console.print("[dim]No quiet windows configured ([[quiet]] or shabbat in config).[/dim]")


def _in_quiet(
    sessions: list[Session], quiet: list[QuietInterval]
) -> list[tuple[Session, QuietInterval]]:
    out = []
    for s in sessions:
        if s.confidence == "low":
            continue
        for q in quiet:
            if s.start < q.end and s.end > q.start:
                out.append((s, q))
                break
    return out


def render_services(console: Console, services: dict[str, Service]) -> None:
    category = None
    for s in sorted(services.values(), key=lambda s: (s.category, s.id)):
        if s.category != category:
            category = s.category
            console.print(f"\n[bold underline]{category}[/bold underline]")
        source = "bundled" if s.source.startswith("preset") else s.source
        verified = "verified" if s.verified else "[yellow]unverified[/yellow]"
        console.print(
            f"[bold]{escape(s.id)}[/bold] ({escape(s.name)}) · min_rate {s.min_rate}/3m · "
            f"min_duration {fmt_duration(s.min_duration)} · max_gap {fmt_duration(s.max_gap)} · "
            f"{verified} · [dim]{escape(source)}[/dim]"
        )
        for kind in ("domain", "host", "regex"):
            values = [p.value for p in s.patterns if p.kind == kind]
            if values:
                console.print(f"  {kind}s: " + escape(", ".join(values)))
        if s.heartbeat:
            console.print("  heartbeat: " + escape(", ".join(p.value for p in s.heartbeat)))
