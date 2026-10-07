"""Arm lifecycles, physical asset integrity, stable actor and image equivalence."""
from datetime import datetime, timezone
import hashlib
import json

from naiwa.arm_motion import ArmPlayer
from naiwa.choreography import clip, key
from naiwa.motion import MotionPlayer, standing_blink
from naiwa.sprite_assets import ASSET_DIR, pose_frames
from naiwa.sprite_defs import PALETTE, SHADOW, TRANSPARENT


def row(source="cursor", session="first", phase="working", number=1):
    return (source, session, number, phase, "input", "project")


def test_arms_grow_independently_fold_before_gesture_swap_and_retire():
    player = ArmPlayer()
    _, states = player.sample((row(),), 0)
    assert states[0][3] == 0
    _, states = player.sample((row(),), .12)
    assert 0 < states[0][3] < 12
    _, states = player.sample((row(),), .24)
    assert states[0][3] == 12
    player.sample((row(phase="needs_you"), row("codex", "second")), .28)
    _, states = player.sample((row(phase="needs_you"), row("codex", "second")), .4)
    first = next(x for x in states if x[0] == "cursor")
    assert first[2] == "working" and first[3] < 12
    _, states = player.sample((row(phase="needs_you"), row("codex", "second")), .6)
    assert next(x for x in states if x[0] == "cursor")[2:]== ("waiting", 0)
    player.sample((row(phase="needs_you"), row("codex", "second")), .9)
    visual, _ = player.sample((row("codex", "second"),), .94)
    assert len(visual) == 2  # Logical counts can be one while the first arm retires.
    player.sample((row("codex", "second"),), 1.3)
    visual, states = player.sample((row("codex", "second"),), 1.34)
    assert len(visual) == 1 and states[0][3] == 12


def test_arm_reappearing_during_retirement_reverses_without_disappearing():
    player = ArmPlayer()
    player.sample((row(),), 0); player.sample((row(),), .3)
    player.sample((), .34)
    _, before = player.sample((), .42)
    _, after = player.sample((row(),), .46)
    assert after[0][3] > 0 and abs(before[0][3]-after[0][3]) < 8
    _, after = player.sample((row(),), .8)
    assert after[0][3] == 12


def test_physical_clips_are_authored_whole_poses_with_registered_ground_and_provenance():
    from naiwa.whole_motion import put_down
    for kind in ("book","keyboard","notebook","image"):
        frames = clip(kind)
        assert len(frames) == 48
        for tick,frame in enumerate(frames):
            expected = put_down(kind)[tick]
            assert frame == expected
            assert all(p in PALETTE for r in frame for p in r)
            assert frame[212:] == frames[0][212:]
        assert frames[-1] != frames[0]
    manifest = json.loads((ASSET_DIR/"physical-clips.json").read_text())
    assert all(hashlib.sha256((ASSET_DIR/name).read_bytes()).hexdigest() == digest
               for name,digest in manifest["sources"].items())


def test_standing_blink_only_changes_the_registered_eye_regions():
    from naiwa.motion import _eye_landmarks
    original = pose_frames("idle")[0]
    blink, step = standing_blink(original, 155/25)
    assert step == 4 and blink != original
    eyes = _eye_landmarks(original)
    assert all(p == q or any(x0 <= x <= x1 and y0 <= y <= y1 for x0,y0,x1,y1 in eyes)
               for y, (r,s) in enumerate(zip(original,blink)) for x,(p,q) in enumerate(zip(r,s)))
    assert standing_blink(original, 160/25) == (original, 0)


def test_rapid_tools_coalesce_the_destination_and_urgent_wait_bypasses_the_move():
    player = MotionPlayer()
    player.sample("tool", 0, 0); player.sample("tool", 2, 0)
    player.sample("tool", 2.04, 1)
    started = player._physical_since
    for tick in range(1, 14):
        player.sample("tool", 2.04+tick/25, 2 if tick > 5 else 1)
        assert player._physical_since == started
    assert player._physical.target == ("tool", 2)
    previous = player.last
    shown, _ = player.sample("needs_you", 2.64)
    assert player.pose == "needs_you" and player._physical is None and shown == previous


def test_indexed_image_matches_all_palette_alpha_and_pixel_scaling():
    from naiwa.window import _image
    colours = sorted(PALETTE)
    canvas = [colours+[TRANSPARENT]*3]  # 28-pixel aligned width.
    for layer in ("all", "body", "shadow"):
        image = _image(canvas, 2, layer=layer)
        for x, colour in enumerate(canvas[0]):
            expected = (TRANSPARENT if layer == "body" and colour == SHADOW else
                        SHADOW if layer == "shadow" and colour == SHADOW else
                        TRANSPARENT if layer == "shadow" else colour)
            assert image.pixelColor(x*2, 0).getRgb() == expected
            assert image.pixelColor(x*2+1, 1).getRgb() == expected


def test_multiple_agents_do_not_steal_the_main_body_tool_every_event(tmp_path):
    from PySide6.QtWidgets import QApplication
    from naiwa.window import NaiwaWindow
    from naiwa.schema import BusEvent
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path)
    stamp = datetime.now(timezone.utc).isoformat()
    window.machine.apply(BusEvent("codex", "one", "t", "tool_start", ts=stamp, tool_name="Read", tool_call_id="r"))
    window._sync()
    assert window._body_agent == "codex:one"
    window.machine.apply(BusEvent("cursor", "two", "t", "tool_start", ts=stamp, tool_name="Bash", tool_call_id="b"))
    window._sync()
    assert window._body_agent == "codex:one"
    window.machine.apply(BusEvent("codex", "one", "t", "tool_end", ts=stamp, tool_call_id="r"))
    window._sync()
    assert window._body_agent == "cursor:two"
    window.close()


def test_native_reused_layers_match_the_palette_canvas_and_hit_geometry(tmp_path):
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from naiwa.motion import animated_frame
    from naiwa.multi_sprite import compose_motion
    from naiwa.window import NaiwaWindow, _image
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path)
    window._scale, window._ratio = 1, 1
    for tick in (0, 13, 55):
        rows = (row(), row("codex", "second", "done"), ("cursor","nameless",2,"needs_you","input",""))
        window._composition = compose_motion("tool", animated_frame("tool", tick, 1), rows, tick)
        window._canvas = window._composition.canvas
        expected = _image(window._canvas, 1, layer="body").convertToFormat(QImage.Format.Format_RGBA8888)
        actual = window._composite_image().convertToFormat(QImage.Format.Format_RGBA8888)
        assert bytes(actual.constBits()) == bytes(expected.constBits())
    window.close()


def test_lying_down_starts_from_the_displayed_body_without_replacing_the_face():
    player = MotionPlayer()
    before, _ = player.sample("tool", 0, 1)
    after, _ = player.sample("rest", 1)
    assert [r[40:200] for r in after] == before
    assert player.pose == "rest"
    assert player._lie_settle is not None
