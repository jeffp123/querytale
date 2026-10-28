"""Incremental API sync.

Pages arrive oldest first, and each page is staged, merged and recorded in one transaction, so
an interrupted sync (Ctrl-C, network loss) resumes from the newest stored event with nothing
duplicated or lost. The newest event is the high-water mark whichever source it came from,
which is what lets a CSV import and API syncs be mixed in any order.
"""

from __future__ import annotations

import fcntl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from querytale import derive, paths
from querytale.api import NextDNSClient
from querytale.config import Config, Profile
from querytale.store import Store, utc_naive

OVERLAP = timedelta(minutes=2)

# Called after each page with (fraction done, newest timestamp in the page).
Progress = Callable[[float, datetime], None]


class SyncLocked(RuntimeError):
    pass


@contextmanager
def sync_lock(path: Path | None = None) -> Iterator[None]:
    """Hold an exclusive lock so two syncs never write at once."""
    path = path or paths.lock_path()
    paths.ensure_private_dir(path.parent)
    with path.open("a") as f:
        paths.make_private(path)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncLocked("Another querytale sync is already running.") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@dataclass
class SyncResult:
    label: str
    pages: int = 0
    rows: int = 0
    added: int = 0
    start: datetime | None = None
    end: datetime | None = None


def parse_ts(value: str) -> datetime:
    return utc_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))


def api_row(entry: dict) -> tuple:
    """Map one API log entry to a staging row (same columns the CSV import fills)."""
    device = entry.get("device") or {}
    reasons = ",".join(r.get("id", "") for r in entry.get("reasons") or [] if isinstance(r, dict))
    return (
        parse_ts(entry["timestamp"]),
        entry.get("domain"),
        entry.get("type") or entry.get("queryType"),
        entry.get("status") or "default",
        reasons,
        entry.get("root"),
        entry.get("tracker"),
        entry.get("matchedName") or entry.get("matched_name"),
        device.get("id") or "",
        device.get("name"),
        device.get("model"),
        device.get("localIp"),
        entry.get("client") or entry.get("clientName"),
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _fetch(
    store: Store,
    client: NextDNSClient,
    cfg: Config,
    profile: Profile,
    result: SyncResult,
    *,
    start: datetime,
    end: datetime | None,
    on_page: Callable[[datetime], None],
    progress: Progress | None,
) -> None:
    span_end = end or _now()
    span = max((span_end - start).total_seconds(), 1.0)
    for page in client.iter_log_pages(profile.id, start=start, end=end):
        result.pages += 1
        if not page:
            continue
        store.con.begin()
        try:
            store.reset_staging()
            store.stage_rows([api_row(e) for e in page])
            merged = store.merge_staging(profile.id, cfg, update=True)
            on_page(merged.last_ts)
            store.con.commit()
        except BaseException:
            store.con.rollback()
            raise
        result.rows += len(page)
        result.added += merged.added
        if progress and merged.last_ts:
            progress(min((merged.last_ts - start).total_seconds() / span, 1.0), merged.last_ts)


def sync_profile(
    store: Store,
    client: NextDNSClient,
    cfg: Config,
    profile: Profile,
    *,
    backfill: timedelta | None = None,
    progress: Progress | None = None,
    now: datetime | None = None,
) -> SyncResult:
    now = now or _now()
    store.sync_profiles(cfg)
    state = store.get_sync_state(profile.id)
    oldest, newest = store.event_range(profile.id)
    result = SyncResult(profile.label)
    backfill = backfill or timedelta(days=cfg.backfill_days)

    # 1. Forward: everything since the newest stored event, or a fresh backfill window.
    if newest is None:
        start = now - backfill
        state.backfilled_from = start
    else:
        start = newest - OVERLAP
        state.backfilled_from = state.backfilled_from or oldest
    result.start = start

    def advance_high_water(ts: datetime) -> None:
        state.high_water_ts = max(filter(None, [state.high_water_ts, ts]))
        store.put_sync_state(state)

    _fetch(store, client, cfg, profile, result, start=start, end=None,
           on_page=advance_high_water, progress=progress)  # fmt: skip

    # 2. Backward: a deeper backfill than what is stored, resumable through backfill_progress.
    wanted_from = now - backfill
    if (
        state.backfill_target is None
        and state.backfilled_from
        and wanted_from < (state.backfilled_from - OVERLAP)
    ):
        state.backfill_target = wanted_from
    if state.backfill_target is not None:
        deep_start = state.backfill_progress or state.backfill_target
        deep_end = state.backfilled_from

        def advance_backfill(ts: datetime) -> None:
            state.backfill_progress = ts - OVERLAP
            store.put_sync_state(state)

        if deep_end and deep_start < deep_end:
            _fetch(store, client, cfg, profile, result, start=deep_start, end=deep_end,
                   on_page=advance_backfill, progress=progress)  # fmt: skip
        state.backfilled_from = state.backfill_target
        state.backfill_target = state.backfill_progress = None
        result.start = state.backfilled_from

    state.last_run_at = now
    store.put_sync_state(state)
    result.end = store.event_range(profile.id)[1]
    if result.added:
        derive.refresh(store, cfg, profile.id, min(result.start, start))
    return result
