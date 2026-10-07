"""Exercise arrival into rest after a cached standing transition, every tick."""
import pytest

from naiwa.motion import FPS, MotionPlayer, animated_frame, rise_frame


@pytest.mark.parametrize("previous", ("done", "needs_you", "droop", "working", "tool"))
def test_lie_arrival_never_replays_the_previous_standing_gesture(previous):
    player = MotionPlayer()
    player.sample("tool", 0, 1)
    for tick in range(80):
        player.sample(previous, 2+tick/FPS, 1 if previous == "tool" else None)
    if previous not in {"working", "tool"}:
        assert player._pose_blend is not None  # The former bug needs this cache.
    reached_floor = False
    lie_cursors = []
    for tick in range(120):
        frame, render_key = player.sample("rest", 6+tick/FPS)
        if render_key[1] == "lie":
            lie_cursors.append(render_key[2])
            if render_key[2] == 0:
                assert frame == rise_frame(0)
                reached_floor = True
        if reached_floor:
            # Compare the real resting clock, including its occasional blink.
            # Catch even one upright replay after arrival without banning blinks.
            rest_tick = int((6+tick/FPS-player.since)*FPS+1e-7) % 80
            expected = rise_frame(0) if render_key[1] == "lie" else animated_frame("rest", rest_tick, 0)
            assert frame == expected, (previous, tick, render_key)
    assert reached_floor
    assert lie_cursors == list(range(22, -1, -1))
    assert player._lie_settle is None and player._rise_tick is None
    assert player.transition is None and player._pose_blend is None


def test_a_new_task_can_still_reverse_lying_down_then_return_to_rest_once():
    player = MotionPlayer()
    player.sample("working", 0)
    for tick in range(27):
        before, _ = player.sample("rest", 2+tick/FPS)
    cursor = player._rise_tick
    resumed, key = player.sample("working", 2+27/FPS)
    assert key[1] == "rise" and key[2] == cursor
    assert resumed == before
    for tick in range(1, 50):
        player.sample("working", 2+(27+tick)/FPS)
    arrived = False
    for tick in range(90):
        frame, key = player.sample("rest", 6+tick/FPS)
        if key[1] == "lie" and key[2] == 0:
            arrived = True
        if arrived:
            assert frame == animated_frame("rest", 0, 0)
    assert arrived and player.transition is None
