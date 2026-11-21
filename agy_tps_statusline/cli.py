"""CLI entry point for agy-tps-statusline."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from agy_tps_statusline import __version__, paths
from agy_tps_statusline.agyswap_sync import maybe_sync_agyswap_quota
from agy_tps_statusline.core import (
    atomic_write_text,
    extract_payload,
    load_json,
    prune_stale,
    update_state,
)
from agy_tps_statusline.installer import (
    collect_doctor_info,
    format_doctor_report,
    install,
    uninstall,
)
from agy_tps_statusline.render import render_statusline
from agy_tps_statusline.transcript import (
    make_timing_getter,
    resolve_transcript_path,
    update_cache_stats,
)


def _configure_stdio() -> None:
    """Force UTF-8 on stdin/stdout so Windows consoles do not fail on emoji."""
    for stream in (sys.stdin, sys.stdout):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _safe_print(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        ascii_safe = text.encode("ascii", errors="replace").decode("ascii")
        print(ascii_safe, flush=True)


def run_statusline_from_raw(raw_input: str, now: Optional[float] = None) -> str:
    """Process one status-line JSON payload and return the rendered line."""
    ts = time.time() if now is None else now
    data: dict = {}
    stripped = raw_input.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = {}

    if data and os.environ.get("AGY_TPS_DEBUG") == "1":
        atomic_write_text(paths.debug_input_path(), raw_input)

    payload = extract_payload(data)
    out_toks = payload["out_toks"]
    agent_state = payload["agent_state"]
    session_id = payload["session_id"]

    state: dict = {
        "out": out_toks,
        "t_start": ts,
        "rate": 0.0,
        "working": agent_state == "working",
        "step_idx": None,
    }

    if out_toks is not None:
        state_dir = paths.tps_state_dir()
        state_file = state_dir / f"{paths.safe_session_id(session_id)}.json"
        prev = load_json(state_file)
        if prev is None:
            prune_stale(state_dir, ts)
        timing_getter = make_timing_getter(data, session_id)
        state, dirty = update_state(prev, out_toks, agent_state, ts, timing_getter)
        prev_cache = prev.get("cache") if isinstance(prev, dict) else None
        if isinstance(prev, dict) and "cache" in prev and not dirty:
            state["cache"] = prev_cache
        else:
            # Only touch the transcript on state transitions (token jump,
            # working/idle flip) or first sight, never on idle ticks.
            prev_out = prev.get("out") if isinstance(prev, dict) else None
            if isinstance(prev_out, int) and out_toks < prev_out:
                prev_cache = None  # session rewound: recount from scratch
            tp = resolve_transcript_path(data, session_id)
            cache = update_cache_stats(tp, prev_cache)
            state["cache"] = cache
            if not isinstance(prev, dict) or cache != prev.get("cache"):
                dirty = True
        if dirty:
            atomic_write_text(state_file, json.dumps(state))

    maybe_sync_agyswap_quota(data, ts, windows=payload["quota_windows"])
    return render_statusline(payload, state, ts)


def _sample_render(now: float) -> str:
    sample_payload = {
        "model": {"display_name": "Orchestrator (Carbon)"},
        "agent_state": "idle",
        "context_window": {
            "total_input_tokens": 56000,
            "total_output_tokens": 6200,
            "context_window_size": 1048576,
            "used_percentage": 5.3,
        },
        "quota": {
            "gemini-5h": {"remaining_fraction": 0.99},
            "gemini-weekly": {"remaining_fraction": 0.88},
            "3p-5h": {"remaining_fraction": 0.89},
            "3p-weekly": {"remaining_fraction": 0.68},
        },
    }
    extracted = extract_payload(sample_payload)
    sample_state = {
        "out": 6200,
        "t_start": now,
        "rate": 64.5,
        "working": False,
        "step_idx": 10,
        "cache": {"idx": 10, "in": 9000, "hit": 47000, "last_in": 1200, "last_hit": 24800},
    }
    return render_statusline(extracted, sample_state, now)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agy-tps-statusline",
        description="Cross-platform status line for Antigravity CLI (agy).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read one status-line JSON payload from stdin and render it.",
    )

    sub = parser.add_subparsers(dest="subcommand")

    p_install = sub.add_parser("install", help="Configure agy settings.json statusLine")
    stack_group = p_install.add_mutually_exclusive_group()
    stack_group.add_argument(
        "--stack",
        action="store_true",
        help="Set statusLine.stack_with_default = true",
    )
    stack_group.add_argument(
        "--no-stack",
        action="store_true",
        help="Set statusLine.stack_with_default = false",
    )

    sub.add_parser("uninstall", help="Disable statusLine in agy settings.json")
    sub.add_parser("doctor", help="Show local installation and cache diagnostics")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    _configure_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "install":
        stack_opt: Optional[bool] = None
        if args.stack:
            stack_opt = True
        elif args.no_stack:
            stack_opt = False

        res = install(stack=stack_opt)
        print(f"Updated {res['settings_path']}")
        if res["backup_path"]:
            print(f"Backup  {res['backup_path']}")
        if res["created_wrapper"]:
            print(f"Wrapper {res['created_wrapper']}")
        if res["upgraded_agyswap_wrapper"]:
            print(f"Upgraded agyswap chain wrapper {res['upgraded_agyswap_wrapper']}")
        for legacy_bak in res.get("quarantined_legacy") or []:
            print(f"Moved legacy script -> {legacy_bak}")
        print(f"Command {res['command']}")
        return 0

    if args.subcommand == "uninstall":
        res = uninstall()
        if res["changed"]:
            print(f"Disabled statusLine in {res['settings_path']}")
        else:
            print(f"Settings file not found: {res['settings_path']}")
        return 0

    if args.subcommand == "doctor":
        info = collect_doctor_info()
        _safe_print(format_doctor_report(info, sample_line=_sample_render(time.time())))
        return 0

    if not args.stdin and sys.stdin.isatty():
        info = collect_doctor_info()
        _safe_print(format_doctor_report(info, sample_line=_sample_render(time.time())))
        print("\nUsage:")
        print("  agy-tps-statusline install      # configure ~/.gemini/antigravity-cli/settings.json")
        print("  agy-tps-statusline uninstall    # disable statusLine in settings.json")
        print("  agy-tps-statusline doctor       # inspect status and paths")
        return 0

    try:
        raw_input = sys.stdin.read()
    except Exception:
        raw_input = ""

    try:
        line = run_statusline_from_raw(raw_input)
        _safe_print(line)
    except Exception:
        # Never exit non-zero so agy does not disable the statusLine after 30 errors.
        _safe_print("\033[90m⚡ -- tok/s\033[0m")
    return 0
