"""Cross-platform installer, uninstaller, and diagnostics for agy-tps-statusline."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from agy_tps_statusline import __version__, paths
from agy_tps_statusline.agyswap_sync import is_agyswap_installed
from agy_tps_statusline.core import atomic_write_json, atomic_write_text, load_json


def _default_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _is_dir_on_path(directory: Path) -> bool:
    path_env = os.environ.get("PATH", "")
    if not path_env:
        return False
    try:
        target = directory.resolve()
    except OSError:
        target = directory
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        try:
            if Path(entry).expanduser().resolve() == target:
                return True
        except OSError:
            continue
    return False


def _write_standalone_wrapper(bin_dir: Path, python_exe: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        target = bin_dir / "agy-tps-statusline.cmd"
        body = f'@echo off\r\n"{python_exe}" -m agy_tps_statusline %*\r\n'
        atomic_write_text(target, body)
    else:
        target = bin_dir / "agy-tps-statusline"
        body = f'#!/bin/sh\nexec "{python_exe}" -m agy_tps_statusline "$@"\n'
        atomic_write_text(target, body)
        target.chmod(0o755)
    return target


_LEGACY_BIN_NAMES = (
    "agy-tps-statusline",
    "agy-tps-statusline.py",
    "agy-tps-statusline.bat",
    "agy-tps-statusline.cmd",
)


def _quarantine_legacy_bin_files(
    current_exe: Optional[Path] = None,
    which_fn: Callable[[str], Optional[str]] = shutil.which,
) -> list[Path]:
    """Move legacy scripts in agy bin dirs out of PATH."""
    legacy_dirs: list[Path] = [
        paths.agy_cli_dir() / "bin",
        paths.gemini_dir() / "bin",
    ]
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        legacy_dirs.append(Path(local_app_data) / "agy" / "bin")
    agy_exe = which_fn("agy")
    if agy_exe:
        try:
            agy_dir = Path(agy_exe).resolve().parent
            if agy_dir not in legacy_dirs:
                legacy_dirs.append(agy_dir)
        except OSError:
            pass

    current_resolved: Optional[Path] = None
    if current_exe is not None:
        try:
            current_resolved = current_exe.resolve()
        except OSError:
            current_resolved = current_exe

    moved: list[Path] = []
    seen_dirs: set[Path] = set()
    for legacy_dir in legacy_dirs:
        try:
            resolved_dir = legacy_dir.resolve()
        except OSError:
            resolved_dir = legacy_dir
        if resolved_dir in seen_dirs or not legacy_dir.is_dir():
            continue
        seen_dirs.add(resolved_dir)
        for name in _LEGACY_BIN_NAMES:
            candidate = legacy_dir / name
            try:
                if not candidate.is_file():
                    continue
                if current_resolved is not None and candidate.resolve() == current_resolved:
                    continue
                backup = candidate.with_name(f"{candidate.name}.legacy.bak")
                os.replace(candidate, backup)
                moved.append(backup)
            except OSError:
                continue
    return moved


def resolve_command_for_install(
    bin_dir: Optional[Path] = None,
    which_fn: Callable[[str], Optional[str]] = shutil.which,
) -> tuple[str, Optional[Path]]:
    """Resolve the command string for ``settings.json`` and write a wrapper if needed."""
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    running_own_bin = bool(
        argv0
        and argv0.name.lower().startswith("agy-tps-statusline")
        and argv0.exists()
    )

    found = which_fn("agy-tps-statusline")
    if found:
        if running_own_bin and argv0 is not None:
            try:
                if Path(found).resolve() != argv0.resolve():
                    return str(argv0.resolve()), None
            except OSError:
                pass
        return "agy-tps-statusline", None

    if running_own_bin and argv0 is not None:
        try:
            return str(argv0.resolve()), None
        except OSError:
            return str(argv0), None

    target_dir = bin_dir or _default_bin_dir()
    wrapper = _write_standalone_wrapper(target_dir, sys.executable)
    if _is_dir_on_path(target_dir):
        return wrapper.name, wrapper
    return str(wrapper), wrapper


def _upgrade_agyswap_chain_wrapper(
    bin_dir: Path,
    command: str,
    which_fn: Callable[[str], Optional[str]],
) -> Optional[Path]:
    """Bypass an existing agyswap ``agy-statusline`` wrapper that chains us."""
    candidates: list[Path] = []
    which_sl = which_fn("agy-statusline")
    if which_sl:
        candidates.append(Path(which_sl))

    for name in ("agy-statusline.cmd", "agy-statusline"):
        p = bin_dir / name
        if p not in candidates:
            candidates.append(p)

    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if "agyswap" not in text or "agy-tps-statusline" not in text:
            continue

        backup = candidate.with_name(f"{candidate.name}.bak")
        if not backup.exists():
            try:
                shutil.copy2(candidate, backup)
            except OSError:
                pass

        if candidate.suffix.lower() in (".cmd", ".bat"):
            new_body = f'@echo off\r\n"{command}" %*\r\n'
            atomic_write_text(candidate, new_body)
        else:
            new_body = f'#!/bin/sh\nexec "{command}" "$@"\n'
            atomic_write_text(candidate, new_body)
            try:
                candidate.chmod(0o755)
            except OSError:
                pass
        return candidate

    return None


def install(
    *,
    stack: Optional[bool] = None,
    bin_dir: Optional[Path] = None,
    which_fn: Callable[[str], Optional[str]] = shutil.which,
) -> dict[str, Any]:
    """Configure ``settings.json`` and upgrade any agyswap chain wrapper."""
    target_bin_dir = bin_dir or _default_bin_dir()
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    quarantined_legacy = _quarantine_legacy_bin_files(argv0, which_fn=which_fn)

    command, created_wrapper = resolve_command_for_install(
        bin_dir=target_bin_dir,
        which_fn=which_fn,
    )

    cfg_path = paths.settings_path()
    backup_path: Optional[Path] = None
    existing = load_json(cfg_path) or {}
    if cfg_path.exists():
        backup_path = cfg_path.with_name(f"{cfg_path.name}.bak")
        try:
            shutil.copy2(cfg_path, backup_path)
        except OSError:
            backup_path = None

    prev_sl = existing.get("statusLine")
    sl_block: dict[str, Any] = dict(prev_sl) if isinstance(prev_sl, dict) else {}
    sl_block["type"] = "command"
    sl_block["command"] = command
    sl_block["enabled"] = True
    if stack is not None:
        sl_block["stack_with_default"] = stack

    existing["statusLine"] = sl_block
    if not atomic_write_json(cfg_path, existing, indent=2):
        raise OSError(f"Failed to write settings file: {cfg_path}")

    upgraded_wrapper = _upgrade_agyswap_chain_wrapper(
        target_bin_dir,
        command,
        which_fn=which_fn,
    )

    return {
        "command": command,
        "created_wrapper": created_wrapper,
        "settings_path": cfg_path,
        "backup_path": backup_path,
        "upgraded_agyswap_wrapper": upgraded_wrapper,
        "quarantined_legacy": quarantined_legacy,
        "statusLine": sl_block,
    }


def uninstall() -> dict[str, Any]:
    """Disable ``statusLine`` in ``settings.json`` while preserving all other settings."""
    cfg_path = paths.settings_path()
    if not cfg_path.exists():
        return {"settings_path": cfg_path, "changed": False}

    existing = load_json(cfg_path)
    if existing is None:
        raise OSError(f"Existing settings file is not valid JSON: {cfg_path}")

    sl = existing.get("statusLine")
    if isinstance(sl, dict):
        sl["enabled"] = False
        changed = True
    else:
        existing["statusLine"] = {"type": "command", "command": "", "enabled": False}
        changed = True

    if not atomic_write_json(cfg_path, existing, indent=2):
        raise OSError(f"Failed to write settings file: {cfg_path}")

    return {"settings_path": cfg_path, "changed": changed}


def collect_doctor_info(which_fn: Callable[[str], Optional[str]] = shutil.which) -> dict[str, Any]:
    """Gather diagnostic information about the local installation."""
    cfg_path = paths.settings_path()
    settings = load_json(cfg_path) or {}
    sl = settings.get("statusLine") if isinstance(settings.get("statusLine"), dict) else {}
    state_dir = paths.tps_state_dir()
    try:
        state_files = list(state_dir.glob("*.json")) if state_dir.is_dir() else []
    except OSError:
        state_files = []

    return {
        "version": __version__,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "executable": which_fn("agy-tps-statusline") or "",
        "settings_path": str(cfg_path),
        "settings_exists": cfg_path.is_file(),
        "statusLine": sl,
        "state_dir": str(state_dir),
        "state_count": len(state_files),
        "agyswap_detected": is_agyswap_installed(),
        "agyswap_quota_path": str(paths.agyswap_quota_path()),
        "brain_dir": str(paths.brain_dir()),
        "brain_exists": paths.brain_dir().is_dir(),
    }


def format_doctor_report(info: dict[str, Any], sample_line: str = "") -> str:
    """Format diagnostic info as human-readable text."""
    sl = info.get("statusLine") or {}
    sl_desc = json.dumps(sl, ensure_ascii=False) if sl else "(not configured)"
    lines = [
        f"agy-tps-statusline {info.get('version')}",
        f"  platform        : {info.get('platform')} (Python {info.get('python')})",
        f"  which           : {info.get('executable') or '(not on PATH)'}",
        f"  settings.json   : {info.get('settings_path')} ({'found' if info.get('settings_exists') else 'missing'})",
        f"  statusLine      : {sl_desc}",
        f"  tps state dir   : {info.get('state_dir')} ({info.get('state_count')} session file(s))",
        f"  brain dir       : {info.get('brain_dir')} ({'found' if info.get('brain_exists') else 'missing'})",
        f"  agyswap sync    : {'enabled' if info.get('agyswap_detected') else 'not detected'} ({info.get('agyswap_quota_path')})",
    ]
    if sample_line:
        lines.append(f"  sample output   : {sample_line}")
    return "\n".join(lines)
