"""Transcript log parsing to isolate LLM request duration from tool execution."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agy_tps_statusline import paths

_ANTIGRAVITY_SEGMENT_RE = re.compile(r"([/\\])antigravity([/\\])")


def to_int(value: Any) -> Optional[int]:
    """Convert value to int, returning None on invalid input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def parse_iso_utc(ts: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp (including trailing 'Z') to UTC epoch seconds."""
    if not isinstance(ts, str):
        return None
    text = ts.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def rewrite_antigravity_cli_path(raw: str) -> str:
    """Rewrite ``/antigravity/`` or ``\\antigravity\\`` to ``antigravity-cli``."""
    return _ANTIGRAVITY_SEGMENT_RE.sub(r"\1antigravity-cli\2", raw)


def resolve_transcript_path(data: dict, session_id: str) -> Optional[Path]:
    """Locate the active session's transcript.jsonl file.

    agy 1.5.8 reports ``~/.gemini/antigravity/brain/<sid>/...`` in
    ``transcript_path`` while CLI transcripts actually live under
    ``~/.gemini/antigravity-cli/brain/<sid>/.system_generated/logs/transcript.jsonl``.
    """
    raw = data.get("transcript_path")
    if isinstance(raw, str) and raw.strip():
        candidates = [raw.strip()]
        rewritten = rewrite_antigravity_cli_path(raw.strip())
        if rewritten != raw.strip():
            candidates.append(rewritten)
        for candidate in candidates:
            try:
                p = Path(candidate).expanduser()
                if p.is_file():
                    return p
            except OSError:
                pass

    safe_sid = paths.safe_session_id(session_id)
    fallback = paths.brain_dir() / safe_sid / ".system_generated" / "logs" / "transcript.jsonl"
    try:
        if fallback.is_file():
            return fallback
    except OSError:
        pass
    return None


def read_latest_llm_timing(
    transcript_path: Optional[Path],
    prev_step_idx: Optional[int],
    tail_bytes: int = 65536,
) -> tuple[Optional[float], Optional[int]]:
    """Return ``(llm_start_ts, max_step_idx)`` for new output-token steps.

    Reads at most the last ``tail_bytes`` of ``transcript_path`` and inspects
    entries with ``output_tokens > 0`` (such as ``PLANNER_RESPONSE`` or
    ``CHECKPOINT``).
    """
    if transcript_path is None:
        return None, prev_step_idx

    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            offset = max(0, size - tail_bytes)
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return None, prev_step_idx

    if offset > 0:
        nl = chunk.find(b"\n")
        if nl == -1:
            return None, prev_step_idx
        chunk = chunk[nl + 1 :]

    text = chunk.decode("utf-8", errors="replace")
    matched: list[tuple[Optional[int], Optional[float]]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        out_tok = to_int(obj.get("output_tokens"))
        if out_tok is None or out_tok <= 0:
            continue
        step_idx = to_int(obj.get("step_index"))
        created_ts = parse_iso_utc(obj.get("created_at"))
        matched.append((step_idx, created_ts))

    if not matched:
        return None, prev_step_idx

    if prev_step_idx is not None:
        newer = [(idx, ts) for idx, ts in matched if idx is not None and idx > prev_step_idx]
        if not newer:
            return None, prev_step_idx
        valid_ts = [ts for _, ts in newer if ts is not None]
        max_idx = max(idx for idx, _ in newer if idx is not None)
        return (min(valid_ts) if valid_ts else None), max_idx

    last_idx, last_ts = matched[-1]
    return last_ts, last_idx


def make_timing_getter(
    data: dict, session_id: str
) -> Callable[[Optional[int]], tuple[Optional[float], Optional[int]]]:
    """Create a lazy callback to read transcript timing only on token jumps."""

    def _getter(prev_step_idx: Optional[int]) -> tuple[Optional[float], Optional[int]]:
        tp = resolve_transcript_path(data, session_id)
        return read_latest_llm_timing(tp, prev_step_idx)

    return _getter
