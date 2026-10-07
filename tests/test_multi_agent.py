from dataclasses import replace
from datetime import datetime, timezone

from naiwa.adapt import adapt
from naiwa.demo import demo_machine
from naiwa.multi_sprite import compose, composition_key, MAX_HANDS_PER_SIDE
from naiwa.notices import StatusNotices
from naiwa.phase import AgentView, PhaseMachine
from naiwa.schema import BusEvent
from naiwa.sprite_defs import PALETTE

NOW = 1791180000.0
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat()


def emit(machine, name, session="one", source="codex", **kwargs):
    machine.apply(BusEvent(source, session, "turn", name, ts=STAMP, **kwargs))


def test_input_wait_survives_heartbeat_and_unrelated_tools_then_resumes():
    machine = PhaseMachine()
    emit(machine, "turn_start")
    emit(machine, "input_wait", tool_call_id="question", tool_name="request_user_input")
    emit(machine, "tool_start", tool_call_id="shell", tool_name="Shell")
    emit(machine, "heartbeat")
    emit(machine, "tool_end", tool_call_id="shell")
    waiting = machine.view(NOW+200)
    assert waiting.pose == "needs_you"
    assert waiting.bubble == "在等你回答"
    assert waiting.agents[0].wait_kind == "input"
    restored = PhaseMachine.from_snapshot(machine.snapshot())
    emit(restored, "tool_end", tool_call_id="question")
    assert restored.view(NOW).pose == "working"


def test_input_and_approval_waits_clear_independently():
    machine = PhaseMachine()
    emit(machine, "input_wait", tool_call_id="question")
    emit(machine, "approval_wait", tool_call_id="permission")
    emit(machine, "input_resume", tool_call_id="question")
    assert machine.view(NOW).agents[0].wait_kind == "approval"
    emit(machine, "tool_end", tool_call_id="permission")
    assert machine.view(NOW).pose == "working"


def test_question_adapter_never_records_question_body():
    for source, name, tool in (("codex", "PreToolUse", "functions.request_user_input"),
                               ("cursor", "preToolUse", "AskQuestion")):
        event = adapt(source, {"hook_event_name": name, "session_id": "one", "conversation_id": "one",
                              "tool_name": tool, "tool_input": {"question": "private contents"}})
        assert event.event == "input_wait"
        assert "private" not in str(event.to_dict())
    assert adapt("codex", {"hook_event_name": "PreToolUse", "session_id": "one",
                           "tool_name": "some_request_user_input_wrapper"}).event == "tool_start"


def test_sources_and_session_numbers_remain_stable_across_turns_and_restart():
    machine = PhaseMachine()
    emit(machine, "turn_start", source="cursor")
    emit(machine, "turn_start", source="codex")
    emit(machine, "turn_start", session="two", source="cursor")
    emit(machine, "turn_done", source="cursor")
    machine.apply(BusEvent("cursor", "one", "next", "turn_start", ts=STAMP))
    restored = PhaseMachine.from_snapshot(machine.snapshot())
    labels = {a.key: a.label for a in restored.view(NOW).agents}
    assert labels == {"cursor:one": "C1", "cursor:two": "C2", "codex:one": "X1"}
    emit(restored, "subagent_start", session="two", source="cursor", subagent_id="child")
    assert len(restored.view(NOW).agents) == 3
    assert restored.view(NOW).active_sessions == 3


def test_completion_and_wait_are_not_hidden_by_other_working_sessions():
    machine = demo_machine("multi", NOW)
    notices = StatusNotices()
    notices.observe(machine.view(NOW).agents, 0)
    # Clear start notices, then complete C3 while five other sessions stay busy.
    notices.queue.clear()
    notices.until = 0
    machine.apply(BusEvent("cursor", "preview-cursor-3", "preview-turn", "turn_done", ts=STAMP))
    view = machine.view(NOW)
    assert view.pose == "tool"
    assert notices.observe(view.agents, 3) == "Cursor 服务端API · C3 · 完成"
    machine.apply(BusEvent("codex", "preview-codex-3", "preview-turn", "input_wait", tool_call_id="q", ts=STAMP))
    assert notices.observe(machine.view(NOW).agents, 3.1) == "Codex 设计素材工作区 · X3 · 等你回答"
    assert any("C3 · 完成" in item[1] for item in notices.queue)


