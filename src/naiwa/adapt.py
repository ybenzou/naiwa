"""Map Cursor and Codex hook payloads onto the whitelist bus event."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re

from naiwa.schema import BusEvent, identifier, token
from naiwa.workspace import workspace_identity

PENDING_CURSOR = "pending-cursor"
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_SUCCESS = {"completed", "success", "done", "succeeded"}
_INTERRUPT = {"aborted", "interrupted", "cancelled", "canceled", "interrupt"}
_ERROR = {"error", "failed", "failure"}

# Match tool identifiers, never question text or transcript contents. Some IDE
# versions do not emit these hooks; absence is not evidence of user input wait.
QUESTION_TOOLS = {"request_user_input", "request_user_input_async", "AskQuestion", "ask_question"}


def _tool_event(event: str, tool_name: str) -> str:
    if event == "tool_start" and tool_name.rsplit(".", 1)[-1] in QUESTION_TOOLS:
        return "input_wait"
    return event

_BODY_KEYS = {
    "prompt",
    "text",
    "task",
    "tool_input",
    "tool_output",
    "command",
    "attachments",
    "last_assistant_message",
    "error_message",
    "agent_message",
    "user_message",
    "transcript_path",
    "agent_transcript_path",
    "cwd",
    "file_path",
}


def key_paths(payload: object, prefix: str = "") -> list[str]:
    # Nested object keys can themselves contain commands/paths. Only known top-level
    # schema names are useful for diagnostics, and none of their values are logged.
    if not isinstance(payload, dict):
        return []
    known = _BODY_KEYS | {
        "hook_event_name", "conversation_id", "generation_id", "session_id", "turn_id",
        "parent_conversation_id", "parent_session_id", "parent_id", "tool_use_id",
        "tool_call_id", "subagent_id", "agent_id", "tool_name", "agent_type",
        "subagent_type", "status", "end_reason", "reason", "stop_reason", "model",
        "permission_mode", "stop_hook_active", "failure_type", "is_interrupt",
        "cursor_version", "model_id", "model_params", "workspace_roots", "user_email",
        "duration", "duration_ms", "source",
    }
    return sorted(key for key in payload if key in known)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(payload: dict, *keys: str) -> str:
    for key in keys:
        value = token(payload.get(key))
        if value:
            return value
    return ""


def _id(payload: dict, *keys: str) -> str:
    return next((identifier(payload.get(k)) for k in keys if identifier(payload.get(k))), "")


def _reason(payload: dict) -> str:
    for key in ("status", "end_reason", "reason", "stop_reason"):
        value = token(payload.get(key))
        if value.lower() in _SUCCESS | _INTERRUPT | _ERROR:
            return value.lower()
    return ""


def _finish_event(reason: str) -> str:
    lowered = reason.lower()
    if lowered in _SUCCESS:
        return "turn_done"
    if lowered in _INTERRUPT:
        return "turn_interrupt"
    if lowered in _ERROR:
        return "turn_error"
    return "turn_unconfirmed"


def transcript_conversation(value: object) -> str:
    """Use only a UUID file or parent name. Never return or retain the path."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return ""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return ""
    path = PurePosixPath(value.replace("\\", "/"))
    for part in (path.stem, path.parent.name):
        if _UUID.fullmatch(part):
            return part.lower()
    return ""


def recover_cursor_identity(payload: dict, transcript_path: object, active_session_ids: list[str]) -> tuple[dict, str]:
    """Fill a missing Cursor conversation id. The returned payload never gains a path."""
    copied = dict(payload)
    if _id(copied, "conversation_id", "session_id"):
        return copied, "none"
    found = transcript_conversation(transcript_path)
    name = _first(copied, "hook_event_name")
    if found:
        copied["conversation_id"] = found
        return copied, "transcript"
    if name == "beforeSubmitPrompt":
        copied["conversation_id"] = PENDING_CURSOR
        return copied, "provisional"
    if name in {"afterAgentThought", "afterAgentResponse"} and len(active_session_ids) == 1:
        copied["conversation_id"] = active_session_ids[0]
        return copied, "single_active"
    return copied, "none"


