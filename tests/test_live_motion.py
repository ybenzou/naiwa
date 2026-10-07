"""Regression coverage for missed short actions, pinned speech and frame continuity."""
from datetime import datetime, timezone
import io
import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from naiwa.hook_input import MetadataReader, read_metadata
from naiwa.motion import MotionPlayer, animated_frame
from naiwa.phase import PhaseMachine
from naiwa.schema import BusEvent
from naiwa.sprite_defs import PALETTE

NOW = 1791180000.0


def event(machine, name, offset=0, turn="t", session="one", **kwargs):
    machine.apply(BusEvent("cursor", session, turn, name,
        ts=datetime.fromtimestamp(NOW+offset, timezone.utc).isoformat(), **kwargs))


def test_large_hook_body_is_streamed_without_losing_metadata_after_it():
    payload = {"prompt": "秘密正文"*900000, "conversation_id": "one", "generation_id": "t",
               "tool_input": {"nested": [{"quote": '\\"{}[]中文'}]}, "workspace_roots": ["/private/项目"]}
    data = json.dumps(payload, ensure_ascii=False).encode()
    result, status, size = read_metadata(io.BytesIO(b"\xef\xbb\xbf"+data))
    assert len(data) > 4*1024*1024
    assert status == "ok" and size > len(data)
    assert result["conversation_id"] == "one"
    assert result["workspace_roots"] == ["/private/项目"]
    assert result["prompt"] is None and result["tool_input"] is None
    assert "秘密" not in str(result)


def test_streaming_escapes_cross_chunk_boundaries_and_invalid_input_is_reported():
    class SmallChunks(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 3))
    data = json.dumps({"text": '\\"[]{}💚', "session_id": "中文", "turn_id": "t"}, ensure_ascii=False).encode()
    result, status, _ = read_metadata(SmallChunks(data))
    assert status == "ok" and result["session_id"] == "中文"
    assert read_metadata(io.BytesIO(b'{"session_id":'))[1] == "invalid_json"
    assert read_metadata(io.BytesIO(b""))[1] == "empty"


def test_fast_tools_and_completion_remain_in_details_and_survive_snapshot():
    machine = PhaseMachine()
    event(machine, "turn_start")
    event(machine, "tool_start", .01, tool_name="view_image", tool_call_id="image")
    event(machine, "tool_end", .02, tool_name="view_image", tool_call_id="image")
    assert machine.view(NOW+1).pose == "working"
    assert machine.details(NOW+1)[0]["last_tool"] == "view_image"
    event(machine, "turn_done", 2)
    assert machine.view(NOW+20).pose == "idle"
    restored = PhaseMachine.from_snapshot(machine.snapshot())
    row = restored.details(NOW+20)[0]
    assert row["pose"] == "done" and row["elapsed"] == 2
    assert row["recent_actions"][0]["result"] == "done"
    assert not restored.details(NOW+1000)


def test_missing_start_recovers_new_turn_without_reviving_retired_turn():
    machine = PhaseMachine()
    event(machine, "turn_start")
    event(machine, "turn_done", 1)
    event(machine, "tool_start", 2, turn="new", tool_name="Read", tool_call_id="read")
    assert machine.view(NOW+2).pose == "tool"
    assert machine.view(NOW+2).agents[0].label == "C1"
    event(machine, "turn_done", 3, turn="new")
    event(machine, "tool_start", 4, turn="t", tool_name="old")
    assert machine.turns["cursor:one"].turn_id == "new"
    event(machine, "tool_start", 300, turn="third", tool_name="Bash")
    assert machine.view(NOW+300).tool_name == "Bash"


def test_parallel_identical_tools_keep_distinct_results_without_duplicate_history():
    machine = PhaseMachine()
    event(machine, "turn_start")
    event(machine, "tool_start", .1, tool_name="Bash", tool_call_id="first")
    event(machine, "tool_start", .2, tool_name="Bash", tool_call_id="second")
    event(machine, "tool_end", .3, tool_call_id="first")
    rows = machine.details(NOW+1)[0]["recent_actions"]
    assert [x["result"] for x in rows] == ["done", "running"]
    event(machine, "tool_end", .4, tool_name="Bash", tool_call_id="first")
    assert len(machine.details(NOW+1)[0]["recent_actions"]) == 2
    assert machine.view(NOW+1).pose == "tool"


