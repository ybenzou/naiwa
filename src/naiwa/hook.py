"""Observe only. Every failure still emits legal neutral JSON and exits zero."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from naiwa.adapt import adapt, key_paths, recover_cursor_identity
from naiwa.allow import allow_response
from naiwa.bus import append_diagnostic, append_event
from naiwa.schema import token
from naiwa.hook_input import read_metadata
from naiwa.hosts import producer_host

def codex_origin() -> str:
    value = os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "")
    return "vscode" if value == "codex_vscode" else "other" if value else "unknown"


def active_cursor_sessions(root) -> list[str]:
    from datetime import datetime, timezone
    from naiwa.adapt import PENDING_CURSOR
    from naiwa.bus import events_path, read_events, read_snapshot
    from naiwa.phase import ACTIVE, PhaseMachine
    machine = PhaseMachine.from_snapshot(read_snapshot(root))
    machine.replay(read_events(events_path(root)))
    now = datetime.now(timezone.utc).timestamp()
    return [turn.session_id for turn in machine.turns.values()
            if turn.source == "cursor" and turn.session_id != PENDING_CURSOR and not turn.disconnected
            and machine._pose(turn, now) in ACTIVE]


def handle(source: str, payload: dict, root=None, *, origin: str = "", observe: bool = True,
           input_status: str = "ok", input_bytes: int = 0, input_mode: str = "stdin") -> dict:
    name = token(payload.get("hook_event_name")) if isinstance(payload, dict) else ""
    response = allow_response(source, name)
    outcome = "unmapped"
    event = None
    recovered = "none"
    try:
        if observe and source == "cursor" and isinstance(payload, dict):
            try:
                active = active_cursor_sessions(root)
            except Exception:
                active = []
            payload, recovered = recover_cursor_identity(
                payload, os.environ.get("CURSOR_TRANSCRIPT_PATH", ""), active)
        event = adapt(source, payload) if observe else None
        if event is not None:
            event = append_event(root, replace(event, origin=origin))
            outcome = "written"
    except Exception:
        outcome = "write_failed"
    try:
        producer = producer_host() if source == "codex" and origin == "vscode" else {}
    except Exception:
        producer = {}
    try:
        append_diagnostic(root, {
            "source": source, "hook_event_name": name, "keys": key_paths(payload),
            "ts": datetime.now(timezone.utc).isoformat(), "origin": origin,
            "observed": outcome == "written", "outcome": outcome,
            "input_status": input_status, "input_bytes": input_bytes,
            "input_mode": input_mode,
            "remote_workspace": source == "cursor" and os.environ.get("CURSOR_CODE_REMOTE") == "true",
            "execution_platform": sys.platform,
            "producer": producer, "workspace_name": event.workspace_name if event is not None else "",
            "recovered": recovered,
            # Already sanitized bus metadata, never prompt/tool contents. Keep
            # correlation evidence when high-volume tools rotate the journal.
            "session_id": event.session_id if event is not None else "",
            "turn_id": event.turn_id if event is not None else "",
            "event": event.event if event is not None else "",
            "end_reason": event.end_reason if event is not None else "",
            "seq": event.seq if outcome == "written" else 0,
        })
    except Exception:
        pass
    return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source", choices=("cursor", "codex"), default="cursor")
    parser.add_argument("--event", default="")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--ide-only", action="store_true")
    parser.add_argument("--input-file", type=Path)
    args, _ = parser.parse_known_args(argv)
    payload = {"hook_event_name": args.event}
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    if args.input_file is not None:
        try:
            with args.input_file.open("rb") as raw:
                parsed, input_status, input_bytes = read_metadata(raw)
        except OSError:
            parsed, input_status, input_bytes = {}, "file_unreadable", 0
    else:
        parsed, input_status, input_bytes = read_metadata(stream)
    if parsed:
        payload = parsed
    if not token(payload.get("hook_event_name")):
        payload["hook_event_name"] = args.event
    origin = codex_origin() if args.source == "codex" else "cursor"
    observe = not (args.source == "codex" and args.ide_only and origin != "vscode")
    response = handle(args.source, payload, root=args.data_dir, origin=origin, observe=observe,
                      input_status=input_status, input_bytes=input_bytes,
                      input_mode="file" if args.input_file is not None else "stdin")
    sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
