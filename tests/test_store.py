from datetime import date, datetime

from querytale.sync import api_row
from tests.fakes import LAPTOP, PHONE, PROFILE_ID, entry, triplet

T = datetime(2026, 10, 28, 3, 30, 0)  # 11:30 pm Oct 27 in New York


def load(store, cfg, entries):
    store.reset_staging()
    store.stage_rows([api_row(e) for e in entries])
    return store.merge_staging(PROFILE_ID, cfg)


def events(store):
    return store.con.execute(
        "SELECT domain, qtypes, status, reasons, device_id, lane FROM events ORDER BY ts, domain"
    ).fetchall()


def test_triplet_collapses_into_one_event(store, cfg):
    result = load(store, cfg, triplet(T, "classroom.google.com"))
    assert result.added == 1
    assert events(store) == [
        ("classroom.google.com", ["A", "AAAA", "HTTPS"], "default", [], "L4PT0", "computer")
    ]


def test_same_time_different_device_stays_separate(store, cfg):
    load(store, cfg, [entry(T, "x.com"), entry(T, "x.com", device=PHONE)])
    assert sorted((r[4], r[5]) for r in events(store)) == [
        ("L4PT0", "computer"),
        ("PH0NE", "phone"),
    ]


def test_blocked_status_and_reasons(store, cfg):
    load(store, cfg, [
        entry(T, "poki.com", "A", status="blocked", reasons=["category:gaming"]),
        entry(T, "poki.com", "AAAA", status="blocked",
              reasons=["category:gaming", "blocklist:hagezi-multi-normal"]),
    ])  # fmt: skip
    (row,) = events(store)
    assert row[2] == "blocked"
    assert row[3] == ["blocklist:hagezi-multi-normal", "category:gaming"]


def test_split_triplet_merges_query_types(store, cfg):
    t = triplet(T, "a.com")
    load(store, cfg, t[:1])
    second = load(store, cfg, t[1:])
    assert second.added == 0
    assert events(store)[0][1] == ["A", "AAAA", "HTTPS"]


def test_lane_overrides_and_relane(store, cfg):
    load(store, cfg, [entry(T, "a.com", device=LAPTOP)])
    assert events(store)[0][5] == "computer"
    cfg.phone_devices = ["alex-laptop"]
    assert store.relane_if_changed(cfg)
    assert events(store)[0][5] == "phone"
    assert not store.relane_if_changed(cfg)


def test_domain_daily_uses_local_day_and_splits_blocked(store, cfg):
    load(store, cfg, [
        entry(T, "www.poki.com"),
        entry(T.replace(minute=40), "poki.com", status="blocked"),
        entry(T.replace(hour=5), "poki.com"),  # 1 am Oct 28 local
    ])  # fmt: skip
    store.rebuild_domain_daily(PROFILE_ID, None, cfg.tz())
    sql = "SELECT day, root, n, n_blocked FROM domain_daily ORDER BY day"
    rows = store.con.execute(sql).fetchall()
    assert rows == [(date(2026, 10, 27), "poki.com", 1, 1), (date(2026, 10, 28), "poki.com", 1, 0)]


def test_query_hides_blocked_by_default(store, cfg):
    load(store, cfg, [entry(T, "poki.com", status="blocked"), entry(T, "crazygames.com")])
    hits = store.query_events(PROFILE_ID, datetime(2026, 10, 28), datetime(2026, 10, 29),
                              match="poki|crazy")  # fmt: skip
    assert [h[3] for h in hits] == ["crazygames.com"]
    hits = store.query_events(PROFILE_ID, datetime(2026, 10, 28), datetime(2026, 10, 29),
                              match="poki|crazy", include_blocked=True)  # fmt: skip
    assert len(hits) == 2
