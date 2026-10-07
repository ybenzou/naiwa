"""Standard-library-only Cursor collector. Deployed as a small archive, never with Qt."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import sqlite3
import sys
import time
import uuid
import tempfile
import io
from urllib.parse import unquote, urlsplit

from naiwa.adapt import PENDING_CURSOR, _id, adapt, recover_cursor_identity
from naiwa.allow import allow_response
from naiwa.hook_input import read_metadata
from naiwa.schema import BusEvent, token

HOOKS = ("beforeSubmitPrompt", "preToolUse", "postToolUse", "postToolUseFailure",
         "subagentStart", "subagentStop", "stop", "afterAgentResponse",
         "afterAgentThought", "sessionEnd", "preCompact")
SKIP = {".cache", ".git", ".local", ".npm", "node_modules", ".venv"}
WORKSPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def journal(root):
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    connection = sqlite3.connect(str(root/"events.sqlite3"), timeout=1)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=1000")
    connection.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT OR IGNORE INTO meta VALUES ('epoch', ?)", (uuid.uuid4().hex,))
    connection.commit()
    os.chmod(root/"events.sqlite3", 0o600)
    return connection


def canonical_workspace(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return ""
    path = value.strip().replace("\\", "/")
    if "://" in path:
        try:
            uri = urlsplit(path)
        except ValueError:
            return ""
        if uri.scheme != "vscode-remote":
            return ""
        path = unquote(uri.path)
    if not path.startswith("/"):
        return ""
    return posixpath.normpath(path)


def load_allow(root):
    try:
        data = json.loads((Path(root)/"allow.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return ""
    value = data.get("root") if isinstance(data, dict) else ""
    return canonical_workspace(value) if isinstance(value, str) else ""


def matches_allow(payload, allowed):
    if not allowed:
        return False
    roots = payload.get("workspace_roots") if isinstance(payload, dict) else None
    candidates = roots[:32] if isinstance(roots, list) else []
    if not candidates and isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
        candidates = [payload["cwd"]]
    return any(canonical_workspace(value) == allowed for value in candidates)


def active_journal_sessions(root):
    """Open Cursor sessions already in this journal. Does not read transcripts."""
    from datetime import datetime, timezone
    from naiwa.phase import ACTIVE, PhaseMachine
    connection = journal(root)
    try:
        rows = connection.execute(
            "SELECT data FROM events ORDER BY seq DESC LIMIT 256").fetchall()
        rows.reverse()
    finally:
        connection.close()
    machine = PhaseMachine()
    for (data,) in rows:
        try:
            machine.apply(BusEvent.from_dict(json.loads(data)))
        except (ValueError, KeyError, TypeError):
            continue
    now = datetime.now(timezone.utc).timestamp()
    return [turn.session_id for turn in machine.turns.values()
            if turn.source == "cursor" and turn.session_id != PENDING_CURSOR and not turn.disconnected
            and machine._pose(turn, now) in ACTIVE]


def observe(root, name, stream, *, require_allow=False):
    response = allow_response("cursor", name)
    recovered = "none"
    try:
        payload, status, size = read_metadata(stream)
        payload.setdefault("hook_event_name", name)
        response = allow_response("cursor", payload.get("hook_event_name", name))
        if require_allow and not matches_allow(payload, load_allow(root)):
            event = None
            save_json(root/"last-hook.json", {"event": token(payload.get("hook_event_name", name)),
                      "input_status": status, "input_bytes": size, "written": False,
                      "remote_workspace": os.environ.get("CURSOR_CODE_REMOTE") == "true",
                      "workspace_name": "", "recovered": "none",
                      "ts": datetime.now(timezone.utc).isoformat()})
            return response
        try:
            active = [] if _id(payload, "conversation_id", "session_id") else active_journal_sessions(root)
        except Exception:
            active = []
        payload, recovered = recover_cursor_identity(
            payload, os.environ.get("CURSOR_TRANSCRIPT_PATH", ""), active)
        event = adapt("cursor", payload)
        if event is not None and require_allow and not matches_allow(payload, load_allow(root)):
            event = None
        written = False
        if event is not None:
            with journal(root) as connection:
                connection.execute("INSERT INTO events(data) VALUES (?)",
                                   (json.dumps(replace(event, origin="cursor-ssh").to_dict(), ensure_ascii=False),))
                last = connection.execute("SELECT MAX(seq) FROM events").fetchone()[0]
                if last % 128 == 0:
                    connection.execute("DELETE FROM events WHERE seq <= ?", (last-20000,))
            connection.close()
            written = True
        save_json(root/"last-hook.json", {"event": token(payload.get("hook_event_name", name)),
                  "input_status": status, "input_bytes": size, "written": written,
                  "remote_workspace": os.environ.get("CURSOR_CODE_REMOTE") == "true",
                  "workspace_name": event.workspace_name if event else "",
                  "recovered": recovered,
                  "ts": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass
    return response


def merge_hooks(existing, bundle, python):
    if not isinstance(existing, dict) or existing.get("version", 1) != 1:
        raise ValueError("Invalid existing Cursor hook configuration")
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Invalid existing hooks")
    for event, entries in hooks.items():
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise ValueError("Invalid existing hook entries")
    result.setdefault("version", 1)
    marker = f"{shlex.quote(str(bundle))} hook --event "
    for event in HOOKS:
        command = f"{shlex.quote(python)} {shlex.quote(str(bundle))} hook --event {event}"
        entries = [entry for entry in hooks.get(event, []) if marker not in str(entry.get("command", ""))]
        hooks[event] = entries+[{"command": command, "timeout": 3, "failClosed": False}]
    return result


def remove_hooks(existing, bundle):
    if not isinstance(existing, dict) or existing.get("version", 1) != 1:
        raise ValueError("Invalid existing Cursor hook configuration")
    result = json.loads(json.dumps(existing))
    hooks = result.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Invalid existing hooks")
    marker = str(bundle).replace("\\", "/")
    for event, entries in list(hooks.items()):
        if not isinstance(entries, list):
            raise ValueError("Invalid existing hook entries")
        hooks[event] = [entry for entry in entries
                        if marker not in str(entry.get("command", "")).replace("\\", "/")]
    return result


def load_hooks(path):
    """An empty Cursor hooks file means there are no hooks yet. Invalid text is left untouched."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid existing Cursor hook configuration")
    if data is None:
        return {}
    return data


