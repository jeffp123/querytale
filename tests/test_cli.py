import stat
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from querytale import paths
from querytale.cli import app
from querytale.timewindow import WindowError, parse_window
from tests.fakes import PROFILE_ID, synthetic_logs, to_csv

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init", "sync", "import", "query"):
        assert cmd in result.output


def test_import_then_query(tmp_path):
    now = datetime.now(UTC).replace(tzinfo=None)
    csv_path = to_csv(synthetic_logs(now - timedelta(hours=3), hours=2), tmp_path / "x.csv")
    result = runner.invoke(
        app, ["import", str(csv_path), "--profile", "alex", "--profile-id", PROFILE_ID]
    )
    assert result.exit_code == 0, result.output
    assert "new lookup events" in result.output

    result = runner.invoke(
        app, ["query", "alex", "--last", "6h", "--match", "poki", "--include-blocked", "--csv"]
    )
    assert result.exit_code == 0, result.output
    lines = result.stdout.strip().splitlines()
    assert lines[0].startswith("time,lane,device,domain")
    assert len(lines) == 1 + 4
    assert all(",blocked," in line for line in lines[1:])

    # Private by default: 0700 folder, 0600 files.
    home = paths.home()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.db_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.config_path().stat().st_mode) == 0o600


def test_unknown_profile_is_a_clean_error():
    result = runner.invoke(app, ["query", "nobody", "--no-sync"])
    assert result.exit_code == 1
    assert "No profile labeled 'nobody'" in result.output


NOW = datetime(2026, 10, 28, 16, 0)  # noon in New York


def test_window_last():
    w = parse_window(last="90m", tz="America/New_York", now=NOW)
    assert (w.start, w.end) == (NOW - timedelta(minutes=90), NOW)


def test_window_date_is_a_local_day():
    w = parse_window(on_date="2026-10-27", tz="America/New_York", now=NOW)
    assert (w.start, w.end) == (datetime(2026, 10, 27, 4), datetime(2026, 10, 28, 4))


def test_window_from_to_local():
    w = parse_window(from_="2026-10-27 15:00", to="2026-10-27 17:00", tz="America/New_York")
    assert (w.start, w.end) == (datetime(2026, 10, 27, 19), datetime(2026, 10, 27, 21))


@pytest.mark.parametrize(
    "kw", [{"last": "3x"}, {"last": "1h", "on_date": "2026-10-27"}, {"to": "2026-10-27"}]
)
def test_window_errors(kw):
    with pytest.raises(WindowError):
        parse_window(tz="UTC", **kw)
