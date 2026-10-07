"""Cursor thoughts keep one left hand without storing paths or guessing among chats."""
from datetime import datetime, timezone
import json
import pytest

from naiwa.adapt import recover_cursor_identity, transcript_conversation
from naiwa.hook import handle
from naiwa.phase import PhaseMachine
from naiwa.schema import BusEvent

NOW = 1791180000.0
UUID = "11111111-2222-4333-8444-555555555555"


def stamp(offset=0):
    return datetime.fromtimestamp(NOW+offset, timezone.utc).isoformat()


def apply(machine, name, offset=0, session="one", turn="t", **kwargs):
    machine.apply(BusEvent("cursor", session, turn, name, ts=stamp(offset), **kwargs))


def test_transcript_name_is_only_a_uuid():
    path = rf"C:\Users\private\.cursor\projects\FE\agent-transcripts\{UUID}\{UUID}.jsonl"
    assert transcript_conversation(path) == UUID
    assert transcript_conversation("/tmp/not-a-conversation/notes.txt") == ""
    assert "/" not in transcript_conversation(path) and "\\" not in transcript_conversation(path)


def test_empty_thought_uses_transcript_uuid_and_not_the_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_TRANSCRIPT_PATH", f"/secret/home/{UUID}/{UUID}.jsonl")
    handle("cursor", {"hook_event_name": "afterAgentThought", "text": "私密思考"}, root=tmp_path)
    from naiwa.bus import events_path, read_events
    events = read_events(events_path(tmp_path))
    diagnostic = json.loads((tmp_path/"probe-keys.jsonl").read_text(encoding="utf-8"))
    assert len(events) == 1 and events[0].event == "heartbeat" and events[0].session_id == UUID
    assert diagnostic["recovered"] == "transcript"
    assert "/secret/home" not in (tmp_path/"probe-keys.jsonl").read_text(encoding="utf-8")
    assert "私密思考" not in events_path(tmp_path).read_text(encoding="utf-8")


def test_empty_submit_then_real_session_keeps_one_hand():
    machine = PhaseMachine()
    apply(machine, "turn_start", session="pending-cursor", turn="temp")
    apply(machine, "turn_start", 1, session="pending-cursor", turn="another")
    assert [turn.number for turn in machine.turns.values()] == [1]
    apply(machine, "tool_start", 2, session="real-chat", turn="gen", tool_name="Grep", tool_call_id="g")
    assert list(machine.turns) == ["cursor:real-chat"]
    assert machine.turns["cursor:real-chat"].number == 1
    assert machine.view(NOW+2).pose == "tool"


def test_unscoped_thought_follows_only_one_open_cursor_session():
    machine = PhaseMachine()
    apply(machine, "turn_start", session="only")
    apply(machine, "heartbeat", 1, session="pending-cursor", turn="")
    assert machine.turns["cursor:only"].phase == "working"
    apply(machine, "turn_start", 2, session="second")
    before = machine.turns["cursor:only"].ts
    apply(machine, "heartbeat", 3, session="pending-cursor", turn="nope")
    assert machine.turns["cursor:only"].ts == before
    assert "cursor:pending-cursor" not in machine.turns


def test_new_generation_reopens_same_hand_and_old_end_does_not_close_it():
    machine = PhaseMachine()
    apply(machine, "turn_start")
    apply(machine, "turn_done", 1)
    apply(machine, "heartbeat", 2, turn="next")
    assert machine.turns["cursor:one"].number == 1
    assert machine.view(NOW+2).pose == "done"
    apply(machine, "tool_start", 3, turn="next", tool_name="Grep", tool_call_id="g")
    apply(machine, "tool_end", 4, turn="t", tool_call_id="g", tool_name="Grep")
    assert machine.view(NOW+4).pose == "tool"
    assert machine.turns["cursor:one"].turn_id == "next"


def test_cursor_thought_generation_drift_cannot_retire_the_real_stop_generation():
    machine = PhaseMachine()
    apply(machine, "turn_start", turn="canonical")
    apply(machine, "tool_start", 1, turn="canonical", tool_name="Read", tool_call_id="r")
    apply(machine, "heartbeat", 2, turn="thought-a")
    assert machine.turns["cursor:one"].turn_id == "canonical"
    assert machine.turns["cursor:one"].tools == {"r"}
    apply(machine, "tool_end", 3, turn="canonical", tool_name="Read", tool_call_id="r")
    apply(machine, "heartbeat", 4, turn="thought-b")
    apply(machine, "turn_done", 5, turn="canonical", end_reason="completed")
    assert machine.turns["cursor:one"].phase == "done"
    apply(machine, "heartbeat", 6, turn="thought-c")
    assert machine.turns["cursor:one"].phase == "done"
    assert not machine.turns["cursor:one"].previous_turn_ids


def test_a_burst_of_reads_stays_on_the_tool_between_calls():
    machine = PhaseMachine()
    apply(machine, "turn_start")
    apply(machine, "tool_start", 1, tool_name="Read", tool_call_id="a")
    apply(machine, "tool_end", 1.2, tool_name="Read", tool_call_id="a")
    assert machine.turns["cursor:one"].phase == "working"
    assert machine.view(NOW+1.2).pose == "tool"
    held = machine.view(NOW+1.6)
    assert held.pose == "tool" and held.agents[0].tool_name == "Read"
    apply(machine, "tool_start", 1.7, tool_name="Read", tool_call_id="b")
    apply(machine, "tool_end", 1.9, tool_name="Read", tool_call_id="b")
    assert machine.view(NOW+2.9).pose == "working"


