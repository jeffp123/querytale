"""Synthetic NextDNS logs and a fake API. Never real exports."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

PROFILE_ID = "abc123"
API_KEY = "test-key-0123456789"
EXPORT_URL = "https://exports.example.net/abc123.csv"

PHONE = {"id": "PH0NE", "name": "Alex's iPhone", "model": "iPhone 13", "localIp": None}
LAPTOP = {"id": "L4PT0", "name": "alex-laptop", "model": "Windows", "localIp": "192.168.1.20"}


def entry(
    ts: datetime,
    domain: str,
    qtype: str = "A",
    *,
    device: dict = LAPTOP,
    status: str = "default",
    reasons: list[str] | None = None,
    tracker: str | None = None,
) -> dict:
    root = ".".join(domain.split(".")[-2:])
    return {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "domain": domain,
        "root": root,
        "tracker": tracker,
        "type": qtype,
        "dnssec": False,
        "encrypted": True,
        "protocol": "DNS-over-HTTPS",
        "clientIp": "203.0.113.5",
        "client": "apple-profile" if device is PHONE else "nextdns-cli",
        "device": device,
        "status": status,
        "reasons": [{"id": r, "name": r} for r in reasons or []],
    }


def triplet(ts: datetime, domain: str, **kw) -> list[dict]:
    return [entry(ts, domain, q, **kw) for q in ("A", "AAAA", "HTTPS")]


def synthetic_logs(start: datetime, hours: int = 6) -> list[dict]:
    """A Steam client heartbeating once a minute, a phone checking Apple, a blocked game site."""
    out: list[dict] = []
    for minute in range(hours * 60):
        ts = start + timedelta(minutes=minute, seconds=7)
        out += triplet(ts, "p2p-ord1.discovery.steamserver.net")
        if minute % 5 == 0:
            out += triplet(ts + timedelta(seconds=20), "gateway.icloud.com", device=PHONE)
        if minute % 30 == 0:
            out.append(
                entry(ts + timedelta(seconds=40), "poki.com", status="blocked",
                      reasons=["category:gaming", "blocklist:nextdns-recommended"])
            )  # fmt: skip
    out.sort(key=lambda e: e["timestamp"])
    return out


def to_csv(entries: list[dict], path: Path) -> Path:
    """Write entries the way a NextDNS export does: newest first, CSV columns."""
    cols = [
        "timestamp", "domain", "query_type", "dnssec", "protocol", "client_ip", "status",
        "reasons", "destination_country", "root_domain", "device_id", "device_name",
        "device_model", "device_local_ip", "matched_name", "client_name",
    ]  # fmt: skip
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for e in sorted(entries, key=lambda e: e["timestamp"], reverse=True):
            d = e["device"]
            w.writerow([
                e["timestamp"], e["domain"], e["type"], "false", e["protocol"], e["clientIp"],
                "" if e["status"] == "default" else e["status"],
                ",".join(r["id"] for r in e["reasons"]), "US", e["root"], d["id"], d["name"],
                d["model"], d["localIp"] or "", "", e["client"],
            ])  # fmt: skip
    return path


def parse_api_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class FakeNextDNS:
    """Serves /profiles and /profiles/:id/logs like the real API, from an in-memory list."""

    def __init__(self, entries: list[dict], *, fail_on_request: int | None = None) -> None:
        self.entries = sorted(entries, key=lambda e: e["timestamp"])
        self.requests: list[httpx.Request] = []
        self.fail_on_request = fail_on_request
        self.rate_limit_next = 0
        self.export_csv = b""

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == EXPORT_URL:
            assert "X-Api-Key" not in request.headers  # the key never leaves api.nextdns.io
            return httpx.Response(200, content=self.export_csv)
        if self.fail_on_request and len(self.requests) == self.fail_on_request:
            raise KeyboardInterrupt  # simulate Ctrl-C mid-sync
        if request.headers.get("X-Api-Key") != API_KEY:
            return httpx.Response(403, json={"errors": [{"code": "forbidden"}]})
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return httpx.Response(429, headers={"Retry-After": "3"})
        path = request.url.path
        if path == "/profiles":
            return httpx.Response(200, json={"data": [{"id": PROFILE_ID, "name": "Alex"}]})
        if path == f"/profiles/{PROFILE_ID}/logs":
            return self.logs(request.url.params)
        if path == f"/profiles/{PROFILE_ID}/logs/download":
            assert request.url.params["redirect"] == "0"
            return httpx.Response(200, json={"data": {"url": EXPORT_URL}})
        return httpx.Response(404, json={"errors": [{"code": "notFound"}]})

    def logs(self, params: httpx.QueryParams) -> httpx.Response:
        assert params["raw"] == "1" and params["sort"] == "asc"
        start = parse_api_ts(params["from"])
        end = parse_api_ts(params["to"]) if "to" in params else None
        rows = [
            e for e in self.entries
            if parse_api_ts(e["timestamp"]) >= start
            and (end is None or parse_api_ts(e["timestamp"]) < end)
        ]  # fmt: skip
        offset = int(params.get("cursor", 0))
        limit = int(params.get("limit", 100))
        page = rows[offset : offset + limit]
        # Like the live API: the cursor never becomes null. Past the end, it keeps returning
        # empty pages with the same cursor.
        cursor = str(min(offset + limit, len(rows)))
        body = {"data": page, "meta": {"pagination": {"cursor": cursor}}}
        return httpx.Response(200, content=json.dumps(body))
