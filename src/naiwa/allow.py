"""Observe-only hook responses. Never ask, deny, or continue a turn."""

from __future__ import annotations

CURSOR_PERMISSION = {
    "preToolUse",
    "postToolUse",
    "beforeShellExecution",
    "beforeMCPExecution",
    "beforeReadFile",
    "subagentStart",
}

FORBIDDEN = {"ask", "deny", "block", "followup_message"}


def allow_response(source: str, event_name: str) -> dict:
    if source == "cursor":
        if event_name == "beforeSubmitPrompt":
            return {"continue": True}
        if event_name in CURSOR_PERMISSION:
            return {"permission": "allow"}
        return {}
    if event_name in {"Stop", "SubagentStop"}:
        return {"continue": True}
    return {}


def is_observe_only(response: dict) -> bool:
    if not isinstance(response, dict):
        return False
    if FORBIDDEN.intersection(response):
        return False
    permission = response.get("permission")
    if permission not in {None, "allow"}:
        return False
    decision = response.get("decision")
    if decision not in {None}:
        return False
    specific = response.get("hookSpecificOutput")
    if isinstance(specific, dict) and specific.get("permissionDecision") not in {None, "allow"}:
        return False
    if isinstance(specific, dict) and "decision" in specific:
        return False
    return True
