"""The querytale command line."""

from __future__ import annotations

import csv
import functools
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from querytale import __version__, config, keystore, paths
from querytale.api import ApiError, NextDNSClient, ReadOnlyViolation
from querytale.config import Config, ConfigError, Profile
from querytale.derive import ensure_minute_service
from querytale.importer import import_csv
from querytale.quiet import quiet_intervals
from querytale.services import ServiceError, load_services, user_services_dir
from querytale.sessions import analyze
from querytale.store import Store
from querytale.sync import SyncLocked, SyncResult, sync_lock, sync_profile
from querytale.textreport import Clock, render_calibrate, render_services, render_summary
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
            f"from {r.rows:,} log rows in {r.pages:,} request{'s' if r.pages != 1 else ''}"
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
        ensure_minute_service(store, profile.id, services)
        result = analyze(store, profile.id, services, window.start, window.end)
        last_sync = store.get_sync_state(profile.id).last_run_at
    render_summary(console, label, window.label, result, Clock(cfg.tz()), last_sync,
                   include_low=show_low)  # fmt: skip


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
        ensure_minute_service(store, profile.id, services)
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


if __name__ == "__main__":
    app()
