"""Replay races and missing fields seen on the actual hook surfaces."""
from datetime import datetime, timedelta, timezone

from naiwa.adapt import adapt, key_paths
from naiwa.phase import DONE_SECONDS, ERROR_SECONDS, PhaseMachine
from naiwa.schema import BusEvent

BASE = datetime(2026, 10, 5, 6, tzinfo=timezone.utc)


def emit(machine, event, offset=0, **kw):
    machine.apply(BusEvent(**{"source": "cursor", "session_id": "same", "turn_id": "one",
                             "event": event, "ts": (BASE+timedelta(seconds=offset)).isoformat(), **kw}))


def view(machine, offset=1):
    return machine.view(BASE.timestamp()+offset)


def test_sources_with_identical_session_ids_do_not_collide():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    emit(machine, "turn_start", source="codex")
    emit(machine, "turn_interrupt")
    assert view(machine).active_sessions == 1
    assert view(machine).source == "codex"


def test_duplicate_start_does_not_clear_tool_or_approval():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    emit(machine, "tool_start", tool_call_id="a", tool_name="Shell")
    emit(machine, "approval_wait", tool_call_id="a")
    emit(machine, "turn_start")
    assert view(machine).pose == "needs_you"
    assert machine.turns["cursor:same"].tools == {"a"}


def test_correlated_completion_releases_approval_but_keeps_other_tools():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    emit(machine, "tool_start", tool_call_id="a", tool_name="Shell")
    emit(machine, "approval_wait", tool_call_id="a")
    emit(machine, "tool_start", tool_call_id="b", tool_name="Read")
    emit(machine, "tool_end", tool_call_id="b")
    assert view(machine).pose == "needs_you"
    emit(machine, "tool_end", tool_call_id="a")
    assert view(machine).pose == "working"


def test_permission_without_call_id_clears_on_post_tool_use():
    machine = PhaseMachine()
    emit(machine, "turn_start", source="codex")
    emit(machine, "tool_start", source="codex", tool_call_id="a", tool_name="Bash")
    emit(machine, "approval_wait", source="codex", tool_name="Bash")
    emit(machine, "tool_end", source="codex", tool_call_id="a", tool_name="Bash")
    assert view(machine).pose == "working"


def test_missing_tool_id_still_shows_tool_and_can_finish():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    emit(machine, "tool_start", tool_name="Shell")
    assert view(machine).pose == "tool"
    emit(machine, "tool_end", tool_name="Shell")
    assert view(machine).pose == "working"


def test_tool_label_returns_to_the_tool_that_remains():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    emit(machine, "tool_start", tool_call_id="a", tool_name="Shell")
    emit(machine, "tool_start", tool_call_id="b", tool_name="Read")
    emit(machine, "tool_end", tool_call_id="b")
    assert view(machine).tool_name == "Shell"


def test_late_subagent_event_cannot_affect_new_turn():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    emit(machine, "turn_start", offset=2, turn_id="two")
    emit(machine, "subagent_start", offset=3, subagent_id="old-child")
    assert not view(machine, 4).subagents


def test_completed_turn_cannot_be_resurrected_by_a_late_tool_end():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    emit(machine, "turn_done", offset=1)
    emit(machine, "tool_end", offset=2, tool_call_id="a")
    assert view(machine, 2).pose == "done"
    assert view(machine, 10).pose == "idle"


def test_completion_and_error_expire_without_persistent_client_ack_log():
    for event, duration in (("turn_done", DONE_SECONDS), ("turn_error", ERROR_SECONDS)):
        machine = PhaseMachine()
        emit(machine, "turn_start")
        emit(machine, event)
        assert view(machine, duration+1).pose == "idle"


def test_mid_turn_install_is_recovered_and_elapsed_tracks_start_not_last_tool():
    machine = PhaseMachine()
    emit(machine, "tool_start", offset=2, tool_call_id="a", tool_name="Shell")
    assert view(machine, 3).pose == "tool"
    emit(machine, "tool_end", offset=5, tool_call_id="a")
    result = view(machine, 9)
    assert result.elapsed_seconds == 7
    assert result.silent_seconds == 4


def test_stale_resumes_on_heartbeat_and_does_not_override_an_active_session():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    assert view(machine, 200).pose == "stale"
    emit(machine, "heartbeat", offset=201)
    assert view(machine, 202).pose == "working"
    emit(machine, "turn_start", offset=500, source="codex")
    assert view(machine, 501).source == "codex"


def test_corrupt_snapshot_rows_are_skipped_and_unusual_ids_are_correlated():
    machine = PhaseMachine.from_snapshot({"turns": [None, {"session_id": "x", "phase": []}], "last_seq": "broken"})
    assert not machine.turns
    payload = {"hook_event_name": "preToolUse", "conversation_id": "session", "generation_id": "round",
               "tool_use_id": "call_part\nfc_" + "a"*200, "tool_name": "MCP:filesystem/read"}
    start = adapt("cursor", payload)
    end = adapt("cursor", {**payload, "hook_event_name": "postToolUse"})
    assert start.tool_call_id == end.tool_call_id
    assert start.tool_call_id.startswith("id-")
    assert start.tool_name == "MCP:filesystem/read"


def test_diagnostics_do_not_recurse_into_body_or_arbitrary_keys():
    payload = {"tool_input": {"secret prompt": "value"}, "custom": {"private path": 1}, "session_id": "x"}
    assert key_paths(payload) == ["session_id", "tool_input"]


def test_unknown_stop_reason_is_not_written_as_free_text():
    stop = adapt("cursor", {"hook_event_name": "stop", "conversation_id": "a", "reason": "secret_token"})
    assert stop.event == "turn_unconfirmed"
    assert stop.end_reason == ""


def test_approval_without_id_and_completion_without_name_use_the_correlated_tool():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    emit(machine, "tool_start", tool_call_id="a", tool_name="Shell")
    emit(machine, "approval_wait", tool_name="Shell")
    emit(machine, "tool_end", tool_call_id="a")
    assert view(machine).pose == "working"