def test_identity_helper_does_not_guess_between_two_sessions():
    payload, recovered = recover_cursor_identity(
        {"hook_event_name": "afterAgentThought", "text": "secret"}, "", ["a", "b"])
    assert recovered == "none" and "conversation_id" not in payload
    payload, recovered = recover_cursor_identity(
        {"hook_event_name": "beforeSubmitPrompt"}, "", [])
    assert recovered == "provisional" and payload["conversation_id"] == "pending-cursor"
    payload, recovered = recover_cursor_identity(
        {"hook_event_name": "afterAgentThought"}, "", ["only"])
    assert recovered == "single_active" and payload["conversation_id"] == "only"


def test_missing_submit_adopts_new_cursor_generation_before_old_turn_times_out():
    machine = PhaseMachine()
    apply(machine, "turn_start", session="fe", turn="old", workspace_name="FE")
    apply(machine, "tool_start", 1, session="fe", turn="old", tool_name="Read", tool_call_id="a")
    apply(machine, "turn_start", session="other", turn="parallel", workspace_name="FE")
    apply(machine, "tool_start", 2, session="fe", turn="next", tool_name="Shell", tool_call_id="b")
    turn = machine.turns["cursor:fe"]
    assert turn.turn_id == "next" and turn.number == 1 and turn.inferred
    assert turn.tools == {"b"} and turn.workspace_name == "FE"
    apply(machine, "turn_done", 3, session="fe", turn="old", end_reason="completed")
    assert turn.phase == "tool"
    apply(machine, "tool_end", 4, session="fe", turn="next", tool_call_id="b", tool_name="Shell")
    apply(machine, "turn_done", 5, session="fe", turn="next", end_reason="completed")
    assert turn.phase == "done"
    assert machine.turns["cursor:other"].phase == "working"


@pytest.mark.parametrize("ending,phase", [("turn_done", "done"), ("turn_interrupt", "idle"),
                                         ("turn_error", "error"), ("turn_unconfirmed", "stopped")])
def test_unseen_cursor_generation_stop_can_finish_quiescent_turn(ending, phase):
    machine = PhaseMachine()
    apply(machine, "turn_start", workspace_name="FE")
    apply(machine, "tool_start", 1, tool_name="Read", tool_call_id="a")
    apply(machine, "tool_end", 2, tool_name="Read", tool_call_id="a")
    apply(machine, "heartbeat", 3)
    apply(machine, ending, 4, turn="unseen")
    current = machine.turns["cursor:one"]
    assert current.phase == phase and current.turn_id == "unseen" and current.number == 1
    apply(machine, "heartbeat", 5, turn="t")
    apply(machine, "tool_start", 6, turn="unseen", tool_name="Read", tool_call_id="late")
    assert machine.turns["cursor:one"] is current and current.phase == phase and not current.tools
    restored = PhaseMachine.from_snapshot(machine.snapshot())
    apply(restored, "heartbeat", 7, turn="t")
    assert restored.turns["cursor:one"].phase == phase


@pytest.mark.parametrize("pending,kwargs", [
    ("tool_start", {"tool_call_id": "a", "tool_name": "Read"}),
    ("approval_wait", {}), ("input_wait", {}),
    ("subagent_start", {"subagent_id": "child"}), ("compact_start", {})])
def test_unseen_stop_does_not_override_outstanding_work(pending, kwargs):
    machine = PhaseMachine()
    apply(machine, "turn_start")
    apply(machine, pending, 1, **kwargs)
    apply(machine, "turn_done", 2, turn="unseen")
    assert machine.turns["cursor:one"].turn_id == "t"
    assert machine.turns["cursor:one"].phase not in {"done", "idle", "stopped", "error"}


def test_late_retired_submit_or_sequenced_older_unknown_stop_cannot_replace_current_turn():
    machine = PhaseMachine()
    apply(machine, "turn_start", turn="old", seq=1)
    apply(machine, "turn_start", 1, turn="new", seq=2)
    apply(machine, "turn_start", 2, turn="old", seq=3)
    apply(machine, "turn_done", -1, turn="unseen", seq=4)
    assert machine.turns["cursor:one"].turn_id == "new"
    assert machine.turns["cursor:one"].phase == "working" and machine.last_seq == 4


def test_codex_generation_mismatch_and_silence_remain_unconfirmed():
    machine = PhaseMachine()
    machine.apply(BusEvent("codex", "chat", "old", "turn_start", ts=stamp()))
    machine.apply(BusEvent("codex", "chat", "unseen", "turn_done", ts=stamp(1)))
    assert machine.turns["codex:chat"].phase == "working"
    assert machine.view(NOW+181).pose == "stale"


def test_hook_diagnostics_keep_sanitized_stop_correlation(tmp_path):
    from naiwa.bus import events_path, read_events
    handle("cursor", {"hook_event_name": "stop", "conversation_id": "chat", "generation_id": "round",
                      "status": "completed", "prompt": "secret contents", "tool_output": "secret output"}, root=tmp_path)
    event = read_events(events_path(tmp_path))[0]
    diagnostic = json.loads((tmp_path/"probe-keys.jsonl").read_text(encoding="utf-8"))
    for field in ("session_id", "turn_id", "event", "end_reason", "seq"):
        assert diagnostic[field] == getattr(event, field)
    assert "secret" not in (tmp_path/"probe-keys.jsonl").read_text(encoding="utf-8")
