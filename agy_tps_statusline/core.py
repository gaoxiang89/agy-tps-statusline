"""Core payload extraction, TPS state machine, and atomic state storage."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from agy_tps_statusline import paths
from agy_tps_statusline.transcript import to_int

STALE_SECONDS = 7 * 24 * 3600
_MIN_DT_SECONDS = 0.2


def to_float(value: Any) -> Optional[float]:
    """Convert value to float, returning None on invalid input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def normalize_quota_windows(data: dict) -> dict[str, dict[str, Any]]:
    """Normalize ``quota`` (agy 1.4+) and legacy ``rate_limits`` shapes."""
    windows: dict[str, dict[str, Any]] = {}

    quota = data.get("quota")
    if isinstance(quota, dict):
        for key, spec in quota.items():
            if not isinstance(spec, dict):
                continue
            remaining = to_float(spec.get("remaining_fraction"))
            if remaining is None:
                continue
            used_pct = max(0.0, min(100.0, round((1.0 - remaining) * 100.0, 1)))
            windows[str(key)] = {
                "used_pct": used_pct,
                "resets_at": str(spec.get("reset_time") or ""),
            }

    rate = data.get("rate_limits")
    if isinstance(rate, dict):
        for key in ("five_hour", "seven_day"):
            spec = rate.get(key)
            if not isinstance(spec, dict):
                continue
            used = to_float(spec.get("used_percentage"))
            if used is None:
                remaining = to_float(spec.get("remaining_fraction"))
                if remaining is None:
                    continue
                used = (1.0 - remaining) * 100.0
            windows.setdefault(
                key,
                {
                    "used_pct": max(0.0, min(100.0, round(used, 1))),
                    "resets_at": str(spec.get("resets_at") or ""),
                },
            )

    return windows


def extract_payload(data: Any) -> dict[str, Any]:
    """Extract normalized fields from agy's status-line JSON payload."""
    if not isinstance(data, dict):
        return {
            "out_toks": None,
            "in_toks": None,
            "ctx_size": None,
            "used_pct": None,
            "agent_state": "",
            "model_name": "",
            "session_id": "default",
            "terminal_width": 0,
            "quota_windows": {},
            "email": "",
        }

    out_toks: Optional[int] = None
    in_toks: Optional[int] = None
    ctx_size: Optional[int] = None
    used_pct: Optional[float] = None

    cw = data.get("context_window")
    if isinstance(cw, dict):
        out_toks = to_int(cw.get("total_output_tokens"))
        in_toks = to_int(cw.get("total_input_tokens"))
        ctx_size = to_int(cw.get("context_window_size"))
        used_pct = to_float(cw.get("used_percentage"))
        if used_pct is None:
            rem = to_float(cw.get("remaining_percentage"))
            if rem is not None:
                used_pct = max(0.0, min(100.0, 100.0 - rem))

    if used_pct is None and in_toks is not None and ctx_size and ctx_size > 0:
        used_pct = max(0.0, min(100.0, (in_toks * 100.0) / ctx_size))

    # Fallbacks for legacy/alternative schemas
    if out_toks is None:
        if "output_tokens" in data:
            out_toks = to_int(data["output_tokens"])
        elif isinstance(data.get("current_usage"), dict):
            out_toks = to_int(data["current_usage"].get("output_tokens"))
        elif isinstance(data.get("tokens"), dict):
            out_toks = to_int(data["tokens"].get("output"))

    model_name = ""
    model = data.get("model")
    if isinstance(model, dict):
        model_name = str(model.get("display_name") or model.get("id") or "")
    elif isinstance(model, str):
        model_name = model

    agent_state = str(data.get("agent_state") or "working")
    session_id = str(data.get("session_id") or data.get("conversation_id") or "default")
    terminal_width = to_int(data.get("terminal_width")) or 0
    email = data.get("email")
    email_str = email.strip() if isinstance(email, str) else ""

    return {
        "out_toks": out_toks,
        "in_toks": in_toks,
        "ctx_size": ctx_size,
        "used_pct": used_pct,
        "agent_state": agent_state,
        "model_name": model_name,
        "session_id": session_id,
        "terminal_width": max(0, terminal_width),
        "quota_windows": normalize_quota_windows(data),
        "email": email_str,
    }