def test_completion_expires_and_only_empty_work_returns_to_rest():
    machine = demo_machine("partial_done", NOW)
    assert machine.view(NOW+3).pose in {"working", "tool"}
    for turn in list(machine.turns.values()):
        machine.apply(BusEvent(turn.source, turn.session_id, turn.turn_id, "turn_done", ts=STAMP))
    assert machine.view(NOW+1).pose == "done"
    assert machine.view(NOW+3).pose == "idle"


def test_two_left_hands_expose_forearms_instead_of_only_palms():
    from naiwa.phase import AgentView
    agents = (
        AgentView("cursor", "local", "t", 1, "done", "", "", 0, 0, "FE"),
        AgentView("cursor", "remote", "t", 5, "working", "Read", "", 0, 0, "sample_project"),
    )
    frame = compose("working", 0, composition_key(agents), 0)
    left = [draw[1] for draw in frame.arm_draws if draw[2]]
    assert len(left) == 2
    assert max(left)-min(left) >= 40
    from naiwa.multi_sprite import placed_arm
    for gesture, angle, left, extension, x, y in frame.arm_draws:
        visible = sum(frame.owners.offsets.get(at) is not None
                      for at in placed_arm(gesture, angle, left, extension, x, y))
        assert visible >= 700


def test_fan_uses_each_source_and_keeps_approved_face_pixels():
    from naiwa.sprite_assets import pose_frames
    from naiwa.multi_sprite import WING
    agents = demo_machine("multi", NOW).view(NOW).agents
    frame = compose("working", 0, composition_key(agents), 0)
    assert set(frame.owners.values()) == {a.key for a in agents}
    for (x, y), key in frame.owners.items():
        assert x < frame.anchor[0] if key.startswith("cursor:") else x > frame.anchor[0]
    body = pose_frames("working")[0]
    for y, row in enumerate(body):
        for x, pixel in enumerate(row):
            if pixel[3] == 255:
                assert frame.canvas[y][x+WING] == pixel
    assert all(p in PALETTE for row in frame.canvas for p in row)


def test_large_fan_prioritizes_waiting_and_reports_every_hidden_session():
    agents = tuple(AgentView("cursor", f"s{i}", "t", i, "working", "", "", 0, 0) for i in range(1, 21))
    agents = (*agents[:-1], replace(agents[-1], pose="needs_you", wait_kind="input"))
    frame = compose("needs_you", 0, composition_key(agents), 0)
    assert "cursor:s20" in frame.owners.values()
    assert len(set(frame.owners.values())) == MAX_HANDS_PER_SIDE
    assert frame.overflow["cursor"] == 20-MAX_HANDS_PER_SIDE


def test_rest_has_horizontal_silhouette_and_common_contact_baseline():
    from naiwa.multi_sprite import rest_frame
    from naiwa.sprite_defs import FOOT_Y
    for i in range(4):
        frame = rest_frame(i)
        opaque = [(x, y) for y, row in enumerate(frame) for x, p in enumerate(row) if p[3] == 255]
        width = max(x for x, _ in opaque)-min(x for x, _ in opaque)
        height = max(y for _, y in opaque)-min(y for _, y in opaque)
        assert width > height*1.5
        assert max(y for _, y in opaque) <= FOOT_Y+1


