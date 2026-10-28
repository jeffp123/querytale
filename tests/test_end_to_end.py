"""The whole live path against the fake API: init, sync, query, summary, report, download."""

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from querytale import cli, config, keystore, paths
from querytale.api import NextDNSClient
from tests.fakes import API_KEY, PROFILE_ID, FakeNextDNS, entry, synthetic_logs, to_csv


def play(start, minutes):
    return [entry(start + timedelta(minutes=m, seconds=s), "store.steampowered.com")
            for m in range(minutes) for s in (5, 20, 40)]  # fmt: skip


@pytest.fixture
def fake(monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    api = FakeNextDNS(synthetic_logs(now - timedelta(hours=5), hours=4)
                      + play(now - timedelta(hours=3), 30))  # fmt: skip
    monkeypatch.setattr(
        cli, "NextDNSClient",
        lambda key, **kw: NextDNSClient(key, transport=api.transport(), sleep=lambda s: None),
    )  # fmt: skip
    return api


def test_init_sync_query_summary_report(fake, tmp_path):
    runner = CliRunner()
    # API key, label for the one profile, then skip the optional CSV.
    result = runner.invoke(cli.app, ["init"], input=f"{API_KEY}\nAlex\n\n")
    assert result.exit_code == 0, result.output
    assert keystore.get_api_key() == API_KEY
    cfg = config.load()
    assert cfg.profiles["alex"].id == PROFILE_ID
    assert "new lookup events" in result.output
    assert API_KEY not in paths.config_path().read_text()

    # A later sync asks only for what came after the newest stored lookup.
    before = len(fake.requests)
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert len(fake.requests) - before == 1
    assert "0 new lookup events" in result.output

    result = runner.invoke(cli.app, ["query", "alex", "--last", "6h", "--match", "steampowered",
                                     "--csv"])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert len(result.stdout.strip().splitlines()) == 1 + 90

    result = runner.invoke(cli.app, ["summary", "alex", "--last", "6h"])
    assert result.exit_code == 0, result.output
    assert "Steam" in result.output and "high" in result.output

    out = tmp_path / "r.html"
    result = runner.invoke(cli.app, ["report", "alex", "--last", "6h", "--no-open",
                                     "--out", str(out)])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert out.exists() and "qt-data" in out.read_text()
    assert all(r.method == "GET" for r in fake.requests)


def test_import_download(fake, tmp_path):
    runner = CliRunner()
    keystore.set_api_key(API_KEY)
    now = datetime.now(UTC).replace(tzinfo=None)
    fake.export_csv = to_csv(synthetic_logs(now - timedelta(days=20), hours=2),
                             tmp_path / "e.csv").read_bytes()  # fmt: skip
    result = runner.invoke(cli.app, ["import", "--download", "--profile", "alex",
                                     "--profile-id", PROFILE_ID])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "148 new lookup events" in result.output
    assert not list((paths.home() / "imports").glob("*.csv"))  # temporary file removed
