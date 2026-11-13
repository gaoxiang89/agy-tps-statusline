"""Environment-overridable filesystem paths used by agy-tps-statusline."""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(var: str, default: Path) -> Path:
    value = os.environ.get(var, "").strip()
    return Path(value).expanduser() if value else default


def gemini_dir() -> Path:
    """Return agy's configuration root (overridable via AGY_GEMINI_DIR)."""
    return _env_path("AGY_GEMINI_DIR", Path.home() / ".gemini")


def agy_cli_dir() -> Path:
    """Return the Antigravity CLI state directory."""
    return gemini_dir() / "antigravity-cli"


def settings_path() -> Path:
    """Return path to agy CLI's settings.json."""
    return agy_cli_dir() / "settings.json"


def cache_dir() -> Path:
    """Return agy CLI's cache directory."""
    return agy_cli_dir() / "cache"


def tps_state_dir() -> Path:
    """Return directory storing per-session TPS state JSON files."""
    return cache_dir() / "tps"


def brain_dir() -> Path:
    """Return directory containing per-conversation brain logs."""
    return agy_cli_dir() / "brain"


def debug_input_path() -> Path:
    """Return optional debug dump path for status-line stdin JSON."""
    return cache_dir() / "last_statusline_input.json"


def agyswap_dir() -> Path:
    """Return agy-swap's data root (overridable via AGYSWAP_DATA_DIR)."""
    return _env_path("AGYSWAP_DATA_DIR", Path.home() / ".agy-swap")


def agyswap_quota_path() -> Path:
    """Return path to agy-swap's Schema 2 quota cache."""
    return agyswap_dir() / "cache" / "quota.json"


def safe_session_id(session_id: str) -> str:
    """Sanitize a session/conversation ID for use as a filename."""
    cleaned = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
    return cleaned or "default"
