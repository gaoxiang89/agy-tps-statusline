"""ANSI-aware status line formatting and responsive terminal truncation."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from agy_tps_statusline.agyswap_sync import format_reset_countdown

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[94m"
GRAY = "\033[90m"
RESET = "\033[0m"

_WIDE_CHARS = {"⚡", "⏳"}

_WINDOW_SHORT_LABELS = {
    "gemini-5h": "gem 5h",
    "gemini-weekly": "gem 7d",
    "gemini-argon-wtu-weekly": "argon 7d",
    "3p-5h": "3p 5h",
    "3p-weekly": "3p 7d",
    "five_hour": "5h",
    "seven_day": "7d",
}

_WINDOW_ORDER = (
    "gemini-5h",
    "gemini-weekly",
    "gemini-argon-wtu-weekly",
    "3p-5h",
    "3p-weekly",
    "five_hour",
    "seven_day",
)


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR escape sequences from ``text``."""
    return _ANSI_RE.sub("", text)


def visible_width(text: str) -> int:
    """Compute approximate terminal display column width ignoring ANSI codes."""
    plain = strip_ansi(text)
    width = 0
    for ch in plain:
        if ch in _WIDE_CHARS or unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def fmt_tokens(count: int) -> str:
    """Format token counts concisely (e.g. ``850``, ``3.9k``, ``1.0M``)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _pct_color(pct: float) -> str:
    if pct >= 80.0:
        return RED
    if pct >= 50.0:
        return YELLOW
    return GREEN


def _format_wait(elapsed: float) -> str:
    secs = max(0, int(elapsed))
    if secs < 60:
        return f"{secs}s"
    mins, rem = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{rem:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


def _format_tps(state: dict[str, Any], now: float) -> str:
    rate = float(state.get("rate") or 0.0)
    working = bool(state.get("working"))
    t_start = float(state.get("t_start") or now)
    elapsed = now - t_start

    if working and elapsed >= 2.0:
        wait_str = _format_wait(elapsed)
        if rate > 0:
            return f"{GRAY}⚡ {rate:.1f} tok/s{RESET} {YELLOW}⏳ {wait_str}{RESET}"
        return f"{YELLOW}⚡ ⏳ {wait_str}{RESET}"

    if rate > 0:
        return f"{GREEN}⚡ {rate:.1f} tok/s{RESET}"
    return f"{GRAY}⚡ -- tok/s{RESET}"


def _format_context(
    in_toks: Optional[int],
    ctx_size: Optional[int],
    used_pct: Optional[float],
    *,
    compact: bool = False,
) -> str:
    if used_pct is None:
        return ""
    color = _pct_color(used_pct)
    if compact or in_toks is None or not ctx_size:
        return f"{color}ctx {used_pct:.0f}%{RESET}"
    return f"{color}ctx {fmt_tokens(in_toks)}/{fmt_tokens(ctx_size)} ({used_pct:.0f}%){RESET}"


def _hit_pct(inp: Any, hit: Any) -> Optional[float]:
    if not isinstance(inp, int) or not isinstance(hit, int):
        return None
    total = inp + hit
    if total <= 0:
        return None
    return hit * 100.0 / total


def _hit_color(pct: float) -> str:
    if pct >= 70.0:
        return GREEN
    if pct >= 30.0:
        return YELLOW
    return GRAY


def _format_cache(cache: Any, *, compact: bool = False) -> str:
    """Render prompt-cache hit rate: session-weighted plus the latest request."""
    if not isinstance(cache, dict):
        return ""
    total = _hit_pct(cache.get("in"), cache.get("hit"))
    if total is None:
        return ""
    text = f"{_hit_color(total)}cache {total:.0f}%{RESET}"
    if compact:
        return text
    last = _hit_pct(cache.get("last_in"), cache.get("last_hit"))
    if last is not None:
        text += f" {GRAY}(last{RESET} {_hit_color(last)}{last:.0f}%{RESET}{GRAY}){RESET}"
    return text


def _compact_window_label(key: str) -> str:
    if key in _WINDOW_SHORT_LABELS:
        return _WINDOW_SHORT_LABELS[key]
    text = key.replace("_", "-").lower()
    text = text.replace("gemini-", "gem ").replace("-weekly", " 7d").replace("-wtu", "")
    return text[:12]


def _color_pct(text: str, pct: float) -> str:
    if pct >= 80.0:
        return f"{RED}{text}{RESET}"
    if pct >= 50.0:
        return f"{YELLOW}{text}{RESET}"
    return f"{GRAY}{text}{RESET}"


def _format_quota_full(windows: dict[str, dict[str, Any]], now: float) -> str:
    if not windows:
        return ""
    ordered = [k for k in _WINDOW_ORDER if k in windows]
    ordered += sorted(k for k in windows if k not in _WINDOW_ORDER)
    tightest = max(ordered, key=lambda k: float(windows[k].get("used_pct") or 0.0))

    def render_one(key: str, label: str) -> str:
        spec = windows[key]
        pct = float(spec.get("used_pct") or 0.0)
        text = f"{label} {pct:.0f}%"
        if key == tightest and pct >= 20.0:
            cd = format_reset_countdown(str(spec.get("resets_at") or ""), now)
            if cd:
                text += f" ({cd})"
        return _color_pct(text, pct)

    chunks: list[str] = []
    consumed: set[str] = set()

    for prefix, g5, g7 in (("gem", "gemini-5h", "gemini-weekly"), ("3p", "3p-5h", "3p-weekly")):
        if g5 in windows and g7 in windows:
            consumed.update((g5, g7))
            p5 = render_one(g5, "5h")
            p7 = render_one(g7, "7d")
            chunks.append(f"{GRAY}{prefix}{RESET} {p5} {GRAY}·{RESET} {p7}")

    for key in ordered:
        if key in consumed:
            continue
        chunks.append(render_one(key, _compact_window_label(key)))

    return f" {GRAY}·{RESET} ".join(chunks) if len(chunks) == 1 else f" {GRAY}│{RESET} ".join(chunks)


def _format_quota_compact(windows: dict[str, dict[str, Any]], now: float) -> str:
    if not windows:
        return ""
    tightest = max(windows.keys(), key=lambda k: float(windows[k].get("used_pct") or 0.0))
    spec = windows[tightest]
    pct = float(spec.get("used_pct") or 0.0)
    label = _compact_window_label(tightest)
    text = f"{label} {pct:.0f}%"
    if pct >= 20.0:
        cd = format_reset_countdown(str(spec.get("resets_at") or ""), now)
        if cd:
            text += f" ({cd})"
    return _color_pct(text, pct)


def _truncate_model(model: str, max_len: int) -> str:
    if max_len <= 0 or len(model) <= max_len:
        return model
    if max_len <= 3:
        return model[:max_len]
    return model[: max_len - 1] + "…"


def render_statusline(
    payload: dict[str, Any],
    state: dict[str, Any],
    now: float,
) -> str:
    """Render the complete ANSI status line, adapting to ``terminal_width``."""
    tps_seg = _format_tps(state, now)

    out_toks = payload.get("out_toks")
    out_seg = (
        f"{CYAN}{fmt_tokens(out_toks)} out{RESET}"
        if isinstance(out_toks, int) and out_toks > 0
        else ""
    )

    in_toks = payload.get("in_toks")
    ctx_size = payload.get("ctx_size")
    used_pct = payload.get("used_pct")
    windows = payload.get("quota_windows") or {}
    model = str(payload.get("model_name") or "")
    term_width = int(payload.get("terminal_width") or 0)

    sep = f" {GRAY}│{RESET} "

    def join_parts(*items: str) -> str:
        return sep.join(item for item in items if item)

    ctx_full = _format_context(in_toks, ctx_size, used_pct, compact=False)
    ctx_compact = _format_context(in_toks, ctx_size, used_pct, compact=True)
    quota_full = _format_quota_full(windows, now)
    quota_compact = _format_quota_compact(windows, now)
    model_seg = f"{BLUE}{model}{RESET}" if model else ""
    cache_full = _format_cache(state.get("cache"), compact=False)
    cache_compact = _format_cache(state.get("cache"), compact=True)

    candidates = [
        join_parts(tps_seg, out_seg, ctx_full, cache_full, quota_full, model_seg),
        join_parts(tps_seg, out_seg, ctx_full, cache_compact, quota_full, model_seg),
        join_parts(tps_seg, out_seg, ctx_compact, cache_compact, quota_compact, model_seg),
        join_parts(tps_seg, out_seg, ctx_compact, cache_compact, model_seg),
        join_parts(tps_seg, out_seg, ctx_compact, model_seg),
        join_parts(tps_seg, out_seg, model_seg),
    ]

    if term_width <= 0:
        return candidates[0]

    for line in candidates:
        if visible_width(line) <= term_width:
            return line

    # Narrow terminal fallback: keep tps + out + truncated model (or tps + model).
    base_no_model = join_parts(tps_seg, out_seg)
    if model_seg:
        avail = term_width - visible_width(base_no_model) - visible_width(sep)
        if avail >= 6:
            return join_parts(tps_seg, out_seg, f"{BLUE}{_truncate_model(model, avail)}{RESET}")
        avail_tps_only = term_width - visible_width(tps_seg) - visible_width(sep)
        if avail_tps_only >= 6:
            return join_parts(tps_seg, f"{BLUE}{_truncate_model(model, avail_tps_only)}{RESET}")
    return base_no_model if visible_width(base_no_model) <= term_width else tps_seg
