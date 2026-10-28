import json
import re
import stat
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from querytale import derive, paths, report
from querytale.cli import app
from querytale.novelty import find_unusual
from querytale.services import load_services
from querytale.sessions import analyze
from querytale.sync import api_row
from tests.fakes import PROFILE_ID, entry, synthetic_logs, to_csv

START = datetime(2026, 10, 27, 12, 0)
SERVICES = load_services()
EVIL = "x</script><script>alert(1)</script>.evil-games.com"


def play(start, minutes, host="store.steampowered.com"):
    return [entry(start + timedelta(minutes=m, seconds=s), host)
            for m in range(minutes) for s in (5, 20, 40)]  # fmt: skip


def build(store, cfg, entries, hours=8):
    store.reset_staging()
    store.stage_rows([api_row(e) for e in entries])
    store.merge_staging(PROFILE_ID, cfg)
    derive.refresh(store, cfg, PROFILE_ID, None)
    end = START + timedelta(hours=hours)
    ctx = report.ReportContext("alex", "test window", START, end, cfg.tz(), None)
    a = analyze(store, PROFILE_ID, SERVICES, START, end)
    u = find_unusual(store, PROFILE_ID, SERVICES, START, end, cfg.tz(), {})
    return report.build_data(store, PROFILE_ID, SERVICES, ctx, a, u)


def embedded(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="qt-data">(.*?)</script>', html, re.S)
    assert m
    return json.loads(m.group(1))


def test_report_is_self_contained_and_locked_down(store, cfg):
    data = build(store, cfg, synthetic_logs(START, hours=6) + play(START + timedelta(hours=1), 30))
    html = report.render_html(data)
    assert "default-src 'none'" in html and "connect-src 'none'" in html
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|@import", html)
    assert "/*__ECHARTS__*/" not in html and "__DATA__" not in html
    assert embedded(html)["meta"]["label"] == "alex"
    assert len(html) < 3_000_000


def test_hostile_hostnames_cannot_break_out_of_the_data_script(store, cfg):
    logs = synthetic_logs(START, hours=3)
    logs += [entry(START + timedelta(minutes=30, seconds=20 * i), EVIL) for i in range(12)]
    html = report.render_html(build(store, cfg, logs))
    assert "<script>alert(1)" not in html
    data = embedded(html)
    keys = [b["key"] for e in data["episodes"] for b in e["bursts"]]
    assert keys == ["evil-games.com"]  # the fake root of the hostile host
    hosts = [h for e in data["episodes"] for b in e["bursts"] for h, _ in b["hosts"]]
    assert hosts == [EVIL]  # survives intact as data


def test_sessions_rows_and_raw_events(store, cfg):
    data = build(store, cfg, synthetic_logs(START, hours=6) + play(START + timedelta(hours=1), 30))
    (s,) = data["sessions"]
    assert s["row"] == "steam|computer" and s["confidence"] == "high"
    assert len(s["events"]) == 90
    assert "querytale query alex --from" in s["query"]
    assert {r["id"] for r in data["rows"]} == {"steam|computer"}
    assert data["running"] and not data["running"][0]["blocked"]
    assert data["active"] == {"games": 30}


def test_frequency_counts_heartbeats_apart(store, cfg):
    data = build(store, cfg, synthetic_logs(START, hours=2), hours=2)
    f = data["frequency"]
    assert f["minutes"] == 1 and len(f["buckets"]) == 120
    computer = f["series"]["computer"]
    assert sum(computer["heartbeat"]) == 120  # one Steam heartbeat a minute
    assert sum(computer["games"]) == 0  # heartbeats are not games activity
    assert sum(f["series"]["phone"]["other"]) == 24  # the phone's iCloud check-ins


def test_large_windows_embed_aggregates_only(store, cfg, monkeypatch):
    monkeypatch.setattr(report, "MAX_EMBED_EVENTS", 10)
    data = build(store, cfg, synthetic_logs(START, hours=6) + play(START + timedelta(hours=1), 30))
    assert data["meta"]["embedRaw"] is False
    assert data["sessions"][0]["events"] is None


def test_bucket_sizes():
    assert report.bucket_minutes(timedelta(hours=24)) == 1
    assert report.bucket_minutes(timedelta(days=2)) == 5
    assert report.bucket_minutes(timedelta(days=7)) == 15


def test_heatmap_only_for_a_week_or_more(store, cfg):
    data = build(store, cfg, synthetic_logs(START, hours=2))
    assert data["heatmap"] is None


def test_report_command_writes_a_private_file(tmp_path):
    runner = CliRunner()
    now = datetime.now(UTC).replace(tzinfo=None)
    path = to_csv(synthetic_logs(now - timedelta(hours=3), hours=2), tmp_path / "x.csv")
    runner.invoke(app, ["import", str(path), "--profile", "alex", "--profile-id", PROFILE_ID])
    result = runner.invoke(app, ["report", "alex", "--last", "6h", "--no-sync", "--no-open"])
    assert result.exit_code == 0, result.output
    (written,) = (paths.home() / "reports" / "alex").glob("*.html")
    assert written.name.endswith("_last-6h.html")
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.parent.stat().st_mode) == 0o700
    # Never overwritten: a second run in the same second gets its own file.
    runner.invoke(app, ["report", "alex", "--last", "6h", "--no-sync", "--no-open"])
    assert len(list(written.parent.glob("*.html"))) == 2
