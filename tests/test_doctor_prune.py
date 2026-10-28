import os
from datetime import UTC, datetime, timedelta
from importlib import resources

import yaml
from typer.testing import CliRunner

from querytale import derive, doctor, domains, paths
from querytale.cli import app
from querytale.services import load_services, parse_service
from querytale.sessions import analyze
from querytale.store import Store
from querytale.sync import api_row
from tests.fakes import PROFILE_ID, entry, synthetic_logs, to_csv

NOW = datetime(2026, 10, 28, 12, 0)


def load(store, cfg, entries):
    store.reset_staging()
    store.stage_rows([api_row(e) for e in entries])
    store.merge_staging(PROFILE_ID, cfg)
    derive.refresh(store, cfg, PROFILE_ID, None)


def test_every_preset_parses_and_matches_its_filename():
    folder = resources.files("querytale") / "presets"
    files = [f for f in folder.iterdir() if f.name.endswith(".yaml")]
    assert len(files) >= 37
    for f in files:
        data = yaml.safe_load(f.read_text())
        assert f.name == f"{data['id']}.yaml"
        parse_service(data, f.name)


def test_presets_do_not_overlap():
    """One hostname, one service: overlapping patterns would double-count a lookup."""
    services = load_services()
    for host in ["www.youtube.com", "classroom.google.com", "docs.google.com", "poki.com",
                 "catalog.gamepass.com", "www.instagram.com", "chatgpt.com", "gemini.google.com",
                 "store.steampowered.com", "apps.overwolf.com", "www.roblox.com"]:  # fmt: skip
        owners = [s.id for s in services.values() if s.matches(host)]
        assert len(owners) == 1, (host, owners)


def test_indirect_signals_stay_low_confidence(store, cfg):
    ads = [entry(NOW + timedelta(minutes=m, seconds=s), "d.applovin.com")
           for m in range(40) for s in (1, 15, 30, 45)]  # fmt: skip
    load(store, cfg, ads)
    (s,) = analyze(store, PROFILE_ID, load_services(), NOW, NOW + timedelta(hours=1)).sessions
    assert s.service.id == "mobile-game-ads" and s.confidence == "low"


def test_system_hosts_are_never_unusual():
    assert domains.classify("gateway.icloud.com", {}) == ("system", "builtin")
    assert domains.classify("icloud.com", {"icloud.com": "school"}) == ("school", "label")


def test_heartbeat_suggestions(store, cfg):
    beats = [entry(NOW - timedelta(hours=10) + timedelta(minutes=2 * i), "ping.someapp.io")
             for i in range(60)]  # fmt: skip
    noise = [entry(NOW - timedelta(hours=10) + timedelta(minutes=i * i % 97), "random.site.org")
             for i in range(60)]  # fmt: skip
    load(store, cfg, synthetic_logs(NOW - timedelta(hours=10), hours=8) + beats + noise)
    found = doctor.heartbeat_suggestions(store, PROFILE_ID, load_services(), NOW)
    hosts = {h.host: h for h in found}
    assert "ping.someapp.io" in hosts and hosts["ping.someapp.io"].median_gap_s == 120
    assert "random.site.org" not in hosts
    assert "p2p-ord1.discovery.steamserver.net" not in hosts  # already a Steam heartbeat
    assert "gateway.icloud.com" not in hosts  # system


def test_doctor_command_runs(tmp_path):
    runner = CliRunner()
    now = datetime.now(UTC).replace(tzinfo=None)
    path = to_csv(synthetic_logs(now - timedelta(hours=3), hours=2), tmp_path / "x.csv")
    runner.invoke(app, ["import", str(path), "--profile", "alex", "--profile-id", PROFILE_ID])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Profiles: alex" in result.output
    assert "under 30 days of history" in result.output


def test_prune_deletes_old_data_and_reports(tmp_path):
    runner = CliRunner()
    now = datetime.now(UTC).replace(tzinfo=None)
    old = synthetic_logs(now - timedelta(days=500), hours=1)
    new = synthetic_logs(now - timedelta(hours=2), hours=1)
    path = to_csv(old + new, tmp_path / "x.csv")
    runner.invoke(app, ["import", str(path), "--profile", "alex", "--profile-id", PROFILE_ID])
    folder = paths.ensure_private_dir(paths.home() / "reports" / "alex")
    stale, fresh = folder / "old.html", folder / "new.html"
    stale.write_text("x")
    fresh.write_text("x")
    ancient = (now - timedelta(days=450)).replace(tzinfo=UTC).timestamp()
    os.utime(stale, (ancient, ancient))

    result = runner.invoke(app, ["prune", "--dry-run"])
    assert "lookup events and 1 reports" in result.output
    assert stale.exists()

    result = runner.invoke(app, ["prune", "--yes"])
    assert result.exit_code == 0, result.output
    assert not stale.exists() and fresh.exists()
    with Store.open() as store:
        first, _ = store.event_range(PROFILE_ID)
        assert first > now - timedelta(days=1)
        (n,) = store.con.execute(
            "SELECT count(*) FROM minute_service WHERE minute < ?", [now - timedelta(days=400)]
        ).fetchone()
        assert n == 0
