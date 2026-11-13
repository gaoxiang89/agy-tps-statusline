"""Zero-fork Schema 2 quota cache syncing for agy-swap."""

from __future__ import annotations

import os
from typing import Any

from agy_tps_statusline import paths
from agy_tps_statusline.core import atomic_write_json, load_json, normalize_quota_windows
from agy_tps_statusline.transcript import parse_iso_utc

SCHEMA = 2
MTIME_THROTTLE_S = 10.0
UNCHANGED_THROTTLE_S = 60.0


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _read_email_hint(data: dict) -> str:
    email = data.get("email")
    if isinstance(email, str) and "@" in email:
        return email.strip()
    return ""


def maybe_sync_agyswap_quota(
    data: dict,
    now: float,
    windows: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """Sync quota windows into ``~/.agy-swap/cache/quota.json`` when installed.

    Uses ``os.stat`` mtime throttling (10s) on the fast path so ~3Hz status-line
    refreshes incur virtually zero overhead.
    """
    if os.environ.get("AGY_TPS_NO_AGYSWAP") == "1":
        return False
    if not isinstance(data, dict):
        return False

    swap_root = paths.agyswap_dir()
    try:
        if not swap_root.is_dir():
            return False
    except OSError:
        return False

    email = _read_email_hint(data)
    if not email:
        return False

    if windows is None:
        windows = normalize_quota_windows(data)
    if not windows:
        return False

    quota_file = paths.agyswap_quota_path()
    try:
        st = os.stat(quota_file)
        if now - st.st_mtime < MTIME_THROTTLE_S:
            return False
    except OSError:
        pass

    cache = load_json(quota_file)
    if not cache or cache.get("schema") != SCHEMA or not isinstance(cache.get("accounts"), dict):
        cache = {"schema": SCHEMA, "accounts": {}}

    accounts: dict[str, Any] = cache["accounts"]
    entry = accounts.get(email)
    if not isinstance(entry, dict):
        entry = {}
    accounts[email] = entry

    existing = entry.get("windows") if isinstance(entry.get("windows"), dict) else {}
    last_seen = _to_float((entry.get("sources") or {}).get("statusline"))
    if existing == windows and (now - last_seen) < UNCHANGED_THROTTLE_S:
        return False

    entry["windows"] = {**existing, **windows}
    entry["captured_at"] = now
    sources = entry.get("sources")
    if not isinstance(sources, dict):
        sources = {}
        entry["sources"] = sources
    sources["statusline"] = now

    return atomic_write_json(quota_file, cache, indent=2)


def is_agyswap_installed() -> bool:
    """Return True if agy-swap's data directory exists."""
    try:
        return paths.agyswap_dir().is_dir()
    except OSError:
        return False


def format_reset_countdown(resets_at: str, now: float) -> str:
    """Format an ISO reset timestamp into a compact ``XhYm`` or ``Ym`` countdown."""
    ts = parse_iso_utc(resets_at)
    if ts is None or ts <= now:
        return ""
    secs = int(ts - now)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{mins}m"
    return f"{max(1, mins)}m"
