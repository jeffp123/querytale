"""Health checks and tuning suggestions for `querytale doctor`."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from querytale import attribution, domains, keystore, paths
from querytale.config import Config
from querytale.novelty import _reason_categories
from querytale.quiet import quiet_intervals
from querytale.services import Service
from querytale.sessions import analyze
from querytale.store import Store

HEARTBEAT_MIN_COUNT = 20
HEARTBEAT_MIN_GAP_S = 30
HEARTBEAT_MAX_IQR = 0.25  # interquartile range as a share of the median gap
LOOKBACK = timedelta(days=30)


@dataclass
class Check:
    ok: bool
    text: str
    hint: str = ""


@dataclass
class HeartbeatSuggestion:
    host: str
    lookups: int
    median_gap_s: float
    iqr_share: float
    service: str | None  # owning service, if any


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    heartbeats: list[HeartbeatSuggestion] = field(default_factory=list)
    unclassified: list[tuple[str, int]] = field(default_factory=list)
    quiet_sessions: list[tuple[str, str, str]] = field(default_factory=list)  # quiet, service, when
    attribution: attribution.AttributionStats | None = None


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def file_checks(cfg: Config) -> list[Check]:
    out = []
    home = paths.home()
    if home.exists():
        ok = _mode(home) & 0o077 == 0
        out.append(Check(ok, f"Data folder {home} is private" if ok else
                         f"Data folder {home} is readable by others ({oct(_mode(home))})",
                         "" if ok else f"chmod 700 '{home}'"))  # fmt: skip
        for path in [paths.db_path(), paths.config_path(), domains.labels_path()]:
            if path.exists() and _mode(path) & 0o077:
                out.append(
                    Check(False, f"{path.name} is readable by others", f"chmod 600 '{path}'")
                )
        reports = home / "reports"
        if reports.exists():
            loose = [p for p in reports.rglob("*.html") if _mode(p) & 0o077]
            if loose:
                out.append(Check(False, f"{len(loose)} reports are readable by others",
                                 f"chmod 600 '{reports}'/*/*.html"))  # fmt: skip
    if not cfg.profiles:
        out.append(Check(False, "No profiles configured", "querytale init"))
    else:
        names = ", ".join(f"{p.label} ({p.id})" for p in cfg.profiles.values())
        out.append(Check(True, f"Profiles: {names}"))
    key_source = (
        f"from {keystore.ENV_VAR}" if os.environ.get(keystore.ENV_VAR)
        else "in the keychain" if keystore.stored_api_key() else None
    )  # fmt: skip
    out.append(Check(key_source is not None,
                     f"API key {key_source}" if key_source else "No API key; only local data",
                     "" if key_source else "querytale init"))  # fmt: skip
    out.append(
        Check(True, f"Time zone {cfg.tz()}" + ("" if cfg.timezone else " (from the system)"))
    )
    return out


def sync_checks(store: Store, cfg: Config, now: datetime) -> list[Check]:
    out = []
    for p in cfg.profiles.values():
        first, last = store.event_range(p.id)
        state = store.get_sync_state(p.id)
        if last is None:
            out.append(Check(False, f"{p.label}: no data yet", f"querytale sync {p.label}"))
            continue
        days = (last - first).days
        age = now - last
        stale = age > timedelta(days=1)
        text = (f"{p.label}: {store.count_events(p.id):,} lookup events over {days} days; "
                f"newest {age.total_seconds() / 3600:.0f} h ago")  # fmt: skip
        if state.last_run_at:
            text += f"; last sync {(now - state.last_run_at).total_seconds() / 3600:.0f} h ago"
        out.append(Check(not stale, text, f"querytale sync {p.label}" if stale else ""))
        if days < 30:
            out.append(Check(False, f"{p.label}: under 30 days of history, unusual sites are "
                             "warming up", f"querytale sync {p.label} --backfill 30d"))  # fmt: skip
        if state.backfill_target:
            out.append(Check(False, f"{p.label}: a deeper backfill is unfinished",
                             f"querytale sync {p.label}"))  # fmt: skip
    return out


def heartbeat_suggestions(
    store: Store, profile_id: str, services: dict[str, Service], now: datetime
) -> list[HeartbeatSuggestion]:
    """Hostnames whose lookups arrive like clockwork: background check-ins, not use."""
    rows = store.con.execute(
        """
        WITH g AS (
            SELECT domain,
                   epoch(ts - lag(ts) OVER (PARTITION BY device_id, domain ORDER BY ts)) AS gap
            FROM events
            WHERE profile_id = ? AND ts >= ? AND status <> 'blocked'
        )
        SELECT domain, count(*) + 1 AS n, median(gap) AS med,
               quantile_cont(gap, 0.75) - quantile_cont(gap, 0.25) AS iqr
        FROM g WHERE gap >= 1
        GROUP BY domain
        HAVING count(*) + 1 >= ? AND median(gap) >= ?
           AND quantile_cont(gap, 0.75) - quantile_cont(gap, 0.25) < ? * median(gap)
        ORDER BY n DESC
        """,
        [profile_id, now - LOOKBACK, HEARTBEAT_MIN_COUNT, HEARTBEAT_MIN_GAP_S, HEARTBEAT_MAX_IQR],
    ).fetchall()
    out = []
    for host, n, med, iqr in rows:
        owner = next((s for s in services.values() if s.matches(host)), None)
        if owner and owner.is_heartbeat(host):
            continue  # already known
        if owner is None and (domains.is_system(host) or domains.is_tracker(host)):
            continue  # OS and SDK check-ins never reach sessions or unusual sites
        out.append(HeartbeatSuggestion(host, int(n), float(med), float(iqr) / float(med),
                                       owner.id if owner else None))  # fmt: skip
    return out


def unclassified_roots(
    store: Store, profile_id: str, services: dict[str, Service], now: datetime, limit: int = 15
) -> list[tuple[str, int]]:
    rows = store.con.execute(
        """
        SELECT root, sum(n) AS n FROM domain_daily
        WHERE profile_id = ? AND day >= ? AND n > 0
        GROUP BY root ORDER BY n DESC LIMIT 400
        """,
        [profile_id, (now - LOOKBACK).date()],
    ).fetchall()
    labels = domains.load_labels()
    reasons = _reason_categories(store, profile_id, {r for r, _ in rows})
    out = []
    for root, n in rows:
        if domains.is_generic(root) or domains.is_tracker(root):
            continue
        if any(s.matches(root) or s.owns(root) for s in services.values()):
            continue
        if domains.classify(root, labels, reasons)[0] != "unknown":
            continue
        out.append((root, int(n)))
        if len(out) >= limit:
            break
    return out


def sessions_in_quiet(
    store: Store, cfg: Config, profile_id: str, services: dict[str, Service], now: datetime
) -> list[tuple[str, str, str]]:
    start = now - LOOKBACK
    quiet = quiet_intervals(cfg, start, now)
    if not quiet:
        return []
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(cfg.tz())
    found = analyze(store, profile_id, services, start, now, details=False)
    out = []
    for s in found.visible_sessions():
        for q in quiet:
            if s.start < q.end and s.end > q.start:
                when = s.start.replace(tzinfo=UTC).astimezone(zone).strftime("%a %b %-d %H:%M")
                out.append((q.name, f"{s.service.name} ({s.lane})", when))
                break
    return out


def run(store: Store, cfg: Config, services: dict[str, Service], label: str | None) -> Report:
    now = datetime.now(UTC).replace(tzinfo=None)
    rep = Report(checks=file_checks(cfg) + sync_checks(store, cfg, now))
    unverified = [s.id for s in services.values() if not s.verified]
    rep.checks.append(Check(True, f"{len(services)} services ({len(unverified)} not yet verified "
                            "against real logs)"))  # fmt: skip
    profiles = [cfg.profile(label)] if label else list(cfg.profiles.values())
    for p in profiles[:1]:  # suggestions for one profile keep the output readable
        if store.event_range(p.id)[1] is None:
            continue
        newest = store.event_range(p.id)[1]
        ref = min(now, newest + timedelta(minutes=1))
        rep.heartbeats = heartbeat_suggestions(store, p.id, services, ref)
        rep.unclassified = unclassified_roots(store, p.id, services, ref)
        rep.quiet_sessions = sessions_in_quiet(store, cfg, p.id, services, ref)
        rep.attribution = attribution.stats(store, p.id)
    return rep
