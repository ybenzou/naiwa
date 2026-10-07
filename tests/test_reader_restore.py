from naiwa.bus import (EventReader, append_diagnostic, append_event, atomic_json,
                       read_snapshot, rotate_if_needed, snapshot_path)
from naiwa.phase import PhaseMachine
from naiwa.schema import BusEvent


def event(kind, turn="canonical", session="chat", **extra):
    return BusEvent("cursor", session, turn, kind, **extra)


def corrupt_snapshot(root, session="chat"):
    snapshot = read_snapshot(root)
    snapshot.pop("phase_rules", None)
    for turn in snapshot["turns"]:
        if turn["session_id"] == session:
            turn.update(turn_id="thought-id", phase="working", end_reason="",
                        previous_turn_ids=["canonical"], tools=[], recent_actions=[], action_count=0)
    atomic_json(snapshot_path(root), snapshot)


def test_cold_restart_recovers_active_tools_from_old_writer_generation_drift(tmp_path):
    append_event(tmp_path, event("turn_start"))
    append_event(tmp_path, event("heartbeat", "thought-id"))
    append_event(tmp_path, event("tool_start", tool_name="Read", tool_call_id="read"))
    rotate_if_needed(tmp_path, max_bytes=1)
    corrupt_snapshot(tmp_path)
    reader = EventReader(tmp_path)
    machine = reader.poll(PhaseMachine())
    turn = machine.turns["cursor:chat"]
    assert turn.turn_id == "canonical" and turn.phase == "tool" and turn.tool_name == "Read"
    append_event(tmp_path, event("turn_done", end_reason="completed"))
    machine = reader.poll(machine)
    assert machine.turns["cursor:chat"].phase == "done"


def test_completed_local_turn_recovered_from_hook_evidence_after_ssh_rotations(tmp_path):
    append_event(tmp_path, event("turn_start"))
    thought = append_event(tmp_path, event("heartbeat", "thought-id"))
    stopped = append_event(tmp_path, event("turn_done", end_reason="completed"))
    for observed in (thought, stopped):
        append_diagnostic(tmp_path, {**observed.to_dict(), "observed": True})
    rotate_if_needed(tmp_path, max_bytes=1)
    corrupt_snapshot(tmp_path)
    old = read_snapshot(tmp_path)
    old["turns"][0]["last_seq"] = thought.seq
    atomic_json(snapshot_path(tmp_path), old)
    # Retention drops the actual local journal rows; diagnostics still retain
    # the genuine completed Stop, with no IDE log access or invented success.
    for index in range(2):
        append_event(tmp_path, event("heartbeat", session="remote"))
        rotate_if_needed(tmp_path, max_bytes=1)
        corrupt_snapshot(tmp_path)
    machine = EventReader(tmp_path).poll(PhaseMachine())
    assert machine.turns["cursor:chat"].turn_id == "canonical"
    assert machine.turns["cursor:chat"].phase == "done"


def test_reader_cache_survives_restart_and_old_writer_snapshot(tmp_path):
    append_event(tmp_path, event("turn_start"))
    append_event(tmp_path, event("heartbeat", "thought-id"))
    append_event(tmp_path, event("turn_done", end_reason="completed"))
    machine = EventReader(tmp_path).poll(PhaseMachine())
    atomic_json(tmp_path/"reader-state.json", {"reader_schema": 1, **machine.snapshot()})
    for index in range(3):
        rotate_if_needed(tmp_path, max_bytes=1)
        corrupt_snapshot(tmp_path)
        append_event(tmp_path, event("heartbeat", session="remote"))
        machine = EventReader(tmp_path).poll(machine)
        atomic_json(tmp_path/"reader-state.json", {"reader_schema": 1, **machine.snapshot()})
    restored = EventReader(tmp_path).poll(PhaseMachine())
    assert restored.turns["cursor:chat"].phase == "done"
    assert restored.turns["cursor:chat"].turn_id == "canonical"


def test_terminal_evidence_for_prior_round_does_not_replace_real_new_round(tmp_path):
    append_event(tmp_path, event("turn_start"))
    stopped = append_event(tmp_path, event("turn_done", end_reason="completed"))
    append_diagnostic(tmp_path, {**stopped.to_dict(), "observed": True})
    append_event(tmp_path, event("turn_start", "new-round"))
    append_event(tmp_path, event("tool_start", "new-round", tool_name="Shell", tool_call_id="shell"))
    rotate_if_needed(tmp_path, max_bytes=1)
    machine = EventReader(tmp_path).poll(PhaseMachine())
    assert machine.turns["cursor:chat"].turn_id == "new-round"
    assert machine.turns["cursor:chat"].phase == "tool"


def test_cache_replays_completion_in_archive_before_consuming_current_journal(tmp_path):
    append_event(tmp_path, event("turn_start"))
    machine = EventReader(tmp_path).poll(PhaseMachine())
    atomic_json(tmp_path/"reader-state.json", {"reader_schema": 1, **machine.snapshot()})
    append_event(tmp_path, event("turn_done", end_reason="completed"))
    rotate_if_needed(tmp_path, max_bytes=1)
    append_event(tmp_path, event("turn_start", session="remote"))
    restored = EventReader(tmp_path).poll(PhaseMachine())
    assert restored.turns["cursor:chat"].phase == "done"
    assert restored.turns["cursor:remote"].phase == "working"


def test_old_thought_id_rotated_out_cannot_hide_fresh_canonical_tools(tmp_path):
    append_event(tmp_path, event("turn_start"))
    append_event(tmp_path, event("heartbeat", "thought-id"))
    rotate_if_needed(tmp_path, max_bytes=1)
    corrupt_snapshot(tmp_path)
    for index in range(2):
        append_event(tmp_path, event("tool_start", tool_name="Read", tool_call_id=str(index)))
        append_event(tmp_path, event("tool_end", tool_name="Read", tool_call_id=str(index)))
        rotate_if_needed(tmp_path, max_bytes=1)
        corrupt_snapshot(tmp_path)
    restored = EventReader(tmp_path).poll(PhaseMachine())
    turn = restored.turns["cursor:chat"]
    assert turn.turn_id == "canonical" and turn.action_count > 0
    append_event(tmp_path, event("tool_start", tool_name="Shell", tool_call_id="next"))
    restored = EventReader(tmp_path).poll(restored)
    assert restored.turns["cursor:chat"].phase == "tool"


def test_current_rules_snapshot_does_not_adopt_retired_tool_generation(tmp_path):
    append_event(tmp_path, event("turn_start"))
    append_event(tmp_path, event("turn_done", end_reason="completed"))
    append_event(tmp_path, event("turn_start", "new-round"))
    rotate_if_needed(tmp_path, max_bytes=1)
    for index in range(2):
        append_event(tmp_path, event("tool_start", tool_name="Read", tool_call_id=str(index)))
        append_event(tmp_path, event("tool_end", tool_name="Read", tool_call_id=str(index)))
        rotate_if_needed(tmp_path, max_bytes=1)
    restored = EventReader(tmp_path).poll(PhaseMachine())
    assert restored.turns["cursor:chat"].turn_id == "new-round"