def update_state(
    prev: Optional[dict[str, Any]],
    out_toks: Optional[int],
    agent_state: str,
    now: float,
    get_transcript_timing: Callable[[Optional[int]], tuple[Optional[float], Optional[int]]],
) -> tuple[dict[str, Any], bool]:
    """Compute next TPS state and whether disk persistence is needed.

    Only marks ``dirty=True`` when persistent fields change (``out``,
    ``working``, ``rate``, or ``step_idx``), keeping disk writes at zero during
    idle ticks and in-flight polling.
    """
    working = agent_state == "working"
    if prev is None:
        step_idx: Optional[int] = None
        if out_toks is not None and out_toks > 0:
            _, step_idx = get_transcript_timing(None)
        return {
            "out": out_toks,
            "t_start": now,
            "rate": 0.0,
            "working": working,
            "step_idx": step_idx,
        }, True

    rate = to_float(prev.get("rate")) or 0.0
    t_start = to_float(prev.get("t_start"))
    if t_start is None:
        t_start = now
    prev_out = to_int(prev.get("out"))
    if prev_out is None:
        prev_out = out_toks
    prev_working = bool(prev.get("working", True))
    step_idx = to_int(prev.get("step_idx"))
    dirty = False

    if working and not prev_working:
        t_start = now
        dirty = True

    if out_toks is not None and prev_out is not None and out_toks < prev_out:
        # Session rewound or reset; treat current count as the new baseline.
        prev_out = out_toks
        t_start = now
        dirty = True

    if out_toks is not None and prev_out is not None and out_toks > prev_out:
        delta = out_toks - prev_out
        llm_start_ts, new_step_idx = get_transcript_timing(step_idx)

        effective_start: Optional[float]
        if prev_working:
            if llm_start_ts is not None and (t_start - 2.0) <= llm_start_ts <= (now + 1.0):
                effective_start = max(t_start, llm_start_ts)
            else:
                effective_start = t_start
        else:
            if llm_start_ts is not None and llm_start_ts <= (now + 1.0):
                effective_start = llm_start_ts
            else:
                # Sub-300ms response without transcript anchor: do not divide
                # by stale idle time.
                effective_start = None

        if effective_start is not None:
            dt = max(now - effective_start, _MIN_DT_SECONDS)
            rate = delta / dt

        t_start = now
        if new_step_idx is not None:
            step_idx = new_step_idx
        dirty = True

    if working != prev_working or out_toks != prev_out:
        dirty = True

    return {
        "out": out_toks,
        "t_start": t_start,
        "rate": rate,
        "working": working,
        "step_idx": step_idx,
    }, dirty


def load_json(path: Path) -> Optional[dict[str, Any]]:
    """Read a JSON object from disk, returning None on error or invalid shape."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def atomic_write_text(path: Path, text: str) -> bool:
    """Atomically write text to ``path`` using a PID-scoped temporary file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def atomic_write_json(path: Path, payload: dict[str, Any], *, indent: Optional[int] = None) -> bool:
    """Serialize ``payload`` to JSON and write atomically."""
    serialized = json.dumps(payload, indent=indent, sort_keys=(indent is not None))
    return atomic_write_text(path, serialized)


def prune_stale(state_dir: Path, now: float) -> None:
    """Remove session TPS state files older than ``STALE_SECONDS``."""
    try:
        candidates = list(state_dir.glob("*.json"))
    except OSError:
        return
    for p in candidates:
        try:
            if now - p.stat().st_mtime > STALE_SECONDS:
                p.unlink()
        except OSError:
            pass
