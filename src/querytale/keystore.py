"""The NextDNS API key lives in the OS keychain; NEXTDNS_API_KEY overrides it."""

from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError

SERVICE = "querytale"
USERNAME = "nextdns-api-key"
ENV_VAR = "NEXTDNS_API_KEY"


def get_api_key() -> str | None:
    if key := os.environ.get(ENV_VAR):
        return key.strip()
    try:
        return keyring.get_password(SERVICE, USERNAME)
    except KeyringError:
        return None


def stored_api_key() -> str | None:
    """The key in the keychain only, ignoring the environment."""
    try:
        return keyring.get_password(SERVICE, USERNAME)
    except KeyringError:
        return None


def set_api_key(key: str) -> None:
    keyring.set_password(SERVICE, USERNAME, key.strip())