def adapt(source: str, payload: dict, ts: str | None = None) -> BusEvent | None:
    if not isinstance(payload, dict):
        return None
    source = token(source)
    if source not in {"cursor", "codex"}:
        return None
    stamp = ts or _now()
    name, workspace_id = workspace_identity(source, payload)
    if source == "cursor":
        event = _cursor(_first(payload, "hook_event_name"), payload, stamp)
    else:
        event = _codex(_first(payload, "hook_event_name"), payload, stamp)
    if event is None:
        return None
    return replace(event, workspace_name=name, workspace_id=workspace_id)


def _cursor(name: str, payload: dict, ts: str) -> BusEvent | None:
    session_id = _id(payload, "conversation_id", "session_id")
    turn_id = _id(payload, "generation_id", "turn_id")
    parent_id = _id(payload, "parent_conversation_id", "parent_id")
    tool_call_id = _id(payload, "tool_use_id", "tool_call_id")
    subagent_id = _id(payload, "subagent_id")
    tool_name = _first(payload, "tool_name", "subagent_type")
    if name in {"subagentStart", "subagentStop"} and parent_id:
        session_id = parent_id
    common = dict(
        source="cursor",
        session_id=session_id,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        subagent_id=subagent_id,
        parent_id=parent_id,
        tool_name=tool_name,
        ts=ts,
    )
    mapping = {
        "beforeSubmitPrompt": "turn_start",
        "preToolUse": "tool_start",
        "postToolUse": "tool_end",
        "postToolUseFailure": "tool_fail",
        "subagentStart": "subagent_start",
        "subagentStop": "subagent_stop",
        "afterAgentResponse": "heartbeat",
        "afterAgentThought": "heartbeat",
        "preCompact": "compact_start",
        "sessionEnd": "session_end",
    }
    if name in mapping:
        if not session_id:
            return None
        return BusEvent(event=_tool_event(mapping[name], tool_name), **common)
    if name == "stop":
        if not session_id:
            return None
        reason = _reason(payload)
        return BusEvent(event=_finish_event(reason), end_reason=reason, **common)
    return None


def _codex(name: str, payload: dict, ts: str) -> BusEvent | None:
    session_id = _id(payload, "session_id")
    turn_id = _id(payload, "turn_id")
    parent_id = _id(payload, "parent_session_id", "parent_id")
    tool_call_id = _id(payload, "tool_use_id", "tool_call_id")
    subagent_id = _id(payload, "agent_id", "subagent_id")
    tool_name = _first(payload, "tool_name", "agent_type")
    common = dict(
        source="codex",
        session_id=session_id,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        subagent_id=subagent_id,
        parent_id=parent_id,
        tool_name=tool_name,
        ts=ts,
    )
    if name in {"SubagentStart", "SubagentStop"} and not session_id and parent_id:
        session_id = parent_id
        common["session_id"] = session_id
    mapping = {
        "UserPromptSubmit": "turn_start",
        "PreToolUse": "tool_start",
        "PostToolUse": "tool_end",
        "PermissionRequest": "approval_wait",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
        "Interrupt": "turn_interrupt",
        "SessionEnd": "session_end",
        "PreCompact": "compact_start",
        "PostCompact": "compact_end",
    }
    if name in mapping:
        if not session_id:
            return None
        event = _tool_event(mapping[name], tool_name)
        reason = "interrupt" if event == "turn_interrupt" else ""
        return BusEvent(event=event, end_reason=reason, **common)
    if name == "Stop":
        if not session_id:
            return None
        reason = _reason(payload)
        return BusEvent(event=_finish_event(reason), end_reason=reason, **common)
    return None
