"""Isolated local replay. Never writes mock IDE events into the production bus."""
from datetime import datetime, timezone

from naiwa.phase import PhaseMachine, STALE_AFTER_SECONDS
from naiwa.schema import BusEvent
from naiwa.workspace import workspace_identity

PHASES = ("idle", "working", "tool", "needs_you", "done", "error", "stale")
SCENARIOS = ("idle", "working", "multi", "mixed", "input_wait", "partial_done", "done", "error", "stale")


def demo_machine(phase: str, now: float | None = None) -> PhaseMachine:
    if phase not in PHASES+SCENARIOS:
        raise ValueError(f"Unknown demo phase: {phase}")
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    stamp = datetime.fromtimestamp(now-(STALE_AFTER_SECONDS+5 if phase == "stale" else 0), timezone.utc).isoformat()
    machine = PhaseMachine()
    if phase in {"multi", "mixed", "input_wait", "partial_done"}:
        for source, count in (("cursor", 3), ("codex", 3)):
            for number in range(1, count+1):
                name = (("naiwa", "optionda", "服务端API") if source == "cursor" else
                        ("optionda", "optionda", "设计素材工作区"))[number-1]
                _, identity = workspace_identity("codex", {"cwd": "/demo/"+name})
                fields = dict(source=source, session_id=f"preview-{source}-{number}", turn_id="preview-turn", ts=stamp,
                              workspace_name=name, workspace_id=identity)
                machine.apply(BusEvent(event="turn_start", **fields))
                if number == 2:
                    machine.apply(BusEvent(event="tool_start", tool_call_id="tool", tool_name="Shell", **fields))
                if phase in {"mixed", "input_wait"} and source == "codex" and number == 3:
                    machine.apply(BusEvent(event="input_wait", tool_call_id="question", tool_name="request_user_input", **fields))
                if phase == "mixed" and source == "cursor" and number == 1:
                    machine.apply(BusEvent(event="approval_wait", tool_call_id="approval", tool_name="Shell", **fields))
                if phase in {"mixed", "partial_done"} and source == "cursor" and number == 3:
                    machine.apply(BusEvent(event="turn_done", end_reason="completed", **fields))
        return machine
    def emit(event, **kw):
        machine.apply(BusEvent("demo", "preview", "preview-turn", event, ts=stamp, workspace_name="naiwa", **kw))
    if phase == "idle":
        return machine
    emit("turn_start")
    if phase == "tool":
        emit("tool_start", tool_call_id="tool-one", tool_name="Shell")
        emit("subagent_start", subagent_id="helper")
    elif phase == "needs_you":
        emit("tool_start", tool_call_id="tool-one", tool_name="Shell")
        emit("approval_wait", tool_call_id="tool-one", tool_name="Shell")
    elif phase == "done":
        emit("turn_done", end_reason="completed")
    elif phase == "error":
        emit("turn_error", end_reason="error")
    return machine
