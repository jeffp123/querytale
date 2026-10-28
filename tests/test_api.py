from datetime import datetime

import httpx
import pytest

from querytale.api import ApiError, NextDNSClient, ReadOnlyViolation
from tests.fakes import API_KEY, PROFILE_ID, FakeNextDNS, entry


def client_for(fake: FakeNextDNS, key: str = API_KEY, **kw) -> NextDNSClient:
    return NextDNSClient(key, transport=fake.transport(), sleep=lambda s: None, **kw)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_client_refuses_non_get_methods(method):
    fake = FakeNextDNS([])
    with client_for(fake) as client, pytest.raises(ReadOnlyViolation):
        client.request(method, f"/profiles/{PROFILE_ID}/logs")
    assert fake.requests == []


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_underlying_http_client_is_guarded_too(method):
    fake = FakeNextDNS([])
    with client_for(fake) as client, pytest.raises(ReadOnlyViolation):
        getattr(client._http, method)(f"/profiles/{PROFILE_ID}/logs")
    assert fake.requests == []


def test_get_works_and_sends_key():
    fake = FakeNextDNS([])
    with client_for(fake) as client:
        assert client.profiles() == [{"id": PROFILE_ID, "name": "Alex"}]
    assert fake.requests[0].headers["X-Api-Key"] == API_KEY


def test_429_honors_retry_after():
    fake = FakeNextDNS([])
    fake.rate_limit_next = 2
    sleeps: list[float] = []
    client = NextDNSClient(API_KEY, transport=fake.transport(), sleep=sleeps.append)
    assert client.profiles()
    assert sleeps == [3.0, 3.0]


def test_gives_up_after_max_retries():
    fake = FakeNextDNS([])
    fake.rate_limit_next = 99
    with client_for(fake, max_retries=2) as client, pytest.raises(ApiError, match="429"):
        client.profiles()
    assert len(fake.requests) == 3


def test_errors_never_contain_the_key():
    def echo_key(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": [{"detail": f"bad {API_KEY}"}]})

    client = NextDNSClient(API_KEY, transport=httpx.MockTransport(echo_key))
    with pytest.raises(ApiError) as exc:
        client.profiles()
    assert API_KEY not in str(exc.value)
    assert "check the API key" in str(exc.value)


def test_log_pages_follow_cursor():
    entries = [entry(datetime(2026, 10, 28, 10, 0, i), f"site{i}.com") for i in range(25)]
    fake = FakeNextDNS(entries)
    with client_for(fake) as client:
        pages = list(client.iter_log_pages(PROFILE_ID, start=datetime(2026, 10, 28), limit=10))
    assert [len(p) for p in pages] == [10, 10, 5]
    assert fake.requests[0].url.params["raw"] == "1"
    assert fake.requests[0].url.params["from"] == "2026-10-28T00:00:00.000Z"
    assert fake.requests[1].url.params["cursor"] == "10"
