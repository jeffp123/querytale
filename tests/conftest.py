from __future__ import annotations

from datetime import datetime

import pytest

from querytale import keystore
from querytale.config import Config, Profile
from querytale.store import Store
from tests.fakes import PROFILE_ID

NOW = datetime(2026, 10, 28, 12, 0, 0)  # UTC


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own private home and a fake keychain."""
    monkeypatch.setenv("QUERYTALE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(keystore.ENV_VAR, raising=False)
    monkeypatch.setenv("TZ", "America/New_York")
    vault: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keystore.keyring, "get_password", lambda s, u: vault.get((s, u)))
    monkeypatch.setattr(
        keystore.keyring, "set_password", lambda s, u, p: vault.__setitem__((s, u), p)
    )
    return tmp_path / "home"


@pytest.fixture
def cfg() -> Config:
    return Config(
        profiles={"alex": Profile("alex", PROFILE_ID, "Alex")}, timezone="America/New_York"
    )


@pytest.fixture
def store():
    s = Store.memory()
    yield s
    s.close()
