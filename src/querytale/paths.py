"""Where querytale keeps its files, and how they are protected.

Everything lives in one private folder: ~/Library/Application Support/querytale on macOS,
the platformdirs equivalent elsewhere. QUERYTALE_HOME overrides it (tests, multiple setups).
"""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs

APP = "querytale"
HOME_ENV = "QUERYTALE_HOME"


def home() -> Path:
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_data_dir(APP, appauthor=False))


def config_path() -> Path:
    return home() / "config.toml"


def db_path() -> Path:
    return home() / "data.duckdb"


def lock_path() -> Path:
    return home() / "sync.lock"


def ensure_private_dir(path: Path) -> Path:
    """Create a directory readable only by the current user (mode 0700)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def make_private(path: Path) -> None:
    """Restrict an existing file to the current user (mode 0600)."""
    if path.exists():
        path.chmod(0o600)