def test_continuous_pixel_loops_have_small_adjacent_changes_and_no_blank_frames():
    for pose in ("working", "tool", "needs_you", "droop", "stale", "rest"):
        frames = [animated_frame(pose, t) for t in (0, 1, 2, 38, 39, 40, 78, 79, 0)]
        for frame in frames:
            assert sum(p[3] == 255 for row in frame for p in row) > 5000
            assert all(p in PALETTE for row in frame for p in row)
        for before, after in ((frames[0], frames[1]), (frames[1], frames[2]),
                              (frames[3], frames[4]), (frames[4], frames[5]), (frames[7], frames[8])):
            changed = sum(a != b for ra, rb in zip(before, after) for a, b in zip(ra, rb))
            assert changed < 4500, (pose, changed)
    assert animated_frame("rest", 40, 0) != animated_frame("rest", 40, 2)


def test_motion_clock_does_not_restart_and_brief_tools_have_a_visible_action():
    player = MotionPlayer()
    player.sample("working", 0)
    player.sample("working", .5)
    assert player.since == 0
    player.sample("tool", 1)
    player.sample("working", 1.1)
    assert player.pose == "tool"
    player.sample("working", 2.2)
    assert player.pose == "working"


def test_tool_loops_use_complete_dense_poses_and_distinct_props():
    from naiwa.whole_motion import key
    frames = [animated_frame("tool",0,variant) for variant in range(4)]
    for kind,frame in zip(("book","keyboard","notebook","image"),frames):
        assert frame == key(kind,0)
        assert frame.rig["kind"] == kind
    assert len({f.indices for f in frames}) == 4


def test_tool_families_choose_distinct_actions_and_switch_from_current_frame():
    from naiwa.notices import tool_variant
    assert tool_variant("Read") == tool_variant("read_file") == tool_variant("Grep") == 0
    assert tool_variant("Bash") == tool_variant("functions.exec_command") == 1
    assert tool_variant("ApplyPatch") == tool_variant("apply_patch") == tool_variant("Write") == 2
    assert tool_variant("view_image") == 3
    player = MotionPlayer()
    player.sample("tool", 0, 0)
    before, _ = player.sample("tool", 1, 0)
    after, _ = player.sample("tool", 1.04, 2)
    assert before == after  # A different activity must not flash into existence.
    assert player.since == 0
    player.sample("tool", 1.5, 2)
    player.sample("tool", 2, 2)
    assert player.since == 0 and player._variant == 2
    player = MotionPlayer()
    player.sample("rest", 0)
    for tick in range(60):
        frame, _ = player.sample("tool", 1+tick/25, 1)
        assert any(p[3] == 255 for row in frame for p in row)


def test_all_tool_loops_keep_palette_and_join_without_a_large_jump():
    for variant in range(4):
        before, after = animated_frame("tool", 79, variant), animated_frame("tool", 0, variant)
        assert all(p in PALETTE for row in after for p in row)
        assert sum(a != b for ra, rb in zip(before, after) for a, b in zip(ra, rb)) < 4500


def test_idle_variant_change_and_interrupted_rise_preserve_the_current_frame():
    player = MotionPlayer()
    player.sample("rest", 0)
    before, _ = player.sample("rest", 15.9)
    after, _ = player.sample("rest", 16)
    assert after == before
    player = MotionPlayer()
    player.sample("rest", 0)
    player.sample("working", 1)
    before, _ = player.sample("working", 1.32)
    after, _ = player.sample("rest", 1.4)
    assert after == before


def test_static_faces_and_contact_pixels_do_not_wobble_during_breathing():
    from naiwa.sprite_assets import pose_frames
    from naiwa.multi_sprite import rest_frame
    from naiwa.motion import REST_EYES
    # Whole actor motion moves face and hands together; it may translate one
    # native pixel. It never swaps the face patch or changes individual limbs.
    from naiwa.rigid_motion import composite
    from naiwa.sprite_defs import SHADOW,TRANSPARENT
    for pose in ("working","needs_you","droop","stale"):
        original = animated_frame(pose,0)
        actor = [[TRANSPARENT if p == SHADOW else p for p in r] for r in original]
        ground = [[SHADOW if p == SHADOW else TRANSPARENT for p in r] for r in original]
        for tick in range(80):
            frame = animated_frame(pose,tick)
            assert abs(frame.rig["dx"]) <= 1
            assert frame == composite(ground,[(actor,dict(dx=frame.rig["dx"]))])
            assert frame[212:] == original[212:]
    original = rest_frame(0)
    for variant in range(3):
        for tick in range(80):
            frame = animated_frame("rest", tick, variant)
            assert frame[198:] == original[198:], (variant, tick)
            if variant == 2:
                for x0, y0, x1, y1 in REST_EYES:
                    assert [row[x0:x1] for row in frame[y0:y1]] == [row[x0:x1] for row in original[y0:y1]]


