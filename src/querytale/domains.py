"""What a hostname is: generic infrastructure, an ad/analytics SDK, and which category.

Generic hosts (CDN edges, cloud and app-hosting platforms) say nothing by their root: their
owner is learned instead (attribution.py), and until then each one counts as its own site, so
a browser game on its own *.supabase.co project still shows up as a rare site.
"""

from __future__ import annotations

import gzip
from functools import cache
from importlib import resources

from querytale import paths

# Matched as hostname suffixes: NextDNS reports roots like com.akadns.net or b.akamaiedge.net.
GENERIC_SUFFIXES = (
    "akadns.net", "akamai.net", "akamaiedge.net", "akamaized.net", "akamaihd.net",
    "edgekey.net", "edgesuite.net", "akamaitechnologies.com",
    "cloudfront.net", "fastly.net", "fastlylb.net", "cdn.cloudflare.net", "b-cdn.net",
    "azureedge.net", "azurefd.net", "trafficmanager.net", "amazonaws.com",
    "cdn77.org", "llnwd.net", "edgecastcdn.net",
    "supabase.co", "vercel.app", "netlify.app", "github.io", "pages.dev", "workers.dev",
    "web.app", "firebaseapp.com", "herokuapp.com", "appspot.com", "onrender.com", "fly.dev",
    "glitch.me", "replit.app", "repl.co",
)  # fmt: skip

# Ad and analytics SDK roots: a burst of one of these usually means a new app was opened.
# Used when the API's `tracker` field is missing (CSV imports) or empty.
TRACKER_SUFFIXES = (
    "app-measurement.com", "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googlesyndication.com", "googleadservices.com", "crashlytics.com",
    "firebaselogging.googleapis.com",
    "appsflyer.com", "appsflyersdk.com", "adjust.com", "adjust.io", "branch.io", "app.link",
    "kochava.com", "singular.net", "amplitude.com", "mixpanel.com", "segment.io", "segment.com",
    "braze.com", "onesignal.com", "sentry.io", "bugsnag.com", "newrelic.com", "datadoghq.com",
    "launchdarkly.com", "statsig.com", "optimizely.com", "hotjar.com", "clarity.ms",
    "scorecardresearch.com", "moatads.com", "adsrvr.org", "rubiconproject.com", "pubmatic.com",
    "criteo.com", "criteo.net", "taboola.com", "outbrain.com", "amazon-adsystem.com",
    "adnxs.com", "applovin.com", "applvn.com", "unityads.unity3d.com", "vungle.com",
    "ironsrc.com", "ironsrc.mobi", "chartboost.com", "adcolony.com", "inmobi.com",
    "smaato.net", "liftoff.io", "mintegral.com", "rayjump.com", "pangle.io", "fyber.com",
    "digitalturbine.com", "tapjoy.com", "facebook.net", "graph.facebook.com",
    "yandex.ru", "mc.yandex.ru", "bidr.io", "casalemedia.com", "openx.net", "3lift.com",
    "sharethrough.com", "teads.tv", "omtrdc.net", "demdex.net", "everesttech.net",
    "channelexco.com", "imasdk.googleapis.com",
)  # fmt: skip


# Operating-system and certificate plumbing: always present, never "a site someone visited".
SYSTEM_SUFFIXES = (
    "apple.com", "icloud.com", "icloud-content.com", "aaplimg.com", "apple-dns.net",
    "mzstatic.com", "cdn-apple.com", "apple-cloudkit.com", "push.apple.com",
    "microsoft.com", "windows.com", "windowsupdate.com", "live.com", "live.net", "office.com",
    "office.net", "microsoftonline.com", "msftconnecttest.com", "msftncsi.com", "msedge.net",
    "msn.com", "microsoftpersonalcontent.com", "onedrive.com", "sharepoint.com", "skype.com",
    "gstatic.com", "googleapis.com", "gvt1.com", "gvt2.com", "gvt3.com", "googleusercontent.com",
    "google.com", "android.com", "googlezip.net", "1e100.net",
    "lencr.org", "digicert.com", "pki.goog", "sectigo.com", "globalsign.com", "usertrust.com",
    "entrust.net", "comodoca.com", "verisign.com", "amazontrust.com", "ocsp.godaddy.com",
    "footprintdns.com", "nextdns.io", "dns.google", "cloudflare-dns.com",
)  # fmt: skip
SYSTEM = "system"


