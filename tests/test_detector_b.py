from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from querytale import attribution, derive, domains
from querytale.cli import app
from querytale.novelty import bursts_from_times, find_unusual
from querytale.services import load_services
from querytale.sync import api_row
from tests.fakes import PHONE, PROFILE_ID, entry

DAY = datetime(2026, 10, 27, 19, 0)  # 3 pm New York
SERVICES = load_services()


def history(days: int = 35) -> list[dict]:
    """Ordinary background: the same few sites every day, so they are never rare."""
    out = []
    for d in range(1, days + 1):
        t = DAY - timedelta(days=d)
        for i, host in enumerate(["www.apple.com", "school.example.org", "news.example.net"]):
            out.append(entry(t + timedelta(minutes=i), host))
    return out


def spaced(host: str, start: datetime, n: int, every: timedelta = timedelta(seconds=70), **kw):
    return [entry(start + i * every, host, **kw) for i in range(n)]


def load(store, cfg, entries):
    store.reset_staging()
    store.stage_rows([api_row(e) for e in entries])
    store.merge_staging(PROFILE_ID, cfg)
    derive.refresh(store, cfg, PROFILE_ID, None)


def unusual(store, cfg, hours=3, labels=None):
    return find_unusual(store, PROFILE_ID, SERVICES, DAY - timedelta(hours=1),
                        DAY + timedelta(hours=hours), cfg.tz(), labels or {})  # fmt: skip


def test_three_game_portals_in_40_minutes_make_one_episode(store, cfg):
    portals = (
        spaced("www.friv.com", DAY, 10)
        + spaced("www.kizi.com", DAY + timedelta(minutes=14), 10)
        + spaced("www.miniclip.com", DAY + timedelta(minutes=28), 10)
    )
    load(store, cfg, history() + portals)
    found = unusual(store, cfg)
    assert not found.warming_up
    (episode,) = found.episodes
    assert [b.key for b in episode.bursts] == ["friv.com", "kizi.com", "miniclip.com"]
    assert episode.title() == "3 rare sites, mostly games"
    assert episode.end - episode.start < timedelta(minutes=40)
    assert all(b.category_source == "ut1" for b in episode.bursts)
    assert found.app_activity == []


def test_tracker_only_bursts_are_collapsed(store, cfg):
    sdk = spaced("launches.appsflyersdk.com", DAY, 10)
    tagged = spaced("events.brand-new-analytics.io", DAY + timedelta(hours=1), 10,
                    tracker="new_analytics")  # fmt: skip
    load(store, cfg, history() + sdk + tagged)
    found = unusual(store, cfg)
    assert found.episodes == []
    assert {b.key for b in found.app_activity} == {"appsflyersdk.com", "brand-new-analytics.io"}


def test_burst_needs_eight_lookups_in_thirty_minutes(store, cfg):
    load(store, cfg, history() + spaced("seven-sites.com", DAY, 7)
         + spaced("eight-sites.com", DAY, 8)
         + spaced("slow-sites.com", DAY, 8, every=timedelta(minutes=5)))  # fmt: skip
    keys = [b.key for e in unusual(store, cfg).episodes for b in e.bursts]
    assert keys == ["eight-sites.com"]


def test_sites_seen_on_more_than_three_prior_days_are_not_rare(store, cfg):
    regular = [entry(DAY - timedelta(days=d), "habit.example.com") for d in (1, 3, 5, 7)]
    load(store, cfg, history() + regular + spaced("habit.example.com", DAY, 10))
    assert unusual(store, cfg).episodes == []


def test_blocked_and_service_lookups_are_never_unusual(store, cfg):
    load(store, cfg, history()
         + spaced("crazygames.com", DAY, 12, status="blocked", reasons=["category:gaming"])
         + spaced("www.youtube.com", DAY, 12))  # fmt: skip
    assert unusual(store, cfg).episodes == []


def test_one_off_generic_host_counts_as_its_own_site(store, cfg):
    load(store, cfg, history() + spaced("abcxyz.supabase.co", DAY, 10, device=PHONE))
    (episode,) = unusual(store, cfg).episodes
    assert episode.lane == "phone"
    assert episode.bursts[0].key == "abcxyz.supabase.co"


def test_episodes_split_on_gaps_and_lanes(store, cfg):
    load(store, cfg, history()
         + spaced("site-one.com", DAY, 10)
         + spaced("site-two.com", DAY + timedelta(hours=1), 10)
         + spaced("site-three.com", DAY, 10, device=PHONE))  # fmt: skip
    episodes = unusual(store, cfg).episodes
    assert sorted((e.lane, len(e.bursts)) for e in episodes) == [
        ("computer", 1), ("computer", 1), ("phone", 1),
    ]  # fmt: skip


