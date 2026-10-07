"""Validated whitelist contract. Bodies and paths never enter the bus."""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from naiwa.workspace import display_name, opaque_id

SCHEMA_VERSION = 3
TOKEN = re.compile(r"^[A-Za-z0-9_.:/+-]{1,160}$")
EVENTS = frozenset({
    "turn_start", "heartbeat", "tool_start", "tool_end", "tool_fail",
    "approval_wait", "input_wait", "input_resume", "subagent_start", "subagent_stop", "turn_done",
    "compact_start", "compact_end",
    "turn_interrupt", "turn_error", "turn_unconfirmed", "done_ack", "session_end",
    "connection_lost", "connection_restored",
})


def token(value: object) -> str:
    if isinstance(value, str) and TOKEN.fullmatch(value.strip()):
        return value.strip()
    return ""


def identifier(value: object) -> str:
    """Hash unusual opaque IDs so starts and ends can still be correlated."""
    if not isinstance(value, str) or not value.strip():
        return ""
    return token(value) or "id-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > 48:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if not math.isfinite(parsed.timestamp()):
            return ""
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


@dataclass(frozen=True)
class BusEvent:
    source: str
    session_id: str
    turn_id: str
    event: str
    tool_call_id: str = ""
    subagent_id: str = ""
    parent_id: str = ""
    end_reason: str = ""
    tool_name: str = ""
    ts: str = ""
    schema_version: int = SCHEMA_VERSION
    seq: int = 0
    origin: str = ""
    workspace_name: str = ""
    workspace_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "BusEvent":
        if not isinstance(raw, dict):
            raise ValueError("Event must be an object")
        version = raw.get("schema_version", 1)
        if type(version) is not int or version not in {1, 2, SCHEMA_VERSION}:
            raise ValueError("Unsupported event version")
        source, event = token(raw.get("source")), token(raw.get("event"))
        if source not in {"cursor", "codex", "demo"} or event not in EVENTS:
            raise ValueError("Unknown event source or name")
        session = identifier(raw.get("session_id"))
        seq = raw.get("seq", 0)
        if not session or type(seq) is not int or seq < 0:
            raise ValueError("Invalid session or sequence")
        return cls(
            source=source, session_id=session, event=event, turn_id=identifier(raw.get("turn_id")),
            tool_call_id=identifier(raw.get("tool_call_id")), subagent_id=identifier(raw.get("subagent_id")),
            parent_id=identifier(raw.get("parent_id")), end_reason=token(raw.get("end_reason")),
            tool_name=token(raw.get("tool_name")), ts=timestamp(raw.get("ts")),
            schema_version=version, seq=seq, origin=token(raw.get("origin")),
            workspace_name=display_name(raw.get("workspace_name")),
            workspace_id=opaque_id(raw.get("workspace_id")),
        )
