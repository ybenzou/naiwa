"""Local diagnostics distinguish configuration from actual IDE hook execution."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from naiwa.bus import atomic_json, runtime_dir
from naiwa.install import CODEX_EVENTS, CURSOR_EVENTS, _is_ours, _load, default_codex_path, default_cursor_path
from naiwa.hosts import coverage


def runtime_kind() -> str:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return "wsl"
    return "windows" if os.name == "nt" else platform.system().lower()


def _version(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        return ""
    command = [executable, "--version"]
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", executable, "--version"]
    try:
        run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=8, check=False, creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (run.stdout or "").splitlines()[0] if run.stdout else ""


def _codex_extensions() -> list[dict]:
    found = []
    for name in (".vscode", ".vscode-insiders", ".cursor", ".vscode-server"):
        root = Path.home() / name / "extensions"
        if not root.exists():
            continue
        for package in root.glob("openai.chatgpt-*/package.json"):
            try:
                data = json.loads(package.read_text(encoding="utf-8"))
                found.append({"editor": name, "path": str(package.parent), "version": data.get("version", "")})
            except (OSError, ValueError):
                pass
    return found


def collect(root: Path | None = None) -> dict:
    return {
        "runtime_kind": runtime_kind(), "platform": platform.platform(),
        "python": platform.python_version(), "python_executable": sys.executable,
        "codex_home": str(default_codex_path().parent), "data_dir": str(runtime_dir(root)),
        "cursor_hooks": str(default_cursor_path()), "codex_hooks": str(default_codex_path()),
        "cursor_version": _version("cursor"), "code_version": _version("code"),
        "codex_extensions": _codex_extensions(),
        "note": "Configuration and extension discovery do not prove hooks executed. Only fresh IDE-origin records do.",
    }


def write_environment(root: Path | None = None) -> Path:
    path = runtime_dir(root) / "environment.json"
    atomic_json(path, collect(root))
    return path


def diagnostics(root: Path | None = None) -> list[dict]:
    rows = []
    for name in ("probe-keys.prev.jsonl", "probe-keys.jsonl"):
        try:
            data = (runtime_dir(root) / name).read_bytes()
        except FileNotFoundError:
            continue
        for line in data[:data.rfind(b"\n")+1].splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except (ValueError, UnicodeError):
                pass
    return rows


def hook_health() -> dict:
    health = {}
    for source, path, events in (("cursor", default_cursor_path(), CURSOR_EVENTS),
                                 ("codex", default_codex_path(), CODEX_EVENTS)):
        try:
            config = _load(path, source)
            present = []
            for name, entries in config.get("hooks", {}).items():
                handlers = entries if source == "cursor" else [h for g in entries for h in g.get("hooks", [])]
                if any(_is_ours(h.get("command")) for h in handlers):
                    present.append(name)
            health[source] = {"path": str(path), "installed": sorted(present),
                              "missing": sorted(set(events)-set(present))}
        except (ValueError, OSError) as exc:
            health[source] = {"path": str(path), "error": str(exc)}
    return health


def report(root: Path | None = None, *, since: str = "") -> dict:
    rows = diagnostics(root)
    observed = {"cursor": set(), "codex": set()}
    skipped = 0
    failures = {}
    for row in rows:
        if since and str(row.get("ts", "")) < since:
            continue
        source = row.get("source")
        if source in observed and row.get("input_status") not in {None, "ok"}:
            key = source+":"+row["input_status"]
            failures[key] = failures.get(key, 0)+1
        if row.get("observed") and source in observed:
            if source == "cursor" or row.get("origin") == "vscode":
                observed[source].add(row.get("hook_event_name", ""))
        elif source == "codex":
            skipped += 1
    try:
        reader = json.loads((runtime_dir(root)/"reader-health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reader = None
    return {"hooks": hook_health(), "observed_events": {s: sorted(v) for s, v in observed.items()},
            "codex_instances": coverage(rows),
            "input_failures": failures, "desktop_reader": reader,
            "skipped_non_ide_codex_records": skipped,
            "codex_trust": "Review non-managed hooks with /hooks; trust is not inferred from the file existing."}


def watch(root: Path | None, seconds: int) -> dict:
    from datetime import datetime, timezone
    since = datetime.now(timezone.utc).isoformat()
    print("在 VS Code Codex 扩展或 Cursor 中发起真实回合；这里只等待新钩子，不发送 GPT 请求。", flush=True)
    deadline = time.monotonic()+seconds
    last = None
    while time.monotonic() < deadline:
        result = report(root, since=since)
        if result["observed_events"] != last:
            print(json.dumps(result["observed_events"], ensure_ascii=False), flush=True)
            last = result["observed_events"]
        time.sleep(min(0.25, max(0, deadline-time.monotonic())))
    return report(root, since=since)
