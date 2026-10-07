"""Regression checks on actual tool/think transitions, including every bridge frame."""
import pytest

from naiwa.motion import MotionPlayer, TRANSITION_FRAMES, FPS, animated_frame, prepare_animation
from naiwa.sprite_defs import PALETTE


def changes(a, b, end=224):
    return sum((p[3] == 255) != (q[3] == 255) for ra, rb in zip(a[:end], b[:end]) for p, q in zip(ra, rb))


@pytest.mark.parametrize("variant", range(4))
@pytest.mark.parametrize("position", (0, 55))
def test_tool_to_thinking_keeps_first_frame_and_avoids_a_whole_body_texture_flash(variant, position):
    prepare_animation()
    player = MotionPlayer()
    player.sample("tool", 0, variant)
    time = 4+position/FPS
    previous, _ = player.sample("tool", time, variant)
    transition = []
    for tick in range(85):
        current, _ = player.sample("working", time+(tick+1)/FPS)
        if player.pose == "working":
            if not transition:
                assert current == previous
            transition.append(current)
        previous = current
    assert len(transition) > TRANSITION_FRAMES
    for before, after in zip(transition, transition[1:]):
        # The former seven-frame bridge changed 7900-9000 pixels on its midpoint.
        assert changes(before, after) < 4500
        assert changes(before, after, 95) < 3200  # Whole head lowers with the crouch.
        assert all(p in PALETTE for row in after for p in row)
        assert sum(p[3] == 255 for row in after for p in row) > 5000
    assert player._physical is None and player.pose == "working"
    assert player._ground_kind == ("book","keyboard","notebook","image")[variant]


def test_short_tool_gap_keeps_motion_but_a_new_action_and_wait_take_effect():
    player = MotionPlayer()
    player.sample("tool", 0, 0)
    player.sample("tool", 5, 0)
    player.sample("working", 5.12)
    assert player.pose == "tool"
    before, _ = player.sample("tool", 5.24, 0)
    after, _ = player.sample("tool", 5.28, 1)
    assert before == after and player._variant == 1
    after, _ = player.sample("needs_you", 5.32)
    assert player.pose == "needs_you" and after == before
    player.sample("done", 5.36)
    assert player.pose == "done"  # No grace period for wait, completion or failure.


def test_new_tool_interrupts_the_return_to_thinking_from_the_displayed_frame():
    player = MotionPlayer()
    player.sample("tool", 0, 0)
    player.sample("tool", 2, 0)
    player.sample("working", 2.4)
    before, _ = player.sample("working", 2.6)
    after, _ = player.sample("tool", 2.64, 1)
    assert changes(after, before) < 4500
    assert player._physical_since == 2.4  # Continue the prop move without restarting.
    before, _ = player.sample("tool", 2.84, 1)
    after, _ = player.sample("tool", 2.88, 2)
    assert changes(after, before) < 4500
    for tick in range(1, 85):
        frame, _ = player.sample("tool", 2.88+tick/FPS, 2)
        assert changes(after, frame) < 4500
        after = frame


def test_actual_playback_and_compilers_never_use_body_meshes_or_colour_blending(monkeypatch):
    import naiwa.motion as motion
    from naiwa.choreography import generated_clip, generated_chin
    def forbidden(*args, **kwargs):
        raise AssertionError("A full-body mesh or scanline warp reached playback")
    for name in ("tween", "_material_fields", "_warp", "_breathe", "_colour"):
        monkeypatch.setattr(motion, name, forbidden)
    for variant in range(4):
        player = MotionPlayer()
        for tick in range(110):
            player.sample("tool" if tick < 45 else "working", tick/FPS, variant)
        motion.generate_frame("tool", 15, variant)
    for kind in ("book", "keyboard", "notebook"):
        assert len(generated_clip(kind)) == 48
    assert len(generated_chin()) == 16
    for pose in ("working", "needs_you", "done", "droop", "stale", "rest"):
        motion.generate_frame(pose, 12)
    for tick in range(23):
        motion.generate_rise(tick)
