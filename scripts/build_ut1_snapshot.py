"""Rebuild the bundled UT1 category snapshot (run before each release).

Usage:
    curl -LO https://dsi.ut-capitole.fr/blacklists/download/blacklists.tar.gz
    python -I scripts/build_ut1_snapshot.py blacklists.tar.gz

Reads the archive without extracting it and writes src/querytale/data/ut1.tsv.gz with only
the categories a parent reviewing activity cares about. `adult` (4.6M domains) is left out
to keep the package small.
"""

from __future__ import annotations

import gzip
import ipaddress
import re
import sys
import tarfile
from datetime import date
from pathlib import Path

# UT1 category -> querytale category, in priority order (first match wins for a domain).
CATEGORIES = {
    "games": "games",
    "educational_games": "education",
    "social_networks": "social",
    "audio-video": "video",
    "chat": "chat",
    "ai": "ai",
    "dating": "dating",
    "gambling": "gambling",
    "mixed_adult": "adult",
    "tricheur": "cheating",
    "warez": "piracy",
    "vpn": "vpn",
    "residential-proxies": "proxy",
    "doh": "doh",
    "remote-control": "remote-control",
    "drogue": "drugs",
    "agressif": "aggressive",
    "manga": "manga",
    "forums": "forums",
    "radio": "audio",
    "sports": "sports",
    "celebrity": "celebrity",
    "webmail": "webmail",
    "filehosting": "filehosting",
    "translation": "translation",
    "shopping": "shopping",
    "press": "news",
    "blog": "blog",
    "publicite": "ads",
    "marketingware": "ads",
}

HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z][a-z0-9-]{1,62}$")
OUT = Path(__file__).resolve().parent.parent / "src/querytale/data/ut1.tsv.gz"


def is_domain(line: str) -> bool:
    try:
        ipaddress.ip_address(line)
        return False
    except ValueError:
        return bool(HOSTNAME.match(line))


def main(archive: str) -> None:
    lists: dict[str, set[str]] = {}
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            parts = member.name.split("/")
            if len(parts) == 3 and parts[2] == "domains" and parts[1] in CATEGORIES:
                f = tar.extractfile(member)
                assert f is not None
                lines = (raw.decode("utf-8", "replace").strip().lower() for raw in f)
                lists[parts[1]] = {line for line in lines if is_domain(line)}
    missing = set(CATEGORIES) - set(lists)
    if missing:
        sys.exit(f"Archive lacks categories: {sorted(missing)}")

    chosen: dict[str, str] = {}
    for ut1, category in CATEGORIES.items():
        for domain in lists[ut1]:
            chosen.setdefault(domain, category)
    with gzip.open(OUT, "wt", compresslevel=9) as out:
        out.write(
            f"# UT1 blacklists snapshot {date.today().isoformat()}, "
            "Universite Toulouse Capitole, CC BY-SA 4.0\n"
        )
        for domain in sorted(chosen):
            out.write(f"{domain}\t{chosen[domain]}\n")
    print(f"{len(chosen):,} domains -> {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main(sys.argv[1])
