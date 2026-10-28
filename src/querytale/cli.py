"""The querytale command line."""

from __future__ import annotations

import csv
import functools
import os
import re
import sys
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from querytale import __version__, config, derive, keystore, paths
from querytale import doctor as doctor_checks
from querytale.api import ApiError, NextDNSClient, ReadOnlyViolation
from querytale.config import Config, ConfigError, Profile
from querytale.domains import IGNORE, load_labels, normalize_domain, save_labels
from querytale.importer import import_csv
from querytale.novelty import find_unusual
from querytale.quiet import quiet_intervals
from querytale.report import ReportContext, build_data, render_html, report_path, write_report
from querytale.services import ServiceError, load_services, user_services_dir
from querytale.sessions import analyze
from querytale.store import Store
from querytale.sync import SyncLocked, SyncResult, sync_lock, sync_profile
from querytale.textreport import (
    Clock,
    render_calibrate,
    render_services,
    render_summary,
    render_unusual,
)  # fmt: skip
from querytale.timewindow import WindowError, parse_duration, parse_window

app = typer.Typer(
    help="See what your NextDNS queries tell. Syncs logs to a local DuckDB file for review.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

KNOWN_ERRORS = (
    ApiError, ConfigError, ReadOnlyViolation, ServiceError, SyncLocked, WindowError, ValueError,
)  # fmt: skip


def handle_errors(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KNOWN_ERRORS as e:
            err.print(f"[red]Error:[/red] {escape(str(e))}")
            raise typer.Exit(1) from None
        except KeyboardInterrupt:
            err.print("\n[yellow]Interrupted.[/yellow] Synced pages are kept; run again to resume.")
            raise typer.Exit(130) from None

    return wrapper


def _version(value: bool) -> None:
    if value:
        console.print(f"querytale {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    # Everything querytale writes is a child's browsing history: private to this user.
    os.umask(0o077)


# Helpers ---------------------------------------------------------------------------------


def require_client() -> NextDNSClient:
    key = keystore.get_api_key()
    if not key:
        raise ConfigError(f"No NextDNS API key. Run `querytale init` or set {keystore.ENV_VAR}.")
    return NextDNSClient(key)


def run_sync(
    store: Store, cfg: Config, profiles: list[Profile], backfill: timedelta | None = None
) -> list[SyncResult]:
    results = []
    with sync_lock(), require_client() as client:
        for profile in profiles:
            with Progress(
                TextColumn(f"Syncing [bold]{profile.label}[/bold]"),
                BarColumn(),
                TextColumn("{task.percentage:>3.0f}%  {task.description}"),
                TimeElapsedColumn(),
                console=err,
                transient=True,
            ) as bar:
                task = bar.add_task("", total=1.0)

                def progress(frac, ts, bar=bar, task=task):
                    local = ts.replace(tzinfo=UTC).astimezone(ZoneInfo(cfg.tz()))
                    bar.update(task, completed=frac, description=f"{local:%b %d %H:%M}")

                results.append(
                    sync_profile(store, client, cfg, profile, backfill=backfill, progress=progress)
                )
    return results


def print_sync_results(results: list[SyncResult]) -> None:
    for r in results:
        console.print(
            f"[bold]{r.label}[/bold]: {r.added:,} new lookup events "
            f"from {r.rows:,} log rows in {r.pages:,} page{'s' if r.pages != 1 else ''}"
        )


def maybe_sync(store: Store, cfg: Config, profile: Profile, no_sync: bool) -> None:
    if no_sync:
        return
    if not keystore.get_api_key():
        err.print("[dim]No API key configured; showing local data only.[/dim]")
        return
    try:
        run_sync(store, cfg, [profile])
    except (ApiError, SyncLocked) as e:
        err.print(f"[yellow]Sync skipped:[/yellow] {e}. Showing local data.")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def do_import(store: Store, cfg: Config, profile: Profile, path: Path) -> None:
    with sync_lock(), console.status(f"Importing {path.name}…"):
        result = import_csv(store, cfg, profile, path)
    tz = ZoneInfo(cfg.tz())
    span = ""
    if result.first_ts:
        first = result.first_ts.replace(tzinfo=UTC).astimezone(tz)
        last = result.last_ts.replace(tzinfo=UTC).astimezone(tz)
        span = f", {first:%Y-%m-%d} to {last:%Y-%m-%d}"
    console.print(
        f"[bold]{profile.label}[/bold]: {result.rows:,} rows read, "
        f"{result.added:,} new lookup events{span}"
    )


def download_and_import(store: Store, cfg: Config, profile: Profile) -> None:
    dest = paths.ensure_private_dir(paths.home() / "imports") / f"{profile.id}.csv"
    try:
        with (
            require_client() as client,
            Progress(
                TextColumn(f"Downloading export for [bold]{profile.label}[/bold]"),
                BarColumn(),
                DownloadColumn(),
                console=err,
                transient=True,
            ) as bar,
        ):
            task = bar.add_task("", total=None)
            client.download_logs(
                profile.id, dest, lambda done, total: bar.update(task, completed=done, total=total)
            )
        do_import(store, cfg, profile, dest)
    finally:
        dest.unlink(missing_ok=True)


# Commands --------------------------------------------------------------------------------


@app.command()
@handle_errors
def init(
    backfill: Annotated[
        str | None, typer.Option(help="How much history to fetch, e.g. 30d or 90d.")
    ] = None,
    no_backfill: Annotated[bool, typer.Option(help="Skip the first sync.")] = False,
) -> None:
    """Store the API key, choose profiles to track, and run the first sync."""
    cfg = config.load()

    key = os.environ.get(keystore.ENV_VAR)
    if key:
        console.print(f"Using the API key from {keystore.ENV_VAR}.")
    elif (stored := keystore.stored_api_key()) and typer.confirm(
        "An API key is already in the keychain. Keep it?", default=True
    ):
        key = stored
    else:
        console.print("Find your API key at the bottom of https://my.nextdns.io/account")
        key = typer.prompt("NextDNS API key", hide_input=True).strip()

    with NextDNSClient(key) as client:
        profiles = client.profiles()
    if not os.environ.get(keystore.ENV_VAR):
        keystore.set_api_key(key)
        console.print("[green]API key saved to the keychain.[/green]")
    if not profiles:
        raise ConfigError("This API key has no NextDNS profiles.")

    table = Table("Profile id", "Name", "Label", title="NextDNS profiles")
    known = {p.id: p.label for p in cfg.profiles.values()}
    for p in profiles:
        table.add_row(escape(p["id"]), escape(p.get("name", "")), known.get(p["id"], ""))
    console.print(table)
    console.print(
        "Give each person's profile a short label, e.g. [bold]alex[/bold]. "
        "Leave it blank to skip a profile."
    )
    tracked: dict[str, Profile] = {}
    for p in profiles:
        label = typer.prompt(
            f"Label for {p.get('name') or p['id']}",
            default=known.get(p["id"], ""),
            show_default=bool(known.get(p["id"])),
        ).strip()
        if label:
            label = slug(label)
            tracked[label] = Profile(label=label, id=p["id"], nextdns_name=p.get("name", ""))
    if not tracked:
        raise ConfigError("No profiles chosen; nothing to track.")
    cfg.profiles = tracked
    console.print(f"Config written to {config.save(cfg)}")

    with Store.open() as store:
        for profile in tracked.values():
            answer = typer.prompt(
                f"Optional: a CSV export for {profile.label} adds up to a year of history. "
                "Path, 'download', or Enter to skip",
                default="",
                show_default=False,
            ).strip()
            if answer.lower() == "download":
                download_and_import(store, cfg, profile)
            elif answer:
                do_import(store, cfg, profile, Path(answer).expanduser())

        if no_backfill:
            return
        span = parse_duration(backfill) if backfill else None
        print_sync_results(run_sync(store, cfg, list(tracked.values()), span))
    console.print(f"Ready. Try [bold]querytale query {next(iter(tracked))} --last 1h[/bold]")


@app.command()
@handle_errors
def sync(
    label: Annotated[str | None, typer.Argument(help="Profile label; all when omitted.")] = None,
    backfill: Annotated[
        str | None, typer.Option(help="Fetch history back this far, e.g. 90d.")
    ] = None,
) -> None:
    """Fetch new log entries from the NextDNS API."""
    cfg = config.load()
    profiles = [cfg.profile(label)] if label else list(cfg.profiles.values())
    if not profiles:
        raise ConfigError("No profiles configured. Run `querytale init`.")
    span = parse_duration(backfill) if backfill else None
    with Store.open() as store:
        print_sync_results(run_sync(store, cfg, profiles, span))


@app.command("import")
@handle_errors
def import_(
    file: Annotated[Path | None, typer.Argument(help="A NextDNS CSV export.")] = None,
    profile: Annotated[str, typer.Option("--profile", "-p", help="Profile label.")] = ...,
    download: Annotated[
        bool, typer.Option("--download", help="Fetch the export from NextDNS instead.")
    ] = False,
    profile_id: Annotated[
        str | None,
        typer.Option(help="Register the label with this NextDNS profile id if it is new."),
    ] = None,
) -> None:
    """Load a full CSV export for a deeper baseline. Safe to repeat; overlaps are dropped."""
    if bool(file) == download:
        raise ConfigError("Give either a FILE or --download.")
    cfg = config.load()
    if profile not in cfg.profiles and profile_id:
        cfg.profiles[profile] = Profile(label=profile, id=profile_id)
        config.save(cfg)
    prof = cfg.profile(profile)
    with Store.open() as store:
        if download:
            download_and_import(store, cfg, prof)
        else:
            if not file.is_file():
                raise ConfigError(f"No such file: {file}")
            do_import(store, cfg, prof, file)


@app.command()
@handle_errors
def query(
    label: Annotated[str, typer.Argument(help="Profile label.")],
    match: Annotated[
        str | None, typer.Option("--match", "-m", help="Regex on the full hostname.")
    ] = None,
    last: Annotated[str | None, typer.Option(help="e.g. 90m, 6h, 3d, 2w.")] = None,
    date: Annotated[str | None, typer.Option(help="A local day, YYYY-MM-DD.")] = None,
    from_: Annotated[str | None, typer.Option("--from", help="Local start time.")] = None,
    to: Annotated[str | None, typer.Option(help="Local end time (default now).")] = None,
    lane: Annotated[str | None, typer.Option(help="phone or computer.")] = None,
    include_blocked: Annotated[
        bool, typer.Option("--include-blocked", help="Also show blocked lookups.")
    ] = False,
    as_csv: Annotated[bool, typer.Option("--csv", help="Write CSV to stdout.")] = False,
    limit: Annotated[int, typer.Option(help="Max rows in the table (CSV is unlimited).")] = 500,
    no_sync: Annotated[bool, typer.Option("--no-sync", help="Use local data only.")] = False,
) -> None:
    """Raw lookup events matching a hostname pattern, as a table or CSV."""
    cfg = config.load()
    profile = cfg.profile(label)
    tz = ZoneInfo(cfg.tz())
    window = parse_window(last=last, on_date=date, from_=from_, to=to, tz=cfg.tz())
    if lane and lane not in ("phone", "computer"):
        raise ConfigError("--lane must be phone or computer")
    if match:
        try:
            re.compile(match)
        except re.error as e:
            raise ConfigError(f"Bad --match regex: {e}") from None

    with Store.open() as store:
        maybe_sync(store, cfg, profile, no_sync)
        rows = store.query_events(
            profile.id, window.start, window.end, match=match,
            include_blocked=include_blocked, lane=lane, limit=None if as_csv else limit + 1,
        )  # fmt: skip

    def local(ts):
        return ts.replace(tzinfo=UTC).astimezone(tz)

    if as_csv:
        out = csv.writer(sys.stdout)
        out.writerow(["time", "lane", "device", "domain", "root", "qtypes", "status", "reasons",
                      "tracker"])  # fmt: skip
        for ts, *rest in rows:
            out.writerow([local(ts).isoformat(timespec="milliseconds"), *rest])
        return

    truncated = len(rows) > limit
    rows = rows[:limit]
    title = f"{profile.label} · {window.label}" + (f" · /{match}/" if match else "")
    table = Table(title=escape(title))
    for col in ("Time", "Lane", "Device", "Domain", "Types", "Status"):
        table.add_column(col, no_wrap=col in ("Time", "Domain"))
    for ts, lane_, device, domain, _root, qtypes, status, reasons, _tracker in rows:
        shown = f"[red]{status}[/red]" if status == "blocked" else status
        if reasons and status == "blocked":
            shown += f" [dim]{escape(reasons)}[/dim]"
        table.add_row(
            f"{local(ts):%a %m-%d %H:%M:%S}", lane_, escape(device or ""), escape(domain), qtypes,
            shown,
        )  # fmt: skip
    console.print(table)
    note = f"{len(rows):,} events"
    if truncated:
        note = f"first {limit:,} events shown; use --limit or --csv for all"
    console.print(f"[dim]{note}[/dim]")


@app.command()
@handle_errors
def summary(
    label: Annotated[str, typer.Argument(help="Profile label.")],
    last: Annotated[str | None, typer.Option(help="e.g. 6h, 3d (default 24h).")] = None,
    date: Annotated[str | None, typer.Option(help="A local day, YYYY-MM-DD.")] = None,
    from_: Annotated[str | None, typer.Option("--from", help="Local start time.")] = None,
    to: Annotated[str | None, typer.Option(help="Local end time (default now).")] = None,
    show_low: Annotated[bool, typer.Option(help="Also list low-confidence bursts.")] = False,
    no_sync: Annotated[bool, typer.Option("--no-sync", help="Use local data only.")] = False,
) -> None:
    """Sessions, app-running time, overlaps and silent stretches, as text."""
    cfg = config.load()
    profile = cfg.profile(label)
    window = parse_window(last=last, on_date=date, from_=from_, to=to, tz=cfg.tz())
    services = load_services()
    with Store.open() as store:
        maybe_sync(store, cfg, profile, no_sync)
        derive.ensure(store, cfg, profile.id, services)
        result = analyze(store, profile.id, services, window.start, window.end)
        unusual = find_unusual(store, profile.id, services, window.start, window.end, cfg.tz())
        last_sync = store.get_sync_state(profile.id).last_run_at
    render_summary(console, label, window.label, result, Clock(cfg.tz()), last_sync, unusual,
                   include_low=show_low)  # fmt: skip


@app.command()
@handle_errors
def report(
    label: Annotated[str, typer.Argument(help="Profile label.")],
    last: Annotated[str | None, typer.Option(help="e.g. 48h, 7d (default 24h).")] = None,
    date: Annotated[str | None, typer.Option(help="A local day, YYYY-MM-DD.")] = None,
    from_: Annotated[str | None, typer.Option("--from", help="Local start time.")] = None,
    to: Annotated[str | None, typer.Option(help="Local end time (default now).")] = None,
    out: Annotated[
        Path | None, typer.Option(help="Write here instead of the reports folder.")
    ] = None,
    no_sync: Annotated[bool, typer.Option("--no-sync", help="Use local data only.")] = False,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Don't open it in the browser.")
    ] = False,
) -> None:
    """Sync, analyze, and write a self-contained interactive HTML report."""
    cfg = config.load()
    profile = cfg.profile(label)
    window = parse_window(last=last, on_date=date, from_=from_, to=to, tz=cfg.tz())
    services = load_services()
    with Store.open() as store:
        maybe_sync(store, cfg, profile, no_sync)
        with console.status("Analyzing…"):
            derive.ensure(store, cfg, profile.id, services)
            result = analyze(store, profile.id, services, window.start, window.end)
            found = find_unusual(store, profile.id, services, window.start, window.end, cfg.tz())
            ctx = ReportContext(label, window.label, window.start, window.end, cfg.tz(),
                                store.get_sync_state(profile.id).last_run_at)  # fmt: skip
            data = build_data(store, profile.id, services, ctx, result, found)
    path = write_report(render_html(data), out or report_path(label, ctx, cfg.tz()))
    size = path.stat().st_size / 1e6
    console.print(f"Report written to {escape(str(path))} ({size:.1f} MB)")
    if not no_open:
        webbrowser.open(path.resolve().as_uri())


@app.command()
@handle_errors
def unusual(
    label: Annotated[str, typer.Argument(help="Profile label.")],
    last: Annotated[str | None, typer.Option(help="e.g. 24h, 7d (default 7d).")] = None,
    date: Annotated[str | None, typer.Option(help="A local day, YYYY-MM-DD.")] = None,
    from_: Annotated[str | None, typer.Option("--from", help="Local start time.")] = None,
    to: Annotated[str | None, typer.Option(help="Local end time (default now).")] = None,
    no_sync: Annotated[bool, typer.Option("--no-sync", help="Use local data only.")] = False,
) -> None:
    """Rare sites that showed up in clusters (novelty episodes) only."""
    cfg = config.load()
    profile = cfg.profile(label)
    window = parse_window(last=last, on_date=date, from_=from_, to=to, tz=cfg.tz(),
                          default_last="7d")  # fmt: skip
    services = load_services()
    with Store.open() as store:
        maybe_sync(store, cfg, profile, no_sync)
        derive.ensure(store, cfg, profile.id, services)
        found = find_unusual(store, profile.id, services, window.start, window.end, cfg.tz())
    clock = Clock(cfg.tz())
    console.print(f"[bold]{label}[/bold] · {window.label} ({clock.span(window.start, window.end)})")
    render_unusual(console, found, clock, heading=False)


@app.command()
@handle_errors
def label(
    domain: Annotated[str, typer.Argument(help="A site, e.g. crazygames.com (subdomains too).")],
    category: Annotated[
        str | None, typer.Argument(help=f"e.g. games, school, social; '{IGNORE}' hides it.")
    ] = None,
    remove: Annotated[bool, typer.Option("--remove", help="Delete this label.")] = False,
) -> None:
    """Classify a site once; every future report uses it."""
    key = normalize_domain(domain)
    labels = load_labels()
    if remove:
        if labels.pop(key, None) is None:
            raise ConfigError(f"No label for {key}")
        save_labels(labels)
        console.print(f"Removed the label for {escape(key)}.")
        return
    if not category:
        raise ConfigError("Give a CATEGORY, or --remove.")
    labels[key] = category.strip().lower()
    save_labels(labels)
    console.print(f"{escape(key)} → [bold]{escape(labels[key])}[/bold]")


@app.command()
@handle_errors
def labels() -> None:
    """List your site labels."""
    current = load_labels()
    if not current:
        console.print("No labels yet. Add one with `querytale label DOMAIN CATEGORY`.")
        return
    table = Table("Site", "Category", show_edge=False)
    for key, category in sorted(current.items(), key=lambda kv: (kv[1], kv[0])):
        table.add_row(escape(key), escape(category))
    console.print(table)


@app.command()
@handle_errors
def calibrate(
    service: Annotated[str, typer.Argument(help="Service id, see `querytale services`.")],
    label: Annotated[str, typer.Argument(help="Profile label.")],
    last: Annotated[str, typer.Option(help="How far back to look.")] = "30d",
    no_sync: Annotated[bool, typer.Option("--no-sync", help="Use local data only.")] = False,
) -> None:
    """Per-minute count histograms for one service, to tune its thresholds."""
    cfg = config.load()
    profile = cfg.profile(label)
    window = parse_window(last=last, tz=cfg.tz())
    services = load_services()
    if service not in services:
        raise ConfigError(f"Unknown service {service!r}; see `querytale services`.")
    with Store.open() as store:
        maybe_sync(store, cfg, profile, no_sync)
        derive.ensure(store, cfg, profile.id, services)
        rows = store.con.execute(
            """
            SELECT minute, lane, n_allowed, n_blocked, hb_allowed, hb_blocked
            FROM minute_service
            WHERE profile_id = ? AND service = ? AND minute >= ? AND minute < ?
            ORDER BY minute
            """,
            [profile.id, service, window.start, window.end],
        ).fetchall()
        result = analyze(store, profile.id, {service: services[service]}, window.start,
                         window.end, details=False)  # fmt: skip
    quiet = quiet_intervals(cfg, window.start, window.end)
    render_calibrate(console, services[service], rows, result, quiet, Clock(cfg.tz()),
                     window.label)  # fmt: skip


@app.command("services")
@handle_errors
def services_() -> None:
    """List configured services and their patterns."""
    render_services(console, load_services())
    console.print(f"[dim]Add or override services with YAML files in {user_services_dir()}[/dim]")


@app.command()
@handle_errors
def doctor(
    label: Annotated[
        str | None, typer.Argument(help="Profile for suggestions (default first).")
    ] = None,
) -> None:
    """Check config and sync health; suggest heartbeats and sites worth labeling."""
    cfg = config.load()
    services = load_services()
    with Store.open() as store:
        for p in cfg.profiles.values():
            if store.event_range(p.id)[1] is not None:
                derive.ensure(store, cfg, p.id, services)
        rep = doctor_checks.run(store, cfg, services, label)
    for c in rep.checks:
        mark = "[green]✓[/green]" if c.ok else "[yellow]![/yellow]"
        hint = f"  [dim]→ {escape(c.hint)}[/dim]" if c.hint else ""
        console.print(f"{mark} {escape(c.text)}{hint}")

    if rep.attribution and rep.attribution.lookups:
        a = rep.attribution
        console.print(
            f"\n[bold]CDN host attribution[/bold]: {a.hosts:,} generic hosts, "
            f"{a.lookups:,} lookups · "
            f"named in hostname {a.share('name'):.0%} · learned {a.share('cooccurrence'):.0%} · "
            f"unattributed {a.share('unattributed'):.0%} · too rare {a.share('too_rare'):.0%}"
        )
    if rep.heartbeats:
        console.print(
            "\n[bold]Possible heartbeats[/bold] (lookups like clockwork in the last 30 days; "
            "add to a service's heartbeat list if it is an app checking in)"
        )
        for h in rep.heartbeats[:12]:
            owner = f" · in {h.service}" if h.service else ""
            console.print(
                f"  {escape(h.host)} [dim]every {h.median_gap_s / 60:.0f} min, "
                f"{h.lookups:,} lookups{escape(owner)}[/dim]"
            )
        console.print("  [dim]In a service YAML:  heartbeat:\n    - host: '<hostname>'[/dim]")
    if rep.unclassified:
        console.print("\n[bold]Busiest unclassified sites[/bold] (last 30 days)")
        for root, n in rep.unclassified:
            console.print(
                f"  {escape(root)} [dim]{n:,} lookups · querytale label {escape(root)} "
                "<category>[/dim]"
            )
    if rep.quiet_sessions:
        console.print(
            "\n[bold]Sessions inside quiet windows[/bold] (tighten thresholds, or a real exception)"
        )
        for quiet, what, when in rep.quiet_sessions:
            console.print(f"  {escape(quiet)}: {escape(what)} {when}")


@app.command()
@handle_errors
def prune(
    older_than: Annotated[
        str, typer.Option(help="Delete data and reports older than this.")
    ] = "400d",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
    dry_run: Annotated[bool, typer.Option(help="Only show what would be deleted.")] = False,
) -> None:
    """Delete old lookups, derived rows and reports from this computer."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - parse_duration(older_than)
    reports_dir = paths.home() / "reports"
    old_reports = [
        p for p in (reports_dir.rglob("*.html") if reports_dir.exists() else [])
        if datetime.fromtimestamp(p.stat().st_mtime, UTC).replace(tzinfo=None) < cutoff
    ]  # fmt: skip
    with Store.open() as store:
        (n_events,) = store.con.execute(
            "SELECT count(*) FROM events WHERE ts < ?", [cutoff]
        ).fetchone()
        console.print(
            f"Older than {older_than} ({cutoff:%Y-%m-%d} UTC): {n_events:,} lookup events and "
            f"{len(old_reports)} reports."
        )
        if dry_run or (not n_events and not old_reports):
            return
        if not yes and not typer.confirm("Delete them permanently?", default=False):
            raise typer.Exit(1)
        derive.prune(store, cutoff)
    for p in old_reports:
        p.unlink()
    console.print("[green]Done.[/green]")


if __name__ == "__main__":
    app()