def _suffix_match(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def is_generic(host: str) -> bool:
    return _suffix_match(host, GENERIC_SUFFIXES)


def is_tracker(host: str) -> bool:
    return _suffix_match(host, TRACKER_SUFFIXES)


def is_system(host: str) -> bool:
    return _suffix_match(host, SYSTEM_SUFFIXES)


def generic_suffix(host: str) -> str | None:
    return next((s for s in GENERIC_SUFFIXES if host.endswith("." + s) or host == s), None)


def _sql_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(f"'{v}'" for v in values) + "]"


# Registered on every connection (store.py). site_key: what novelty counts as "a site".
SQL_MACROS = f"""
CREATE OR REPLACE TEMP MACRO is_generic(d) AS
    len(list_filter({_sql_list(GENERIC_SUFFIXES)}, lambda s: d = s OR ends_with(d, '.' || s))) > 0;
CREATE OR REPLACE TEMP MACRO site_key(d, r) AS
    CASE WHEN is_generic(d) THEN d ELSE coalesce(r, d) END;
"""


def suffixes(host: str) -> list[str]:
    """www.games.example.com -> [www.games.example.com, games.example.com, example.com, com]."""
    labels = host.split(".")
    return [".".join(labels[i:]) for i in range(len(labels))]


def lookup_suffix(mapping: dict[str, str], host: str) -> str | None:
    for candidate in suffixes(host):
        if candidate in mapping:
            return mapping[candidate]
    return None


# Categories -------------------------------------------------------------------------------

# NextDNS block reasons that name a category or a service.
REASON_CATEGORIES = {
    "category:gaming": "games",
    "category:video-streaming": "video",
    "category:social-networks": "social",
    "category:dating": "dating",
    "category:gambling": "gambling",
    "category:porn": "adult",
    "category:piracy": "piracy",
}
SERVICE_CATEGORIES = {
    **dict.fromkeys(
        [
            "facebook",
            "instagram",
            "tiktok",
            "snapchat",
            "twitter",
            "reddit",
            "discord",
            "pinterest",
            "tumblr",
            "vk",
            "messenger",
            "whatsapp",
            "telegram",
            "signal",
            "bereal",
            "mastodon",
            "imgur",
            "9gag",
        ],
        "social",
    ),  # fmt: skip
    **dict.fromkeys(
        [
            "youtube",
            "twitch",
            "netflix",
            "disneyplus",
            "hulu",
            "primevideo",
            "hbomax",
            "dailymotion",
            "vimeo",
            "spotify",
        ],
        "video",
    ),  # fmt: skip
    **dict.fromkeys(
        [
            "fortnite",
            "roblox",
            "minecraft",
            "steam",
            "epicgames",
            "leagueoflegends",
            "blizzard",
            "xboxlive",
            "playstation-network",
            "battle.net",
            "riotgames",
            "hearthstone",
            "clashofclans",
            "callofduty",
        ],
        "games",
    ),  # fmt: skip
    "chatgpt": "ai",
    "tinder": "dating",
}


def reason_category(reasons: list[str]) -> str | None:
    for r in reasons:
        if r in REASON_CATEGORIES:
            return REASON_CATEGORIES[r]
        if r.startswith("service:") and r[8:] in SERVICE_CATEGORIES:
            return SERVICE_CATEGORIES[r[8:]]
    return None


@cache
def ut1() -> dict[str, str]:
    """The bundled UT1 snapshot (see data/NOTICE): domain -> category."""
    data = resources.files("querytale") / "data" / "ut1.tsv.gz"
    out: dict[str, str] = {}
    with data.open("rb") as raw, gzip.open(raw, "rt") as f:
        for line in f:
            if not line.startswith("#"):
                domain, _, category = line.rstrip("\n").partition("\t")
                out[domain] = category
    return out


# User labels ------------------------------------------------------------------------------

IGNORE = "ignore"  # a label that hides a site from unusual-site lists


def labels_path():
    return paths.home() / "labels.toml"


def load_labels() -> dict[str, str]:
    import tomllib

    path = labels_path()
    if not path.exists():
        return {}
    return {str(k): str(v) for k, v in tomllib.loads(path.read_text()).get("labels", {}).items()}


def save_labels(labels: dict[str, str]) -> None:
    import tomli_w

    path = labels_path()
    paths.ensure_private_dir(path.parent)
    path.write_text(
        "# Site categories set with `querytale label`. They win over every other source.\n"
        + tomli_w.dumps({"labels": dict(sorted(labels.items()))})
    )
    paths.make_private(path)


def normalize_domain(text: str) -> str:
    """Accept pasted URLs: https://www.Example.com/path -> example.com."""
    host = text.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split(":", 1)[0].strip(".")
    host = host.removeprefix("www.")
    if not host or " " in host or "." not in host:
        raise ValueError(f"{text!r} doesn't look like a domain")
    return host


def classify(
    key: str, labels: dict[str, str], reason_categories: dict[str, str] | None = None
) -> tuple[str, str]:
    """(category, source) for a site key: your labels, built-in system list, NextDNS, UT1."""
    if category := lookup_suffix(labels, key):
        return category, "label"
    if is_system(key):
        return SYSTEM, "builtin"
    if reason_categories and key in reason_categories:
        return reason_categories[key], "nextdns"
    if category := lookup_suffix(ut1(), key):
        return category, "ut1"
    return "unknown", ""
