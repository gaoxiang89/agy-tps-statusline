"""Hermetic unit tests for agy-tps-statusline."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agy_tps_statusline import paths
from agy_tps_statusline.agyswap_sync import maybe_sync_agyswap_quota
from agy_tps_statusline.cli import run_statusline_from_raw
from agy_tps_statusline.core import extract_payload, update_state
from agy_tps_statusline.installer import install, uninstall
from agy_tps_statusline.render import render_statusline, strip_ansi, visible_width
from agy_tps_statusline.transcript import (
    parse_iso_utc,
    read_latest_llm_timing,
    resolve_transcript_path,
    rewrite_antigravity_cli_path,
    update_cache_stats,
)


class StatuslineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.gemini_root = self.root / ".gemini"
        self.swap_root = self.root / ".agy-swap"
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "AGY_GEMINI_DIR": str(self.gemini_root),
                "AGYSWAP_DATA_DIR": str(self.swap_root),
                "AGY_TPS_NO_AGYSWAP": "0",
                "AGY_TPS_DEBUG": "0",
            },
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self._tmp.cleanup()

    def _write_transcript(self, session_id: str, entries: list[dict]) -> Path:
        tp = (
            paths.brain_dir()
            / paths.safe_session_id(session_id)
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        tp.parent.mkdir(parents=True, exist_ok=True)
        with open(tp, "w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item) + "\n")
        return tp

    def test_01_baseline_idle_working_jump_and_zero_inflight_writes(self) -> None:
        noop_timing = lambda _prev: (None, None)

        s0, dirty0 = update_state(None, 100, "idle", 1000.0, noop_timing)
        self.assertTrue(dirty0)
        self.assertEqual(s0["rate"], 0.0)
        self.assertFalse(s0["working"])

        # Repeated idle ticks should not mark dirty or change t_start.
        s1, dirty1 = update_state(s0, 100, "idle", 1050.0, noop_timing)
        self.assertFalse(dirty1)
        self.assertEqual(s1["t_start"], 1000.0)

        # idle -> working marks dirty and sets t_start.
        s2, dirty2 = update_state(s1, 100, "working", 1100.0, noop_timing)
        self.assertTrue(dirty2)
        self.assertTrue(s2["working"])
        self.assertEqual(s2["t_start"], 1100.0)

        # In-flight polling without token jump must have dirty == False.
        s3, dirty3 = update_state(s2, 100, "working", 1100.6, noop_timing)
        self.assertFalse(dirty3)
        self.assertEqual(s3["t_start"], 1100.0)

        # Token jump after 1.5s produces 150 / 1.5 = 100 tok/s.
        s4, dirty4 = update_state(s3, 250, "working", 1101.5, noop_timing)
        self.assertTrue(dirty4)
        self.assertAlmostEqual(s4["rate"], 100.0, places=4)

    def test_02_strips_45s_tool_execution_time_via_transcript(self) -> None:
        sid = "sess-tool-gap"
        t_step1 = 1794576000.0
        # Tool runs from t_step1 to t_step1 + 45.0; next LLM call starts at +45.0
        # and completes at +47.0 (+2.0s LLM duration) outputting 160 tokens.
        self._write_transcript(
            sid,
            [
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-13T13:20:00Z",
                    "output_tokens": 100,
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "created_at": "2026-11-13T13:20:44Z",
                },
                {
                    "step_index": 3,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-13T13:20:45Z",
                    "output_tokens": 160,
                },
            ],
        )

        tp = resolve_transcript_path({}, sid)
        self.assertIsNotNone(tp)

        prev = {
            "out": 100,
            "t_start": t_step1,
            "rate": 50.0,
            "working": True,
            "step_idx": 1,
        }
        now = parse_iso_utc("2026-11-13T13:20:47Z")
        assert now is not None

        new_state, dirty = update_state(
            prev,
            260,
            "working",
            now,
            lambda prev_idx: read_latest_llm_timing(tp, prev_idx),
        )
        self.assertTrue(dirty)
        self.assertEqual(new_state["step_idx"], 3)
        # Pure LLM duration is 2.0s (not 47.0s), so 160 / 2.0 = 80.0 tok/s.
        self.assertAlmostEqual(new_state["rate"], 80.0, places=3)

    def test_03_checkpoint_step_with_output_tokens(self) -> None:
        sid = "sess-checkpoint"
        tp = self._write_transcript(
            sid,
            [
                {
                    "step_index": 10,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-13T13:15:00Z",
                    "output_tokens": 200,
                },
                {
                    "step_index": 11,
                    "source": "SYSTEM",
                    "type": "CHECKPOINT",
                    "created_at": "2026-11-13T13:15:55Z",
                    "output_tokens": 300,
                },
            ],
        )
        ts, idx = read_latest_llm_timing(tp, 10)
        self.assertEqual(idx, 11)
        self.assertEqual(ts, parse_iso_utc("2026-11-13T13:15:55Z"))

    def test_04_transcript_path_resolution_posix_and_windows(self) -> None:
        sid = "sess-win-posix"
        tp = self._write_transcript(
            sid,
            [
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-13T13:30:00Z",
                    "output_tokens": 50,
                }
            ],
        )

        # POSIX mismatch path: ~/.gemini/antigravity/brain/<sid>/...
        posix_reported = str(
            self.gemini_root
            / "antigravity"
            / "brain"
            / sid
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        resolved_posix = resolve_transcript_path({"transcript_path": posix_reported}, sid)
        self.assertEqual(resolved_posix, tp)

        # Windows backslash path rewriting + fallback resolution
        win_reported = (
            r"C:\Users\u\.gemini\antigravity\brain"
            f"\\{sid}\\.system_generated\\logs\\transcript.jsonl"
        )
        self.assertEqual(
            rewrite_antigravity_cli_path(win_reported),
            r"C:\Users\u\.gemini\antigravity-cli\brain"
            f"\\{sid}\\.system_generated\\logs\\transcript.jsonl",
        )
        resolved_win = resolve_transcript_path({"transcript_path": win_reported}, sid)
        self.assertEqual(resolved_win, tp)

    def test_05_sub_300ms_jump_from_idle_uses_transcript_timestamp(self) -> None:
        sid = "sess-fast-idle"
        tp = self._write_transcript(
            sid,
            [
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-13T13:00:00Z",
                    "output_tokens": 100,
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-13T13:10:00Z",
                    "output_tokens": 90,
                },
            ],
        )
        t_step1 = parse_iso_utc("2026-11-13T13:00:00Z")
        t_step2 = parse_iso_utc("2026-11-13T13:10:00Z")
        assert t_step1 is not None and t_step2 is not None

        prev = {
            "out": 100,
            "t_start": t_step1,
            "rate": 40.0,
            "working": False,
            "step_idx": 1,
        }
        now = t_step2 + 1.0  # 601s after step 1, 1.0s after step 2 started
        state, dirty = update_state(
            prev,
            190,
            "idle",
            now,
            lambda prev_idx: read_latest_llm_timing(tp, prev_idx),
        )
        self.assertTrue(dirty)
        self.assertAlmostEqual(state["rate"], 90.0, places=3)

    def test_06_live_wait_indicator_and_dimmed_rate(self) -> None:
        payload = extract_payload(
            {
                "model": {"display_name": "Orchestrator (Carbon)"},
                "agent_state": "working",
                "context_window": {"total_output_tokens": 1200},
            }
        )
        state = {
            "out": 1200,
            "t_start": 1000.0,
            "rate": 64.5,
            "working": True,
            "step_idx": 3,
        }
        # Before 2s: bright green rate, no hourglass.
        early = render_statusline(payload, state, 1001.5)
        self.assertIn("\033[32m⚡ 64.5 tok/s", early)
        self.assertNotIn("⏳", early)

        # At >= 2s: dimmed gray rate + yellow hourglass wait time.
        waiting = render_statusline(payload, state, 1012.3)
        self.assertIn("\033[90m⚡ 64.5 tok/s", waiting)
        self.assertIn("\033[33m⏳ 12s", waiting)

    def test_07_responsive_truncation_across_terminal_widths(self) -> None:
        base_data = {
            "model": {"display_name": "Orchestrator (Carbon)"},
            "agent_state": "idle",
            "context_window": {
                "total_input_tokens": 120000,
                "total_output_tokens": 6200,
                "context_window_size": 1048576,
                "used_percentage": 11.4,
            },
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.90},
                "gemini-weekly": {"remaining_fraction": 0.80},
                "3p-5h": {"remaining_fraction": 0.85},
                "3p-weekly": {"remaining_fraction": 0.40},
            },
        }
        state = {"out": 6200, "t_start": 1000.0, "rate": 52.0, "working": False, "step_idx": 5}

        for width in (180, 105, 70):
            data = {**base_data, "terminal_width": width}
            line = render_statusline(extract_payload(data), state, 1005.0)
            self.assertLessEqual(
                visible_width(line),
                width,
                f"Exceeded width {width}: {strip_ansi(line)}",
            )

        wide_plain = strip_ansi(
            render_statusline(extract_payload({**base_data, "terminal_width": 180}), state, 1005.0)
        )
        self.assertIn("gem", wide_plain)
        self.assertIn("3p", wide_plain)
        self.assertIn("ctx 120.0k/1.0M", wide_plain)

        narrow_plain = strip_ansi(
            render_statusline(extract_payload({**base_data, "terminal_width": 70}), state, 1005.0)
        )
        self.assertIn("52.0 tok/s", narrow_plain)
        self.assertNotIn("gem", narrow_plain)

    def test_08_agyswap_quota_sync_and_mtime_throttle(self) -> None:
        self.swap_root.mkdir(parents=True, exist_ok=True)
        payload_data = {
            "email": "tester@example.com",
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.75, "reset_time": "2026-11-13T18:00:00Z"},
                "3p-weekly": {"remaining_fraction": 0.50, "reset_time": "2026-11-19T12:00:00Z"},
            },
        }

        t0 = 1794577000.0
        self.assertTrue(maybe_sync_agyswap_quota(payload_data, t0))

        qpath = paths.agyswap_quota_path()
        self.assertTrue(qpath.is_file())
        saved = json.loads(qpath.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema"], 2)
        entry = saved["accounts"]["tester@example.com"]
        self.assertEqual(entry["windows"]["gemini-5h"]["used_pct"], 25.0)
        self.assertEqual(entry["windows"]["3p-weekly"]["used_pct"], 50.0)

        # Update file mtime to t0 and verify 10s throttle skips write.
        os.utime(qpath, (t0, t0))
        changed_payload = {
            "email": "tester@example.com",
            "quota": {"gemini-5h": {"remaining_fraction": 0.50}},
        }
        self.assertFalse(maybe_sync_agyswap_quota(changed_payload, t0 + 5.0))

        # After >10s, changed quota writes.
        self.assertTrue(maybe_sync_agyswap_quota(changed_payload, t0 + 12.0))
        saved2 = json.loads(qpath.read_text(encoding="utf-8"))
        self.assertEqual(
            saved2["accounts"]["tester@example.com"]["windows"]["gemini-5h"]["used_pct"],
            50.0,
        )

    def test_09_install_uninstall_and_upgrade_agyswap_wrapper(self) -> None:
        cfg_path = paths.settings_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps(
                {
                    "model": "Orchestrator (Carbon)",
                    "toolPermission": "always-proceed",
                    "statusLine": {"type": "", "command": "agy-statusline", "enabled": True},
                }
            ),
            encoding="utf-8",
        )

        bin_dir = self.root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        agyswap_wrapper = bin_dir / "agy-statusline"
        agyswap_wrapper.write_text(
            '#!/bin/sh\nexec "/usr/local/bin/agyswap" statusline --chain "agy-tps-statusline"\n',
            encoding="utf-8",
        )

        res = install(
            stack=True,
            bin_dir=bin_dir,
            which_fn=lambda name: str(bin_dir / name) if name == "agy-statusline" else None,
        )
        self.assertTrue(cfg_path.with_name("settings.json.bak").is_file())
        updated = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["model"], "Orchestrator (Carbon)")
        self.assertEqual(updated["toolPermission"], "always-proceed")
        self.assertTrue(updated["statusLine"]["enabled"])
        self.assertTrue(updated["statusLine"]["stack_with_default"])
        self.assertEqual(updated["statusLine"]["type"], "command")
        self.assertEqual(res["upgraded_agyswap_wrapper"], agyswap_wrapper)

        self.assertTrue(agyswap_wrapper.with_name("agy-statusline.bak").is_file())
        new_wrapper_text = agyswap_wrapper.read_text(encoding="utf-8")
        self.assertNotIn("agyswap", new_wrapper_text)
        self.assertIn("agy-tps-statusline", new_wrapper_text)

        # Legacy scripts inside ~/.gemini/antigravity-cli/bin and ~/.gemini/bin are quarantined.
        legacy_bin = paths.agy_cli_dir() / "bin"
        legacy_bin.mkdir(parents=True, exist_ok=True)
        legacy_bat = legacy_bin / "agy-tps-statusline.bat"
        legacy_py = legacy_bin / "agy-tps-statusline.py"
        legacy_bat.write_text("@echo off\r\npython agy-tps-statusline.py\r\n", encoding="utf-8")
        legacy_py.write_text("import sys; sys.stdin.read()\n", encoding="utf-8")

        gemini_bin = paths.gemini_dir() / "bin"
        gemini_bin.mkdir(parents=True, exist_ok=True)
        gemini_bat = gemini_bin / "agy-tps-statusline.bat"
        gemini_bat.write_text("@echo off\r\npython agy-tps-statusline.py\r\n", encoding="utf-8")

        appdata_bin = self.root / "AppData" / "Local" / "agy" / "bin"
        appdata_bin.mkdir(parents=True, exist_ok=True)
        appdata_bat = appdata_bin / "agy-tps-statusline.bat"
        appdata_bat.write_text("@echo off\r\npython agy-tps-statusline.py\r\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "AppData" / "Local")}):
            res2 = install(bin_dir=bin_dir, which_fn=lambda _n: None)
        self.assertFalse(legacy_bat.exists())
        self.assertFalse(legacy_py.exists())
        self.assertFalse(gemini_bat.exists())
        self.assertFalse(appdata_bat.exists())
        self.assertTrue((legacy_bin / "agy-tps-statusline.bat.legacy.bak").is_file())
        self.assertTrue((legacy_bin / "agy-tps-statusline.py.legacy.bak").is_file())
        self.assertTrue((gemini_bin / "agy-tps-statusline.bat.legacy.bak").is_file())
        self.assertTrue((appdata_bin / "agy-tps-statusline.bat.legacy.bak").is_file())
        self.assertEqual(len(res2["quarantined_legacy"]), 4)

        uninstall()
        after_uninstall = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertFalse(after_uninstall["statusLine"]["enabled"])
        self.assertEqual(after_uninstall["model"], "Orchestrator (Carbon)")

    def test_10_end_to_end_raw_json_and_malformed_input(self) -> None:
        raw = json.dumps(
            {
                "session_id": "e2e-1",
                "agent_state": "working",
                "model": {"display_name": "Orchestrator (Carbon)"},
                "context_window": {
                    "total_input_tokens": 5000,
                    "total_output_tokens": 100,
                    "context_window_size": 100000,
                },
            }
        )
        line1 = run_statusline_from_raw(raw, now=1000.0)
        self.assertIn("-- tok/s", strip_ansi(line1))
        # Debug dump must be disabled by default.
        self.assertFalse(paths.debug_input_path().exists())

        # Second call with token jump
        raw2 = json.dumps(
            {
                "session_id": "e2e-1",
                "agent_state": "idle",
                "model": {"display_name": "Orchestrator (Carbon)"},
                "context_window": {
                    "total_input_tokens": 5000,
                    "total_output_tokens": 200,
                    "context_window_size": 100000,
                },
            }
        )
        line2 = run_statusline_from_raw(raw2, now=1002.0)
        self.assertIn("50.0 tok/s", strip_ansi(line2))

        # Malformed input never raises.
        bad = run_statusline_from_raw("{not-json", now=1003.0)
        self.assertIn("-- tok/s", strip_ansi(bad))

    def test_11_cache_hit_rate_incremental_and_render(self) -> None:
        sid = "cache-1"
        base = [
            {"step_index": 0, "type": "USER_INPUT", "content": "hi"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-01-01T00:00:00Z",
             "input_tokens": 10000, "cache_read_tokens": 0, "output_tokens": 100},
            {"step_index": 3, "type": "PLANNER_RESPONSE", "created_at": "2026-01-01T00:00:05Z",
             "input_tokens": 1000, "cache_read_tokens": 9000, "output_tokens": 100},
        ]
        tp = self._write_transcript(sid, base)

        # Full scan on first sight.
        c1 = update_cache_stats(tp, None)
        self.assertEqual(c1, {"idx": 3, "in": 11000, "hit": 9000, "last_in": 1000, "last_hit": 9000})
        # No new steps: unchanged.
        self.assertEqual(update_cache_stats(tp, c1), c1)

        # Incremental add.
        base.append({"step_index": 5, "type": "PLANNER_RESPONSE", "created_at": "2026-01-01T00:00:09Z",
                     "input_tokens": 500, "cache_read_tokens": 19500, "output_tokens": 50})
        self._write_transcript(sid, base)
        c2 = update_cache_stats(tp, c1)
        self.assertEqual(c2, {"idx": 5, "in": 11500, "hit": 28500, "last_in": 500, "last_hit": 19500})

        # Tail window that misses already-counted steps falls back to a full scan.
        c3 = update_cache_stats(tp, {"idx": 1, "in": 10000, "hit": 0, "last_in": 0, "last_hit": 0}, tail_bytes=10)
        self.assertEqual(c3, c2)

        state = {"out": 250, "t_start": 0.0, "rate": 10.0, "working": False, "step_idx": 5, "cache": c2}
        payload = extract_payload({"model": {"display_name": "M"}})
        plain = strip_ansi(render_statusline(payload, state, 0.0))
        self.assertIn("cache 71% (last 98%)", plain)

        # End-to-end via CLI: cache stats persisted in session state.
        raw = json.dumps({"session_id": sid, "agent_state": "idle",
                          "context_window": {"total_output_tokens": 250}})
        line = strip_ansi(run_statusline_from_raw(raw, now=2000.0))
        self.assertIn("cache 71%", line)
        saved = json.loads((paths.tps_state_dir() / f"{sid}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["cache"]["hit"], 28500)


if __name__ == "__main__":
    unittest.main()
