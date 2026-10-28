from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from querytale.cli import app
from querytale.config import Config
from querytale.derive import rebuild_minute_service
from querytale.quiet import quiet_intervals
from querytale.services import ServiceError, load_services, parse_service
from querytale.sessions import analyze
from querytale.sync import api_row
from tests.fakes import LAPTOP, PHONE, PROFILE_ID, entry, synthetic_logs, to_csv

NIGHT = datetime(2026, 10, 27, 3, 0)  # 11 pm New York
SERVICES = load_services()


def load(store, cfg, entries, services=SERVICES):
    store.reset_staging()
    store.stage_rows([api_row(e) for e in entries])
    store.merge_staging(PROFILE_ID, cfg)
    rebuild_minute_service(store, PROFILE_ID, services, None)


def run(store, start, hours=12, services=SERVICES):
    return analyze(store, PROFILE_ID, services, start, start + timedelta(hours=hours))


def play(start: datetime, minutes: int, per_minute: int = 3, device=LAPTOP, **kw) -> list[dict]:
    """Steam activity: store pages, community and content hosts several times a minute."""
    hosts = ["store.steampowered.com", "steamcommunity.com", "cdn.steamstatic.com",
             "api.steampowered.com", "media.steamcontent.com"]  # fmt: skip
    out = []
    for m in range(minutes):
        for k in range(per_minute):
            ts = start + timedelta(minutes=m, seconds=5 + 15 * k)
            out.append(entry(ts, hosts[(m + k) % len(hosts)], device=device, **kw))
    return out


# Services ---------------------------------------------------------------------------------


def test_bundled_presets_load():
    assert {"steam", "youtube", "google-classroom"} <= set(SERVICES)
    steam = SERVICES["steam"]
    assert steam.category == "games"
    assert steam.is_heartbeat("p2p-ord1.discovery.steamserver.net")
    assert steam.matches("store.steampowered.com")
    assert not steam.matches("notsteampowered.com")
    classroom = SERVICES["google-classroom"]
    assert classroom.matches("classroom.google.com")
    assert not classroom.matches("docs.google.com")


def test_user_file_overrides_preset_by_id(tmp_path):
    (tmp_path / "steam.yaml").write_text("id: steam\nname: My Steam\ndomains: [steampowered.com]\n")
    services = load_services(tmp_path)
    assert services["steam"].name == "My Steam"
    assert "youtube" in services


@pytest.mark.parametrize(
    "data",
    [{"name": "no id"}, {"id": "x"}, {"id": "x", "regex": ["("]},
     {"id": "x", "domains": ["a.com"], "min_duration": "soon"}],
)  # fmt: skip
def test_bad_service_definitions(data):
    with pytest.raises(ServiceError):
        parse_service(data)


# minute_service ---------------------------------------------------------------------------


def test_router_lookups_milliseconds_apart_fold_into_one(store, cfg):
    t = NIGHT
    load(store, cfg, [
        entry(t, "store.steampowered.com", "A"),
        entry(t + timedelta(milliseconds=12), "store.steampowered.com", "HTTPS"),
        entry(t + timedelta(seconds=30), "store.steampowered.com", "A"),
    ])  # fmt: skip
    (n,) = store.con.execute("SELECT sum(n_allowed) FROM minute_service").fetchone()
    assert n == 2


def test_heartbeats_and_blocked_are_counted_apart(store, cfg):
    t = NIGHT
    load(store, cfg, [
        entry(t, "p2p-ord1.discovery.steamserver.net"),
        entry(t + timedelta(seconds=2), "p2p-ord1.discovery.steamserver.net", status="blocked"),
        entry(t + timedelta(seconds=4), "store.steampowered.com", status="blocked"),
        entry(t + timedelta(seconds=6), "store.steampowered.com"),
    ])  # fmt: skip
    row = store.con.execute(
        "SELECT n_allowed, n_blocked, hb_allowed, hb_blocked FROM minute_service"
    ).fetchone()
    assert row == (1, 1, 1, 1)


# Sessions ---------------------------------------------------------------------------------


