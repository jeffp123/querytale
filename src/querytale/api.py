"""A read-only NextDNS API client.

A NextDNS API key can change settings and clear logs, so this client permits GET and nothing
else. The check happens twice: in `request()`, and in an httpx request hook that also covers
anyone reaching for the underlying httpx client directly.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from querytale import __version__

BASE_URL = "https://api.nextdns.io"
PAGE_LIMIT = 1000


class ReadOnlyViolation(RuntimeError):
    """Raised for any HTTP method other than GET."""


class ApiError(RuntimeError):
    pass


def _guard(request: httpx.Request) -> None:
    if request.method != "GET":
        raise ReadOnlyViolation(f"querytale is read-only; refusing {request.method} {request.url}")


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _download_url(body: dict) -> str:
    """The export URL from a redirect=0 response; tolerate the shapes the docs leave open."""
    data = body.get("data", body)
    url = data.get("url") if isinstance(data, dict) else data
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ApiError("NextDNS did not return a download link for the log export")
    return url


class NextDNSClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 6,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ) -> None:
        self._key = api_key
        self._transport = transport
        self._max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url,
            headers={"X-Api-Key": api_key, "User-Agent": f"querytale/{__version__}"},
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            event_hooks={"request": [_guard]},
        )

    def __enter__(self) -> NextDNSClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        if method.upper() != "GET":
            raise ReadOnlyViolation(f"querytale is read-only; refusing {method.upper()} {path}")
        extra = {"timeout": timeout} if timeout else {}
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._http.get(path, params=params, **extra)
            except ReadOnlyViolation:
                raise
            except httpx.TransportError as e:
                if attempt == self._max_retries:
                    raise ApiError(self.redact(f"Network error talking to NextDNS: {e}")) from None
                self._sleep(min(2**attempt, 60))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries:
                    break
                self._sleep(self._retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise ApiError(self._describe(resp))
            return resp.json()
        raise ApiError(self._describe(resp) + f" (gave up after {self._max_retries} retries)")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        return self.request("GET", path, params)

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return float(min(2**attempt, 60))

    def _describe(self, resp: httpx.Response) -> str:
        detail = ""
        try:
            errors = resp.json().get("errors") or []
            detail = ", ".join(str(e.get("code") or e.get("detail") or e) for e in errors)
        except ValueError:
            detail = resp.text[:200]
        hint = " (check the API key)" if resp.status_code in (401, 403) else ""
        msg = f"NextDNS API {resp.status_code} for {resp.request.url.path}{hint}"
        return self.redact(f"{msg}: {detail}" if detail else msg)

    # Endpoints -------------------------------------------------------------------------

    def profiles(self) -> list[dict]:
        return self.get("/profiles").get("data", [])

    def iter_log_pages(
        self,
        profile_id: str,
        *,
        start: datetime,
        end: datetime | None = None,
        limit: int = PAGE_LIMIT,
    ) -> Iterator[list[dict]]:
        """Yield pages of raw log entries, oldest first, following the cursor to the end.

        The live API never ends a sorted listing with a null cursor: past the newest entry it
        keeps answering with an empty page and the same cursor. An empty page or a repeated
        cursor is therefore the end.
        """
        params: dict[str, Any] = {"raw": 1, "sort": "asc", "from": _iso(start), "limit": limit}
        if end is not None:
            params["to"] = _iso(end)
        while True:
            body = self.get(f"/profiles/{profile_id}/logs", params)
            page = body.get("data") or []
            if page:
                yield page
            cursor = ((body.get("meta") or {}).get("pagination") or {}).get("cursor")
            if not page or not cursor or cursor == params.get("cursor"):
                return
            params["cursor"] = cursor

    def download_logs(
        self, profile_id: str, dest: Path, progress: Callable[[int, int | None], None] | None = None
    ) -> Path:
        """Fetch the full CSV export to `dest`.

        The API returns a short-lived signed URL; that file is fetched without the API key,
        through a client with the same GET-only guard.
        """
        # NextDNS generates the file before answering, which can take minutes for a year.
        body = self.request(
            "GET", f"/profiles/{profile_id}/logs/download", {"redirect": 0}, timeout=600
        )
        url = _download_url(body)
        with (
            httpx.Client(
                transport=self._transport,
                timeout=None,
                follow_redirects=True,
                event_hooks={"request": [_guard]},
            ) as http,
            http.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                raise ApiError(f"Export download failed with HTTP {resp.status_code}")
            length = resp.headers.get("Content-Length")
            total = int(length) if length else None
            done = 0
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        return dest
