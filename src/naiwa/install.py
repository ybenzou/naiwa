"""Validated, atomic merges. Invalid existing configs are never overwritten."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from naiwa.bus import atomic_json, runtime_dir

CURSOR_EVENTS = (
    "beforeSubmitPrompt", "preToolUse", "postToolUse", "postToolUseFailure",
    "subagentStart", "subagentStop", "stop", "afterAgentResponse", "afterAgentThought", "sessionEnd",
)
CODEX_EVENTS = (
    "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest",
    "SubagentStart", "SubagentStop", "Stop", "Interrupt", "SessionEnd", "PreCompact", "PostCompact",
)
MARKER = " -m naiwa.hook --source "


def observe_script() -> Path:
    return Path(__file__).resolve().parents[2] / "hooks" / "observe.py"


def command_for(source: str, python: str, event: str = "", data_dir: Path | None = None) -> str:
    directory = runtime_dir(data_dir).resolve()
    if source not in {"cursor", "codex"}:
        raise ValueError("Unknown hook source")
    if event and event not in CURSOR_EVENTS + CODEX_EVENTS:
        raise ValueError("Unknown hook event")
    if os.name == "nt":
        # Cursor's Windows backend is PowerShell and supplies a payload tempfile.
        # Codex retains the cmd-compatible definition so existing trust is stable.
        if any(c in str(value) for value in (python, directory) for c in '\r\n"$`%'):
            raise ValueError("Windows hook paths cannot contain quotes, %, $, backticks or line breaks")
        if source == "cursor":
            wrapper = Path(__file__).with_name("assets")/"cursor-input.ps1"
            return f'& "{wrapper.resolve()}" -PythonExe "{python}" -DataDir "{directory}" -HookEvent "{event}"'
        command = f'cmd.exe /d /c call "{python}" -m naiwa.hook --source {source} --data-dir "{directory}"'
    else:
        import shlex
        command = f'{shlex.quote(python)} -m naiwa.hook --source {source} --data-dir {shlex.quote(str(directory))}'
    if event:
        command += f" --event {event}"
    if source == "codex":
        command += " --ide-only"
    return command


def _is_ours(command: object) -> bool:
    if not isinstance(command, str):
        return False
    normalized = command.replace("\\", "/")
    return MARKER in command or "naiwa/hooks/observe.py" in normalized or "/naiwa/assets/cursor-input.ps1" in normalized


def _load(path: Path, source: str = "") -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Cannot merge invalid hooks config: {path}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("hooks", {}), dict):
        raise ValueError(f"hooks must be an object: {path}")
    for name, entries in raw.get("hooks", {}).items():
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError(f"Invalid entries for {name}: {path}")
        if source == "codex":
            for group in entries:
                handlers = group.get("hooks", [])
                if not isinstance(handlers, list) or any(not isinstance(h, dict) for h in handlers):
                    raise ValueError(f"Invalid handlers for {name}: {path}")
    if source == "cursor" and raw.get("version", 1) != 1:
        raise ValueError(f"Unsupported Cursor hook version: {path}")
    return raw


def _backup(path: Path, backup_dir: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    target = backup_dir / f"{path.parent.name}-{digest}-{stamp}.before-naiwa.json"
    shutil.copy2(path, target)
    return target


def _without_ours(data: dict, source: str) -> dict:
    hooks = data.setdefault("hooks", {})
    for event, entries in list(hooks.items()):
        if source == "cursor":
            kept = [item for item in entries if not _is_ours(item.get("command"))]
        else:
            kept = []
            for group in entries:
                original = group.get("hooks", [])
                handlers = [h for h in original if not _is_ours(h.get("command"))]
                # Preserve unknown/empty groups owned by the user, remove only our empty groups.
                if handlers or handlers == original:
                    kept.append({**group, "hooks": handlers} if "hooks" in group else group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return data


def preview_install(path: Path, source: str, python: str, data_dir: Path | None = None) -> dict:
    data = _without_ours(_load(path, source), source)
    if source == "cursor":
        data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    for event in CURSOR_EVENTS if source == "cursor" else CODEX_EVENTS:
        handler = {"command": command_for(source, python, event, data_dir),
                   "timeout": 2 if event == "Interrupt" else 3}
        if source == "cursor":
            handler["failClosed"] = False
            hooks.setdefault(event, []).append(handler)
        else:
            hooks.setdefault(event, []).append({"hooks": [{"type": "command", **handler}]})
    return data


def _install(path: Path, source: str, python: str, backup_dir: Path, data_dir: Path | None) -> None:
    data = preview_install(path, source, python, data_dir)
    if path.exists() and data == _load(path, source):
        return
    _backup(path, backup_dir)
    atomic_json(path, data)


def install_cursor(path: Path, python: str, backup_dir: Path, data_dir: Path | None = None) -> None:
    _install(path, "cursor", python, backup_dir, data_dir)


def install_codex(path: Path, python: str, backup_dir: Path, data_dir: Path | None = None) -> None:
    _install(path, "codex", python, backup_dir, data_dir)


def _uninstall(path: Path, source: str) -> None:
    if not path.exists():
        return
    original = _load(path, source)
    data = _without_ours(json.loads(json.dumps(original)), source)
    if data != original:
        _backup(path, default_backup_dir())
        atomic_json(path, data)


def uninstall_cursor(path: Path) -> None:
    _uninstall(path, "cursor")


def uninstall_codex(path: Path) -> None:
    _uninstall(path, "codex")


def default_cursor_path() -> Path:
    return Path.home() / ".cursor" / "hooks.json"


def default_codex_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


def default_backup_dir() -> Path:
    return runtime_dir() / "backups"