def test_idle_steam_overnight_is_running_not_playing(store, cfg):
    load(store, cfg, synthetic_logs(NIGHT, hours=8))
    result = run(store, NIGHT, hours=8)
    assert result.sessions == []
    (running,) = [r for r in result.running if r.service.id == "steam"]
    assert not running.blocked
    assert running.end - running.start > timedelta(hours=7, minutes=50)


def test_blocked_lookups_never_make_a_session(store, cfg):
    blocked_play = play(NIGHT, 40, per_minute=6, status="blocked")
    blocked_beats = [
        entry(NIGHT + timedelta(minutes=5 * i), "p2p-ord1.discovery.steamserver.net",
              status="blocked")
        for i in range(24)
    ]  # fmt: skip
    load(store, cfg, blocked_play + blocked_beats)
    result = run(store, NIGHT, hours=3)
    assert result.sessions == []
    (running,) = result.running
    assert running.blocked
    assert running.end - running.start >= timedelta(minutes=115)


def test_play_session_with_heartbeat_noise(store, cfg):
    start = NIGHT + timedelta(hours=1)
    load(store, cfg, synthetic_logs(NIGHT, hours=4) + play(start, 30))
    (s,) = run(store, NIGHT, hours=4).sessions
    assert s.service.id == "steam"
    assert s.lane == "computer"
    assert (s.start, s.end) == (start, start + timedelta(minutes=30))
    assert s.confidence == "high"  # 30 min at ~9/3m, at least 2x min_rate 4
    assert s.devices == ["alex-laptop"]
    assert "p2p-ord1.discovery.steamserver.net" not in dict(s.top_hosts)


def test_short_gaps_join_long_gaps_split(store, cfg):
    a = play(NIGHT, 10)
    b = play(NIGHT + timedelta(minutes=14), 10)  # 4 min gap: joins
    c = play(NIGHT + timedelta(minutes=40), 10)  # 16 min gap: new session
    load(store, cfg, a + b + c)
    sessions = run(store, NIGHT, hours=2).sessions
    assert [(s.start - NIGHT, s.end - NIGHT) for s in sessions] == [
        (timedelta(0), timedelta(minutes=24)),
        (timedelta(minutes=40), timedelta(minutes=50)),
    ]


def test_short_or_thin_activity_is_low_confidence(store, cfg):
    load(store, cfg, play(NIGHT, 3) + play(NIGHT + timedelta(hours=1), 8, per_minute=2))
    sessions = run(store, NIGHT, hours=3).sessions
    assert [s.confidence for s in sessions] == ["low", "medium"]
    visible = run(store, NIGHT, hours=3).visible_sessions()
    assert [s.start for s in visible] == [sessions[1].start]


def test_phone_confidence_cap(store, cfg):
    capped = {"steam": parse_service(
        {"id": "steam", "category": "games", "domains": ["steampowered.com", "steamcommunity.com",
         "steamstatic.com", "steamcontent.com"], "phone_max_confidence": "medium"}
    )}  # fmt: skip
    load(store, cfg, play(NIGHT, 30, device=PHONE), capped)
    (s,) = run(store, NIGHT, hours=1, services=capped).sessions
    assert s.lane == "phone"
    assert s.confidence == "medium"


def test_games_during_school_is_an_overlap(store, cfg):
    school = [
        entry(NIGHT + timedelta(minutes=m, seconds=s), "classroom.google.com")
        for m in range(60)
        for s in (10,)
    ]
    load(store, cfg, school + play(NIGHT + timedelta(minutes=20), 25))
    result = run(store, NIGHT, hours=2)
    assert {s.service.id for s in result.sessions} == {"google-classroom", "steam"}
    (o,) = result.overlaps
    assert (o.start - NIGHT, o.end - NIGHT) == (timedelta(minutes=20), timedelta(minutes=45))
    assert result.active_time() == {"games": timedelta(minutes=25), "school": timedelta(hours=1)}


