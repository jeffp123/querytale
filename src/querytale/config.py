"""config.toml: tracked profiles, time zone, lane overrides and sync settings.

The API key is never stored here; see keystore.py.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from querytale import paths


class ConfigError(RuntimeError):
    pass


@dataclass
class Profile:
    label: str
    id: str
    nextdns_name: str = ""


@dataclass
class Config:
    profiles: dict[str, Profile] = field(default_factory=dict)
    timezone: str | None = None
    # Device ids or names forced into a lane, overriding the client/model heuristic.
    phone_devices: list[str] = field(default_factory=list)
    computer_devices: list[str] = field(default_factory=list)
    backfill_days: int = 30
    # Known-quiet windows used for calibration: [{name, start: "fri 20:30", end: "sat 17:00"}].
    quiet: list[dict] = field(default_factory=list)
    shabbat: bool = False  # built-in candle lighting to nightfall window; needs a location
    latitude: float | None = None
    longitude: float | None = None
    path: Path | None = None

    def profile(self, label: str) -> Profile:
        try:
            return self.profiles[label]
        except KeyError:
            known = ", ".join(sorted(self.profiles)) or "none yet"
            raise ConfigError(
                f"No profile labeled {label!r} (configured: {known}). Run `querytale init`."
            ) from None

    def tz(self) -> str:
        return self.timezone or system_timezone()


def system_timezone() -> str:
    """IANA name of the local time zone, without extra dependencies."""
    if tz := os.environ.get("TZ"):
        return tz.lstrip(":")
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return "UTC"
    marker = "zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else "UTC"


def load(path: Path | None = None) -> Config:
    path = path or paths.config_path()
    if not path.exists():
        return Config(path=path)
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    profiles = {
        label: Profile(label=label, id=str(p["id"]), nextdns_name=p.get("nextdns_name", ""))
        for label, p in raw.get("profiles", {}).items()
    }
    lanes = raw.get("lanes", {})
    location = raw.get("location", {})
    return Config(
        profiles=profiles,
        timezone=raw.get("timezone"),
        phone_devices=list(lanes.get("phone", [])),
        computer_devices=list(lanes.get("computer", [])),
        backfill_days=int(raw.get("sync", {}).get("backfill_days", 30)),
        quiet=list(raw.get("quiet", [])),
        shabbat=bool(raw.get("shabbat", False)),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        path=path,
    )


def save(cfg: Config) -> Path:
    path = cfg.path or paths.config_path()
    paths.ensure_private_dir(path.parent)
    doc: dict = {}
    if cfg.timezone:
        doc["timezone"] = cfg.timezone
    if cfg.shabbat:
        doc["shabbat"] = True
    if cfg.latitude is not None and cfg.longitude is not None:
        doc["location"] = {"latitude": cfg.latitude, "longitude": cfg.longitude}
    doc["sync"] = {"backfill_days": cfg.backfill_days}
    doc["lanes"] = {"phone": cfg.phone_devices, "computer": cfg.computer_devices}
    if cfg.quiet:
        doc["quiet"] = cfg.quiet
    doc["profiles"] = {
        p.label: {"id": p.id, "nextdns_name": p.nextdns_name} for p in cfg.profiles.values()
    }
    path.write_text(tomli_w.dumps(doc))
    paths.make_private(path)
    cfg.path = path
    return path
