import json
from datetime import datetime, timedelta, timezone

from naiwa.adapt import adapt
from naiwa.bus import append_event, read_events, read_snapshot, rotate_if_needed
from naiwa.phase import STALE_AFTER_SECONDS, PhaseMachine
from naiwa.schema import BusEvent


def event(name, **kwargs):
    payload = {
        "source": "cursor",
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "event": name,
        "ts": "2026-10-05T06:00:00+00:00",
    }
    payload.update(kwargs)
    return BusEvent(**payload)


def test_parallel_sessions_ignore_subagents():
    machine = PhaseMachine()
    machine.apply(event("turn_start", session_id="a"))
    machine.apply(event("turn_start", session_id="b", turn_id="turn-b"))
    machine.apply(event("subagent_start", session_id="a", subagent_id="sub-1"))
    view = machine.view(datetime.fromisoformat("2026-10-05T06:00:10+00:00").timestamp())
    assert view.active_sessions == 2
    assert view.subagents == ("cursor:sub-1",)
    assert view.pose == "working"


def test_late_stop_does_not_close_new_turn():
    machine = PhaseMachine()
    machine.apply(event("turn_start", turn_id="old"))
    machine.apply(event("turn_start", turn_id="new"))
    machine.apply(event("turn_done", turn_id="old", end_reason="completed"))
    view = machine.view(datetime.fromisoformat("2026-10-05T06:00:10+00:00").timestamp())
    assert view.pose == "working"


def test_one_tool_end_leaves_the_other_running():
    machine = PhaseMachine()
    machine.apply(event("turn_start"))
    machine.apply(event("tool_start", tool_call_id="t1", tool_name="Shell"))
    machine.apply(event("tool_start", tool_call_id="t2", tool_name="Read"))
    machine.apply(event("tool_fail", tool_call_id="t1"))
    view = machine.view(datetime.fromisoformat("2026-10-05T06:00:10+00:00").timestamp())
    assert view.pose == "tool"
    assert machine.turns["cursor:sess-1"].tools == {"t2"}


def test_duplicate_tool_end_is_harmless():
    machine = PhaseMachine()
    machine.apply(event("turn_start"))
    machine.apply(event("tool_start", tool_call_id="t1"))
    machine.apply(event("tool_end", tool_call_id="t1"))
    machine.apply(event("tool_end", tool_call_id="t1"))
    assert machine.turns["cursor:sess-1"].phase == "working"
    assert machine.turns["cursor:sess-1"].tools == set()


def test_cursor_completed_done_and_abort_idle():
    done = PhaseMachine()
    done.apply(event("turn_start"))
    done.apply(event("turn_done", end_reason="completed"))
    view = done.view(datetime.fromisoformat("2026-10-05T06:00:01+00:00").timestamp())
    assert view.pose == "done"
    assert view.bubble == "回合结束"
    aborted = PhaseMachine()
    aborted.apply(event("turn_start"))
    aborted.apply(event("turn_interrupt", end_reason="aborted"))
    assert aborted.view(datetime.fromisoformat("2026-10-05T06:00:10+00:00").timestamp()).pose == "idle"


def test_unconfirmed_stop_is_not_success_and_tool_fail_is_not_task_error():
    machine = PhaseMachine()
    machine.apply(event("turn_start", source="codex"))
    machine.apply(event("tool_fail", source="codex", tool_call_id="t1"))
    assert machine.turns["codex:sess-1"].phase == "working"
    machine.apply(event("turn_unconfirmed", source="codex"))
    now = datetime.fromisoformat("2026-10-05T06:00:01+00:00").timestamp()
    view = machine.view(now)
    assert view.pose == "stopped"
    assert view.bubble == "本轮结束"
    assert machine.view(now+3).pose == "idle"


def test_task_error_and_stale_are_different():
    failed = PhaseMachine()
    failed.apply(event("turn_start"))
    failed.apply(event("turn_error", end_reason="error"))
    assert failed.view(datetime.fromisoformat("2026-10-05T06:00:10+00:00").timestamp()).bubble == "任务出错"
    stale = PhaseMachine()
    stale.apply(event("turn_start"))
    now = datetime.fromisoformat("2026-10-05T06:00:00+00:00") + timedelta(seconds=STALE_AFTER_SECONDS + 5)
    view = stale.view(now.timestamp())
    assert view.pose == "stale"
    assert view.bubble == "暂时没有新消息"


def test_subagent_stop_keeps_the_main_turn():
    machine = PhaseMachine()
    machine.apply(event("turn_start"))
    machine.apply(event("subagent_start", subagent_id="sub-1"))
    machine.apply(event("subagent_stop", subagent_id="sub-1"))
    assert machine.turns["cursor:sess-1"].phase == "working"
    assert machine.turns["cursor:sess-1"].subagents == set()


def test_done_ack_matches_turn_only():
    machine = PhaseMachine()
    machine.apply(event("turn_start", turn_id="one"))
    machine.apply(event("turn_done", turn_id="one"))
    machine.apply(event("done_ack", turn_id="other"))
    assert machine.turns["cursor:sess-1"].phase == "done"
    machine.apply(event("done_ack", turn_id="one"))
    assert machine.turns["cursor:sess-1"].phase == "idle"


def test_restart_replays_snapshot_then_new_events(tmp_path):
    append_event(tmp_path, event("turn_start", session_id="kept"))
    assert rotate_if_needed(tmp_path, max_bytes=1)
    append_event(tmp_path, event("turn_start", session_id="fresh", turn_id="turn-2"))
    restored = PhaseMachine.from_snapshot(read_snapshot(tmp_path))
    restored.replay(read_events(tmp_path / "events.jsonl"))
    assert set(restored.turns) == {"cursor:kept", "cursor:fresh"}


def test_partial_tail_is_dropped(tmp_path):
    path = tmp_path / "runtime"
    path.mkdir()
    target = path / "events.jsonl"
    good = event("turn_start").to_dict()
    target.write_text(json.dumps(good) + "\n" + "{\"event\":", encoding="utf-8")
    assert len(read_events(target)) == 1


def test_adapter_drops_prompt_and_maps_endings():
    cursor = adapt(
        "cursor",
        {
            "hook_event_name": "stop",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "status": "completed",
            "prompt": "secret prompt",
        },
        ts="2026-10-05T06:00:00+00:00",
    )
    assert cursor is not None
    assert cursor.event == "turn_done"
    assert "secret" not in json.dumps(cursor.to_dict())
    aborted = adapt(
        "cursor",
        {"hook_event_name": "stop", "conversation_id": "conv-1", "generation_id": "gen-1", "status": "aborted"},
    )
    assert aborted.event == "turn_interrupt"
    codex = adapt("codex", {"hook_event_name": "Stop", "session_id": "thr_1", "turn_id": "turn_1", "last_assistant_message": "hello"})
    assert codex.event == "turn_unconfirmed"
    assert "hello" not in json.dumps(codex.to_dict())
    waiting = adapt("codex", {"hook_event_name": "PermissionRequest", "session_id": "thr_1", "turn_id": "turn_1", "tool_name": "Bash"})
    assert waiting.event == "approval_wait"
    parent = adapt(
        "cursor",
        {
            "hook_event_name": "subagentStart",
            "conversation_id": "child",
            "parent_conversation_id": "parent-1",
            "subagent_id": "sub-9",
            "generation_id": "gen-9",
            "task": "do not store",
        },
    )
    assert parent.session_id == "parent-1"
    assert parent.subagent_id == "sub-9"
    assert "do" not in json.dumps(parent.to_dict())