def test_sql_and_python_matching_agree(store, cfg):
    hosts = ["store.steampowered.com", "steampowered.com", "notsteampowered.com",
             "classroom.google.com", "docs.google.com", "rr3---sn-x.googlevideo.com",
             "p2p-abc.discovery.steamserver.net", "yt3.ggpht.com", "ggpht.com"]  # fmt: skip
    load(store, cfg, [entry(NIGHT + timedelta(seconds=i * 5), h) for i, h in enumerate(hosts)])
    sql = set(store.con.execute(
        "SELECT DISTINCT service FROM minute_service"
    ).fetchall())  # fmt: skip
    python = {(sid,) for sid, svc in SERVICES.items() for h in hosts if svc.matches(h)}
    assert sql == python


def test_silent_stretches(store, cfg):
    load(store, cfg, [entry(NIGHT, "a.com"), entry(NIGHT + timedelta(hours=3), "b.com")])
    gaps = run(store, NIGHT, hours=3.5).gaps  # 30 min tail: under the 1 h threshold
    assert [(g.lane, g.end - g.start) for g in gaps] == [("computer", timedelta(hours=3))]


# Quiet windows ----------------------------------------------------------------------------


def test_weekly_quiet_window_in_local_time():
    cfg = Config(
        timezone="America/New_York",
        quiet=[{"name": "fri-sat", "start": "fri 20:30", "end": "sat 17:00"}],
    )
    (q,) = quiet_intervals(cfg, datetime(2026, 10, 26), datetime(2026, 11, 2))
    assert (q.start, q.end) == (datetime(2026, 10, 31, 0, 30), datetime(2026, 10, 31, 21, 0))


def test_shabbat_window_from_location():
    cfg = Config(timezone="America/New_York", shabbat=True, latitude=40.71, longitude=-74.0)
    (q,) = quiet_intervals(cfg, datetime(2026, 10, 26), datetime(2026, 11, 2))
    # Sunset in New York on Fri Oct 30 2026 is about 5:52 pm EDT; candles 18 minutes earlier.
    assert datetime(2026, 10, 30, 21, 25) < q.start < datetime(2026, 10, 30, 21, 40)
    assert datetime(2026, 10, 31, 22, 25) < q.end < datetime(2026, 10, 31, 22, 45)


def test_shabbat_needs_a_location():
    from querytale.config import ConfigError

    with pytest.raises(ConfigError, match="location"):
        quiet_intervals(Config(shabbat=True), datetime(2026, 10, 26), datetime(2026, 11, 2))


# CLI --------------------------------------------------------------------------------------


def test_summary_calibrate_and_services_commands(tmp_path):
    runner = CliRunner()
    start = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=5)
    logs = synthetic_logs(start, hours=4) + play(start + timedelta(hours=1), 30)
    path = to_csv(logs, tmp_path / "x.csv")
    assert (
        runner.invoke(
            app, ["import", str(path), "--profile", "alex", "--profile-id", PROFILE_ID]
        ).exit_code
        == 0
    )

    result = runner.invoke(app, ["summary", "alex", "--last", "6h", "--no-sync"])
    assert result.exit_code == 0, result.output
    assert "Steam" in result.output and "high" in result.output
    assert "App running" in result.output

    result = runner.invoke(app, ["calibrate", "steam", "alex", "--last", "1d", "--no-sync"])
    assert result.exit_code == 0, result.output
    assert "100% with exactly 1 lookup" in result.output

    result = runner.invoke(app, ["services"])
    assert result.exit_code == 0 and "google-classroom" in result.output
    assert "[a-z0-9]" in result.output  # data is escaped, not read as Rich markup

    result = runner.invoke(app, ["calibrate", "nope", "alex", "--no-sync"])
    assert result.exit_code == 1


def test_quiet_and_location_survive_a_config_round_trip(tmp_path):
    from querytale import config

    cfg = Config(timezone="America/New_York", shabbat=True, latitude=40.7, longitude=-74.0,
                 quiet=[{"name": "night", "start": "sun 22:00", "end": "mon 06:30"}],
                 path=tmp_path / "config.toml")  # fmt: skip
    config.save(cfg)
    again = config.load(cfg.path)
    assert (again.shabbat, again.latitude, again.longitude, again.quiet) == (
        True, 40.7, -74.0, cfg.quiet,
    )  # fmt: skip
