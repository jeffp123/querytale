"""Service definitions: one small YAML file per service.

Bundled presets ship in querytale/presets; YAML files in <home>/services override them by id.
Patterns always match the full hostname: `domains` covers a domain and all its subdomains,
`hosts` exact hostnames, `regex` anything else. `heartbeat` patterns mark background check-ins
that mean "the app is running", not "someone is using it".
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import resources
from pathlib import Path

import yaml

from querytale import paths
from querytale.timewindow import WindowError, parse_duration

CONFIDENCES = ("low", "medium", "high")


class ServiceError(ValueError):
    pass


@dataclass(frozen=True)
class Pattern:
    kind: str  # domain | host | regex
    value: str

    def matches(self, host: str) -> bool:
        if self.kind == "domain":
            return host == self.value or host.endswith("." + self.value)
        if self.kind == "host":
            return host == self.value
        return re.search(self.value, host) is not None


@dataclass
class Service:
    id: str
    name: str
    category: str
    patterns: list[Pattern]
    heartbeat: list[Pattern] = field(default_factory=list)
    min_rate: int = 4
    min_duration: timedelta = timedelta(minutes=5)
    max_gap: timedelta = timedelta(minutes=5)
    verified: bool = False
    # Highest confidence a phone session can reach (DNS can't tell refresh from use there).
    phone_max_confidence: str = "high"
    max_confidence: str = "high"  # e.g. "low" for indirect signals such as ad SDKs
    source: str = ""

    def matches(self, host: str) -> bool:
        return any(p.matches(host) for p in self.patterns) or self.is_heartbeat(host)

    def owns(self, root: str) -> bool:
        """Whether a learned generic-host owner belongs here (domain patterns only)."""
        return any(p.kind == "domain" and p.matches(root) for p in self.patterns)

    def is_heartbeat(self, host: str) -> bool:
        return any(p.matches(host) for p in self.heartbeat)

    def describe_patterns(self) -> list[str]:
        return [f"{p.kind}: {p.value}" for p in self.patterns]


def _patterns(items: object, where: str) -> list[Pattern]:
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append(Pattern("domain", item.lower()))
        elif isinstance(item, dict) and len(item) == 1:
            kind, value = next(iter(item.items()))
            if kind not in ("domain", "host", "regex"):
                raise ServiceError(f"{where}: unknown pattern kind {kind!r}")
            out.append(Pattern(kind, str(value)))
        else:
            raise ServiceError(f"{where}: can't read pattern {item!r}")
    for p in out:
        if p.kind == "regex":
            try:
                re.compile(p.value)
            except re.error as e:
                raise ServiceError(f"{where}: bad regex {p.value!r}: {e}") from None
    return out


def parse_service(data: dict, source: str = "") -> Service:
    if not isinstance(data, dict) or "id" not in data:
        raise ServiceError(f"{source}: a service needs at least an id")
    sid = str(data["id"])
    where = source or sid
    patterns = (
        [Pattern("domain", str(d).lower()) for d in data.get("domains") or []]
        + [Pattern("host", str(h).lower()) for h in data.get("hosts") or []]
        + [Pattern("regex", str(r)) for r in data.get("regex") or []]
    )
    patterns = _patterns([{p.kind: p.value} for p in patterns], where)
    heartbeat = _patterns(data.get("heartbeat"), where)
    if not patterns and not heartbeat:
        raise ServiceError(f"{where}: no domains, hosts or regex")
    cap = data.get("phone_max_confidence", "high")
    overall = data.get("max_confidence", "high")
    if cap not in CONFIDENCES or overall not in CONFIDENCES:
        raise ServiceError(f"{where}: confidence caps must be one of {CONFIDENCES}")
    try:
        return Service(
            id=sid,
            name=str(data.get("name", sid)),
            category=str(data.get("category", "other")),
            patterns=patterns,
            heartbeat=heartbeat,
            min_rate=int(data.get("min_rate", 4)),
            min_duration=parse_duration(str(data.get("min_duration", "5m"))),
            max_gap=parse_duration(str(data.get("max_gap", "5m"))),
            verified=bool(data.get("verified", False)),
            phone_max_confidence=cap,
            max_confidence=overall,
            source=source,
        )
    except WindowError as e:
        raise ServiceError(f"{where}: {e}") from None


def user_services_dir() -> Path:
    return paths.home() / "services"


def load_services(user_dir: Path | None = None) -> dict[str, Service]:
    found: dict[str, Service] = {}
    bundled = resources.files("querytale") / "presets"
    for item in sorted(bundled.iterdir(), key=lambda p: p.name):
        if item.name.endswith((".yaml", ".yml")):
            svc = parse_service(yaml.safe_load(item.read_text()), f"preset {item.name}")
            found[svc.id] = svc
    user_dir = user_dir or user_services_dir()
    if user_dir.is_dir():
        for path in sorted(user_dir.glob("*.y*ml")):
            try:
                data = yaml.safe_load(path.read_text())
            except yaml.YAMLError as e:
                raise ServiceError(f"{path}: {e}") from None
            svc = parse_service(data, str(path))
            found[svc.id] = svc
    return found


def fingerprint(services: dict[str, Service]) -> str:
    """Changes whenever matching would change, so derived tables know to rebuild."""
    doc = {
        sid: [[p.kind, p.value] for p in s.patterns]
        + [["hb", p.kind, p.value] for p in s.heartbeat]
        for sid, s in sorted(services.items())
    }
    return hashlib.sha256(json.dumps(doc).encode()).hexdigest()[:16]