def install(root, bundle):
    path = Path.home()/".cursor"/"hooks.json"
    existing = load_hooks(path)
    planned = merge_hooks(existing, bundle, sys.executable)
    if planned != existing:
        if path.exists():
            backup = root/"backups"/("cursor-hooks-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex[:8]+".json")
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(path.read_bytes())
            os.chmod(backup, 0o600)
        save_json(path, planned)


def uninstall(root, bundle):
    path = Path.home()/".cursor"/"hooks.json"
    if not path.exists():
        return
    existing = load_hooks(path)
    planned = remove_hooks(existing, bundle)
    if planned != existing:
        backup = root/"backups"/("cursor-hooks-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex[:8]+".json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(path.read_bytes())
        os.chmod(backup, 0o600)
        save_json(path, planned)


def find_named(workspace):
    home = Path.home().resolve()
    found = []
    for current, dirs, _files in os.walk(home, followlinks=False):
        here = Path(current).resolve()
        depth = 0 if here == home else len(here.relative_to(home).parts)
        dirs[:] = [name for name in dirs if name not in SKIP and not name.startswith(".")]
        if depth >= 4:
            dirs.clear()
            continue
        if workspace in dirs:
            found.append(str((here/workspace).resolve()))
    return found


def accepted_root(explicit, workspace):
    path = Path(explicit).expanduser().resolve()
    home = Path.home().resolve()
    if path != home and home not in path.parents:
        raise ValueError("Root is outside the login home")
    if not path.is_dir() or path.name != workspace:
        raise ValueError("Root is not the named workspace directory")
    return str(path)


def prepare(root, workspace, explicit, bundle):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    if not WORKSPACE.fullmatch(workspace or ""):
        print(json.dumps({"kind": "error", "reason": "workspace"}), flush=True)
        return 2
    if explicit:
        try:
            chosen = accepted_root(explicit, workspace)
        except ValueError:
            print(json.dumps({"kind": "error", "reason": "root"}), flush=True)
            return 2
    else:
        found = find_named(workspace)
        if len(found) != 1:
            print(json.dumps({"kind": "error", "reason": "count", "count": len(found)}), flush=True)
            return 2
        chosen = found[0]
    save_json(Path(root)/"allow.json", {"root": chosen})
    try:
        install(root, bundle)
    except ValueError:
        print(json.dumps({"kind": "error", "reason": "hooks"}), flush=True)
        return 2
    print(json.dumps({"kind": "ready", "workspace": workspace}), flush=True)
    return 0


def stream_events(root, after=0, previous_epoch=""):
    connection = journal(root)
    epoch = connection.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
    if epoch != previous_epoch:
        after = 0
    def send(message):
        print(json.dumps(message, ensure_ascii=False), flush=True)
    send({"kind": "ready", "epoch": epoch, "protocol": 1, "python": sys.version.split()[0], "self_test": self_test(root)})
    pulse = 0
    try:
        while True:
            rows = connection.execute("SELECT seq,data FROM events WHERE seq>? ORDER BY seq LIMIT 256", (after,)).fetchall()
            for seq, data in rows:
                send({"kind": "event", "epoch": epoch, "seq": seq, "event": json.loads(data)})
                after = seq
            if time.monotonic()-pulse >= 2:
                try:
                    diagnostic = json.loads((root/"last-hook.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    diagnostic = {}
                send({"kind": "pulse", "epoch": epoch, "seq": after, "last_hook": diagnostic})
                pulse = time.monotonic()
            time.sleep(.2)
    finally:
        connection.close()


def self_test(root):
    with tempfile.TemporaryDirectory(prefix="collector-check-", dir=root) as temporary:
        target = Path(temporary)
        payload = {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "self-test",
                   "generation_id": "self-test-turn", "prompt": "discard-me"}
        response = observe(target, "beforeSubmitPrompt", io.BytesIO(json.dumps(payload).encode()))
        connection = journal(target)
        row = connection.execute("SELECT data FROM events").fetchone()
        connection.close()
        return response == {"continue": True} and row is not None and "discard-me" not in row[0]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("hook", "stream", "install-stream", "prepare", "uninstall"))
    parser.add_argument("--event", default="")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--epoch", default="")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)
    root = Path.home()/".agent-pet"/"naiwa-remote"
    bundle = Path(sys.argv[0]).resolve()
    if args.command == "hook":
        print(json.dumps(observe(root, args.event, sys.stdin.buffer, require_allow=True)))
        return 0
    if args.command == "prepare":
        return prepare(root, args.workspace, args.root, bundle)
    if args.command == "uninstall":
        uninstall(root, bundle)
        print(json.dumps({"kind": "ready", "removed": True}), flush=True)
        return 0
    if args.command == "install-stream":
        install(root, bundle)
    stream_events(root, args.after, args.epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
