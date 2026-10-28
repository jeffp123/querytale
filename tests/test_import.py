from datetime import timedelta

from querytale.api import NextDNSClient
from querytale.importer import import_csv
from querytale.store import Store
from querytale.sync import api_row, sync_profile
from tests.conftest import NOW
from tests.fakes import API_KEY, PROFILE_ID, FakeNextDNS, entry, synthetic_logs, to_csv

COLS = "ts, domain, root, qtypes, status, reasons, device_id, device_name, device_model, lane"


def test_csv_and_api_normalize_identically(store, cfg, tmp_path):
    logs = synthetic_logs(NOW - timedelta(hours=2), hours=2)
    import_csv(store, cfg, cfg.profiles["alex"], to_csv(logs, tmp_path / "export.csv"))

    other = Store.memory()
    other.reset_staging()
    other.stage_rows([api_row(e) for e in logs])
    other.merge_staging(PROFILE_ID, cfg)

    sql = f"SELECT {COLS} FROM events ORDER BY ts, domain, device_id"
    assert store.con.execute(sql).fetchall() == other.con.execute(sql).fetchall()


def test_reimport_adds_nothing(store, cfg, tmp_path):
    path = to_csv(synthetic_logs(NOW - timedelta(hours=2), hours=2), tmp_path / "export.csv")
    first = import_csv(store, cfg, cfg.profiles["alex"], path)
    assert first.rows == 3 * 120 + 3 * 24 + 4
    assert first.added == 120 + 24 + 4
    again = import_csv(store, cfg, cfg.profiles["alex"], path)
    assert again.added == 0


def test_sync_after_import_adds_only_newer_rows(store, cfg, tmp_path):
    exported = synthetic_logs(NOW - timedelta(days=40), hours=24 * 40 - 1)
    import_csv(store, cfg, cfg.profiles["alex"], to_csv(exported, tmp_path / "export.csv"))
    before = store.con.execute("SELECT count(*) FROM events").fetchone()[0]

    newer = [entry(NOW - timedelta(minutes=20), "crazygames.com"),
             entry(NOW - timedelta(minutes=10), "poki.com")]  # fmt: skip
    fake = FakeNextDNS(exported + newer)
    client = NextDNSClient(API_KEY, transport=fake.transport(), sleep=lambda s: None)
    result = sync_profile(store, client, cfg, cfg.profiles["alex"], now=NOW)

    assert result.added == 2
    assert store.con.execute("SELECT count(*) FROM events").fetchone()[0] == before + 2
    # The export covered the backfill window, so the sync asked only for the last minutes.
    assert len(fake.requests) == 1
    assert fake.requests[0].url.params["from"] >= "2026-10-28T10:"


def test_import_before_sync_or_after_gives_same_result(cfg, tmp_path):
    logs = synthetic_logs(NOW - timedelta(hours=4), hours=4)
    path = to_csv(logs[: len(logs) // 2], tmp_path / "half.csv")

    def via(order):
        s = Store.memory()
        client = NextDNSClient(API_KEY, transport=FakeNextDNS(logs).transport())
        for step in order:
            if step == "import":
                import_csv(s, cfg, cfg.profiles["alex"], path)
            else:
                sync_profile(s, client, cfg, cfg.profiles["alex"], now=NOW)
        return s.con.execute(f"SELECT {COLS} FROM events ORDER BY ALL").fetchall()

    assert via(["import", "sync"]) == via(["sync", "import"])
