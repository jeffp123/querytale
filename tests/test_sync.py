from datetime import timedelta

import pytest

from querytale.api import NextDNSClient
from querytale.sync import SyncLocked, sync_lock, sync_profile
from tests.conftest import NOW
from tests.fakes import API_KEY, FakeNextDNS, entry, synthetic_logs


def run(store, cfg, fake, **kw):
    client = NextDNSClient(API_KEY, transport=fake.transport(), sleep=lambda s: None)
    client_pages = client.iter_log_pages

    # Small pages so a few hours of data spans many requests.
    def small_pages(*a, **k):
        return client_pages(*a, **{**k, "limit": 100})

    client.iter_log_pages = small_pages
    return sync_profile(store, client, cfg, cfg.profiles["alex"], now=kw.pop("now", NOW), **kw)


def count(store):
    return store.con.execute("SELECT count(*) FROM events").fetchone()[0]


def test_first_sync_backfills_the_configured_window(store, cfg):
    old = synthetic_logs(NOW - timedelta(days=40), hours=2)
    recent = synthetic_logs(NOW - timedelta(hours=6), hours=6)
    fake = FakeNextDNS(old + recent)
    result = run(store, cfg, fake)
    assert result.added == count(store) > 0
    assert store.event_range("abc123")[0] >= NOW - timedelta(days=30)
    assert store.get_sync_state("abc123").backfilled_from == NOW - timedelta(days=30)


def test_interrupted_backfill_resumes_without_duplicates(store, cfg):
    logs = synthetic_logs(NOW - timedelta(hours=6), hours=6)
    reference = FakeNextDNS(logs)
    from querytale.store import Store

    clean = Store.memory()
    run(clean, cfg, reference)
    expected = count(clean)

    interrupted = FakeNextDNS(logs, fail_on_request=7)
    with pytest.raises(KeyboardInterrupt):
        run(store, cfg, interrupted)
    partial = count(store)
    assert 0 < partial < expected

    resumed = FakeNextDNS(logs)
    run(store, cfg, resumed, now=NOW + timedelta(minutes=1))
    assert count(store) == expected
    # It resumed near where it stopped rather than refetching everything.
    assert len(resumed.requests) < len(reference.requests)
    dupes = store.con.execute(
        "SELECT count(*) FROM (SELECT ts, domain, device_id FROM events "
        "GROUP BY ALL HAVING count(*) > 1)"
    ).fetchone()[0]
    assert dupes == 0


def test_incremental_sync_fetches_only_new_minutes(store, cfg):
    logs = synthetic_logs(NOW - timedelta(hours=6), hours=6)
    run(store, cfg, FakeNextDNS(logs))
    later = logs + [entry(NOW + timedelta(minutes=30), "new-site.com")]
    fake = FakeNextDNS(later)
    result = run(store, cfg, fake, now=NOW + timedelta(hours=1))
    assert result.added == 1
    assert len(fake.requests) == 1


def test_deeper_backfill_extends_history(store, cfg):
    logs = synthetic_logs(NOW - timedelta(days=60), hours=1) + synthetic_logs(
        NOW - timedelta(hours=2), hours=2
    )
    run(store, cfg, FakeNextDNS(logs))
    assert store.event_range("abc123")[0] > NOW - timedelta(days=30)
    run(store, cfg, FakeNextDNS(logs), backfill=timedelta(days=90))
    assert store.event_range("abc123")[0] < NOW - timedelta(days=59)
    state = store.get_sync_state("abc123")
    assert state.backfilled_from == NOW - timedelta(days=90)
    assert state.backfill_target is None


def test_domain_daily_is_built_after_sync(store, cfg):
    run(store, cfg, FakeNextDNS(synthetic_logs(NOW - timedelta(hours=3), hours=3)))
    roots = dict(
        store.con.execute("SELECT root, sum(n) FROM domain_daily GROUP BY root").fetchall()
    )
    assert roots["steamserver.net"] == 180
    assert roots["poki.com"] == 0  # blocked only


def test_lock_prevents_concurrent_syncs(tmp_path):
    lock = tmp_path / "sync.lock"
    with sync_lock(lock), pytest.raises(SyncLocked), sync_lock(lock):
        pass
    with sync_lock(lock):  # released afterwards
        pass