def test_each_fan_slot_and_gesture_has_a_root_overlapping_the_current_body():
    from naiwa.motion import animated_frame, rise_frame
    from naiwa.multi_sprite import WING, WIDTH, shoulder_anchor, rotated_arm
    bodies = [animated_frame(pose, tick) for pose in ("working", "needs_you", "done", "droop")
              for tick in (0, 10, 20)]
    bodies.extend(rise_frame(tick) for tick in (0, 5, 10, 15, 22))
    for body in bodies:
        offset = WING+(WIDTH-len(body[0]))//2
        for left in (True, False):
            x, y = shoulder_anchor(body, left, offset)
            for gesture in ("working", "waiting", "done", "droop"):
                for angle in (-70, -48, -32, -12, 10, 32, 48, 64):
                    pixels, _ = rotated_arm(gesture, angle, left)
                    contact = sum(abs(dx) <= 16 and abs(dy) <= 16 and
                                  0 <= x+dx-offset < len(body[0]) and 0 <= y+dy < len(body) and
                                  body[y+dy][x+dx-offset][3] == 255 for dx, dy, _ in pixels)
                    assert contact >= 60, (gesture, angle, left, contact)


def test_two_codex_windows_keep_independent_compaction_tool_and_completion(tmp_path):
    from naiwa.bus import append_event, EventReader
    from naiwa.notices import action
    root = tmp_path
    for session, workspace in (("window-one", "workspace-a"), ("window-two", "workspace-b")):
        payload = {"session_id": session, "turn_id": "t", "cwd": "D:/"+workspace,
                   "hook_event_name": "UserPromptSubmit"}
        append_event(root, adapt("codex", payload, STAMP))
    append_event(root, adapt("codex", {"session_id": "window-two", "turn_id": "t",
        "hook_event_name": "PreCompact", "trigger": "auto"}, STAMP))
    append_event(root, adapt("codex", {"session_id": "window-one", "turn_id": "t",
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "command"}, STAMP))
    reader = EventReader(root); machine = reader.poll(PhaseMachine())
    agents = {a.workspace_name: a for a in machine.view(NOW).agents}
    assert len(agents) == 2 and machine.view(NOW).active_sessions == 2
    assert agents["workspace-a"].pose == "tool"
    assert action(agents["workspace-b"]) == "正在整理上下文"
    restored = PhaseMachine.from_snapshot(machine.snapshot())
    assert next(a for a in restored.view(NOW).agents if a.workspace_name == "workspace-b").activity == "compacting"
    append_event(root, adapt("codex", {"session_id": "window-two", "turn_id": "t",
        "hook_event_name": "PostCompact"}, STAMP))
    append_event(root, adapt("codex", {"session_id": "window-one", "turn_id": "t",
        "hook_event_name": "Stop", "status": "completed"}, STAMP))
    machine = reader.poll(machine)
    states = {a.workspace_name: a for a in machine.view(NOW).agents}
    assert states["workspace-a"].pose == "done" and states["workspace-b"].pose == "working"
    assert states["workspace-b"].activity == "" and states["workspace-a"].number != states["workspace-b"].number
    # A late compact end can establish a previously unseen window, but cannot
    # revive the old terminal turn or overwrite the other window's activity.
    late = PhaseMachine()
    late.apply(BusEvent("codex", "third", "t", "compact_end", ts=STAMP))
    assert late.view(NOW).pose == "working"
    late.apply(BusEvent("codex", "third", "t", "turn_done", ts=STAMP))
    late.apply(BusEvent("codex", "third", "t", "compact_start", ts=STAMP))
    assert late.view(NOW).pose == "done"


def test_no_work_never_displays_queued_work_or_wait_bubbles():
    notices = StatusNotices()
    notices.observe(demo_machine("multi", NOW).view(NOW).agents, 0)
    assert notices.observe((), .1) == ""
    assert not notices.queue
    notices.observe(demo_machine("mixed", NOW).view(NOW).agents, 1)
    text = notices.observe((), 1.1)
    assert "等你" not in text
    assert "正在" not in text
    assert all("等你" not in item[1] for item in notices.queue)


def test_completion_preempts_a_routine_working_bubble_immediately():
    agent = AgentView("cursor", "a", "t", 1, "working", "", "", 0, 0)
    other = replace(agent, session_id="b", number=2)
    notices = StatusNotices()
    notices.observe((agent, other), 0)
    text = notices.observe((replace(agent, pose="done"), other), .1)
    assert text == "Cursor C1 · 完成"
