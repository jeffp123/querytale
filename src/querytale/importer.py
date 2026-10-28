"""Optional CSV import: a full NextDNS export loaded through the same normalization as the API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from querytale import derive
from querytale.config import Config, Profile
from querytale.store import Store


@dataclass
class ImportResult:
    rows: int
    added: int
    first_ts: datetime | None
    last_ts: datetime | None


def import_csv(store: Store, cfg: Config, profile: Profile, path: Path) -> ImportResult:
    store.sync_profiles(cfg)
    store.con.begin()
    try:
        store.reset_staging()
        rows = store.stage_csv(path)
        merged = store.merge_staging(profile.id, cfg, update=False)
        store.reset_staging()  # free the staged rows

        # The export counts as backfilled history; the next sync starts after its newest row.
        state = store.get_sync_state(profile.id)
        oldest, newest = store.event_range(profile.id)
        if oldest and (state.backfilled_from is None or oldest < state.backfilled_from):
            state.backfilled_from = oldest
        if newest and (state.high_water_ts is None or newest > state.high_water_ts):
            state.high_water_ts = newest
        store.put_sync_state(state)
        store.con.commit()
    except BaseException:
        store.con.rollback()
        raise
    if merged.added:
        derive.refresh(store, cfg, profile.id, merged.first_ts)
    return ImportResult(rows, merged.added, merged.first_ts, merged.last_ts)