def test_category_sources_and_ignore(store, cfg):
    load(store, cfg, history()
         + [entry(DAY - timedelta(days=60), "mystery-site.com", status="blocked",
                  reasons=["category:gambling"])]
         + spaced("mystery-site.com", DAY, 10)
         + spaced("y8.com", DAY + timedelta(hours=1), 10)
         + spaced("boring-site.com", DAY + timedelta(hours=2), 10))  # fmt: skip
    found = unusual(store, cfg, labels={"y8.com": "school", "boring-site.com": "ignore"})
    got = {b.key: (b.category, b.category_source) for e in found.episodes for b in e.bursts}
    assert got == {
        "mystery-site.com": ("gambling", "nextdns"),
        "y8.com": ("school", "label"),
    }


def test_warming_up_without_30_days_of_history(store, cfg):
    load(store, cfg, history(days=5) + spaced("y8.com", DAY, 10))
    assert unusual(store, cfg).warming_up


def test_bursts_from_times_marks_whole_clusters():
    t = [DAY + timedelta(minutes=m) for m in [0, 1, 2, 3, 4, 5, 6, 7, 50, 51, 120]]
    assert bursts_from_times(t) == [(0, 7)]


# Attribution ------------------------------------------------------------------------------


def test_name_in_hostname():
    known = {"nytimes.com", "msn.com", "apple.com"}
    index = {"nytimes": "nytimes.com", "msn": "msn.com", "apple": "apple.com"}
    assert attribution.name_owner("nytimes.map.fastly.net", index, known) == "nytimes.com"
    assert attribution.name_owner("img-s-msn-com.akamaized.net", index, known) == "msn.com"
    assert attribution.name_owner("gw.ls-apple.com.akadns.net", index, known) == "apple.com"
    assert attribution.name_owner("e1234.dsce9.akamaiedge.net", index, known) is None
    assert attribution.name_owner("cdn.global.fastly.net", index, known) is None
    assert attribution.name_owner("www.nytimes.com", index, known) is None  # not generic


def test_cooccurrence_learns_a_cloudfront_owner(store, cfg):
    events = history()
    # Background noise on the same device, so chance co-occurrence is low.
    for i in range(400):
        events.append(entry(DAY - timedelta(hours=40) + timedelta(minutes=3 * i), f"x{i % 37}.org"))
    for i in range(12):
        t = DAY - timedelta(hours=30) + timedelta(hours=i)
        events.append(entry(t, "www.ownersite.com"))
        events.append(entry(t + timedelta(seconds=1), "d1a2b3c4.cloudfront.net"))
    for i in range(12):  # appears with nothing in particular around it
        events.append(entry(DAY - timedelta(hours=50) + timedelta(hours=i, seconds=30),
                            "d9z9z9z9.cloudfront.net"))  # fmt: skip
    events.append(entry(DAY, "rare1.cloudfront.net"))
    load(store, cfg, events)
    owners = attribution.owners(store, PROFILE_ID)
    assert owners == {"d1a2b3c4.cloudfront.net": "ownersite.com"}
    st = attribution.stats(store, PROFILE_ID)
    assert st.by_method == {"cooccurrence": 12, "unattributed": 12, "too_rare": 1}


def test_attributed_host_counts_toward_its_owners_service(store, cfg):
    events = history() + [entry(DAY - timedelta(days=2), "www.youtube.com")]
    events += spaced("youtube.map.fastly.net", DAY, 30, every=timedelta(seconds=20))
    load(store, cfg, events)
    assert attribution.owners(store, PROFILE_ID)["youtube.map.fastly.net"] == "youtube.com"
    (n,) = store.con.execute(
        "SELECT sum(n_allowed) FROM minute_service WHERE service = 'youtube'"
    ).fetchone()
    assert n == 31


# Labels -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [("https://www.CrazyGames.com/game/x", "crazygames.com"), ("poki.com.", "poki.com"),
     ("games.example.org:443", "games.example.org")],
)  # fmt: skip
def test_normalize_domain(raw, clean):
    assert domains.normalize_domain(raw) == clean


def test_label_commands():
    runner = CliRunner()
    assert runner.invoke(app, ["label", "https://www.poki.com/", "games"]).exit_code == 0
    assert domains.load_labels() == {"poki.com": "games"}
    result = runner.invoke(app, ["labels"])
    assert "poki.com" in result.output
    assert runner.invoke(app, ["label", "poki.com", "--remove"]).exit_code == 0
    assert domains.load_labels() == {}
    assert runner.invoke(app, ["label", "poki.com", "--remove"]).exit_code == 1


def test_ut1_snapshot_is_bundled():
    assert domains.ut1()["crazygames.com"] == "games"
    assert domains.classify("www.instagram.com", {}) == ("social", "ut1")
