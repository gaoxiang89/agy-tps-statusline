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


def _iter_jsonl_objects(chunk: bytes):
    text = chunk.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _read_transcript_chunk(path: Path, tail_bytes: Optional[int]) -> Optional[tuple[bytes, bool]]:
    """Return ``(chunk, is_partial)``; ``tail_bytes=None`` reads the whole file."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            offset = 0 if tail_bytes is None else max(0, size - tail_bytes)
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return None
    if offset > 0:
        nl = chunk.find(b"\n")
        if nl == -1:
            return b"", True
        chunk = chunk[nl + 1 :]
    return chunk, offset > 0


def _usage_entries(chunk: bytes) -> list[tuple[int, int, int]]:
    """Extract ``(step_idx, uncached_input, cache_read)`` from model steps."""
    entries: list[tuple[int, int, int]] = []
    for obj in _iter_jsonl_objects(chunk):
        if "input_tokens" not in obj and "cache_read_tokens" not in obj:
            continue
        idx = to_int(obj.get("step_index"))
        if idx is None:
            continue
        inp = max(0, to_int(obj.get("input_tokens")) or 0)
        hit = max(0, to_int(obj.get("cache_read_tokens")) or 0)
        if inp + hit <= 0:
            continue
        entries.append((idx, inp, hit))
    return entries


def update_cache_stats(
    transcript_path: Optional[Path],
    prev: Optional[dict[str, Any]],
    tail_bytes: int = 65536,
) -> Optional[dict[str, Any]]:
    """Incrementally accumulate prompt-cache usage from the transcript.

    Each model step records ``input_tokens`` (uncached prompt tokens) and
    ``cache_read_tokens`` (prompt tokens served from cache). The hit rate is
    ``cache_read / (input + cache_read)``.

    Returned dict: ``idx`` (last counted step), ``in``/``hit`` (session sums),
    ``last_in``/``last_hit`` (latest step). Returns ``prev`` unchanged when
    nothing new is found, or ``None`` when no data is available.
    """
    if transcript_path is None:
        return prev

    prev_ok = isinstance(prev, dict) and to_int(prev.get("idx")) is not None
    entries: list[tuple[int, int, int]]
    if prev_ok:
        res = _read_transcript_chunk(transcript_path, tail_bytes)
        if res is None:
            return prev
        chunk, partial = res
        entries = _usage_entries(chunk)
        prev_idx = to_int(prev.get("idx"))  # type: ignore[union-attr]
        # If the tail window does not reach back to the last counted step,
        # some steps would be skipped: rescan the full file instead.
        if partial and not any(idx <= prev_idx for idx, _, _ in entries):
            prev_ok = False
    if not prev_ok:
        res = _read_transcript_chunk(transcript_path, None)
        if res is None:
            return prev
        entries = _usage_entries(res[0])
        if not entries:
            return None
        total_in = sum(e[1] for e in entries)
        total_hit = sum(e[2] for e in entries)
        last = max(entries, key=lambda e: e[0])
        return {
            "idx": last[0],
            "in": total_in,
            "hit": total_hit,
            "last_in": last[1],
            "last_hit": last[2],
        }

    assert prev is not None
    newer = [e for e in entries if e[0] > prev_idx]
    if not newer:
        return prev
    last = max(newer, key=lambda e: e[0])
    return {
        "idx": last[0],
        "in": (to_int(prev.get("in")) or 0) + sum(e[1] for e in newer),
        "hit": (to_int(prev.get("hit")) or 0) + sum(e[2] for e in newer),
        "last_in": last[1],
        "last_hit": last[2],
    }