def test_every_rest_frame_and_blink_boundary_has_a_small_change():
    # The earlier test sampled only nine times, missing the badly distorted
    # stretch frames and the instantaneous blink in the middle of the loop.
    for variant in range(3):
        frames = [animated_frame("rest", tick, variant) for tick in range(80)]
        for tick, frame in enumerate(frames):
            changed = sum(a != b for ra, rb in zip(frames[tick-1], frame) for a, b in zip(ra, rb))
            assert changed < 1500, (variant, tick, changed)
    assert animated_frame("rest", 0, 2) == animated_frame("rest", 79, 2)


def test_resting_hand_stays_in_front_of_head_without_crossing_eyes():
    from naiwa.motion import _raised_rest_arm, REST_EYES
    from naiwa.multi_sprite import rest_frame
    original = rest_frame(0)
    contacts = 0
    for tick in range(80):
        arm = _raised_rest_arm(tick)
        frame = animated_frame("rest", tick, 2)
        for y in range(90, 154):
            for x in range(104):
                if arm[y][x][3] == 255 and original[y][x][3] == 255:
                    contacts += 1
                    assert frame[y][x] == arm[y][x], (tick, x, y)
        for x0, y0, x1, y1 in REST_EYES:
            assert all(arm[y][x][3] == 0 for y in range(y0, y1) for x in range(x0, x1))
    assert contacts > 1000


def test_rest_schedule_stretches_once_then_returns_to_calm_without_restarting():
    player = MotionPlayer()
    player.sample("rest", 0)
    for before_time, boundary, after_time, variant in ((15.96, 16, 16.4, 1),
                                                      (31.96, 32, 32.4, 2),
                                                      (35.16, 35.2, 35.6, 0)):
        before, _ = player.sample("rest", before_time)
        after, _ = player.sample("rest", boundary)
        assert after == before
        player.sample("rest", after_time)
        assert player._variant == variant and player.since == 0
    player.sample("rest", 40)
    assert player._variant == 0


def test_identical_static_pixels_share_a_render_key():
    player = MotionPlayer()
    first, first_key = player.sample("rest", 0)
    next_frame, next_key = player.sample("rest", .04)
    assert next_frame == first and next_key == first_key


def test_arm_tags_keep_identity_and_colour_and_stay_still_during_waving():
    from naiwa.multi_sprite import compose, composition_key
    from naiwa.phase import AgentView
    first = AgentView("cursor", "one", "t", 1, "done", "", "", 0, 0, "app")
    second = AgentView("cursor", "two", "t", 2, "working", "", "", 0, 0, "api")
    a = compose("working", 0, composition_key((first,)), 0).labels[0]
    b = next(x for x in compose("working", 0, composition_key((first, second)), 25).labels if x[0] == first.key)
    assert a[:3] == b[:3] and a[4] == b[4]
    # A changed fan may reposition captions to clear the newly exposed arm,
    # while animation alone must not move the resulting caption slots.
    c = next(x for x in compose("working", 0, composition_key((first, second)), 50).labels if x[0] == first.key)
    assert b == c


def test_pinned_details_restore_and_short_actions_remain_readable(tmp_path):
    from PySide6.QtWidgets import QApplication
    from naiwa.window import NaiwaWindow
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    event(window.machine, "turn_start", time.time()-NOW, workspace_name="FE")
    event(window.machine, "tool_start", time.time()-NOW, tool_name="view_image", tool_call_id="i")
    event(window.machine, "tool_end", time.time()-NOW, tool_name="view_image", tool_call_id="i")
    window.toggle_details()
    assert any("查看图片" in s for s in window._detail_lines())
    window.close()
    restored = NaiwaWindow(root=tmp_path, demo=True)
    assert restored.detail_open
    restored.close()


def test_file_notifications_update_live_window_without_waiting_for_poll(tmp_path):
    from PySide6.QtWidgets import QApplication
    from naiwa.bus import append_event
    from naiwa.window import NaiwaWindow
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path)
    window._poll.stop()
    stamp = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()
    append_event(tmp_path, BusEvent("cursor", "live", "t", "tool_start", ts=stamp, tool_name="Read"))
    while window.machine.last_seq < 1 and time.monotonic()-start < 1:
        app.processEvents()
        time.sleep(.005)
    assert window._view().tool_name == "Read"
    assert time.monotonic()-start < .5
    window.close()
