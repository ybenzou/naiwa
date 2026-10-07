from naiwa.gaze import approved_eyes, canonical_gaze, eye_boxes
from naiwa.multi_sprite import shoulder_anchor
from naiwa.pixel_frame import PixelFrame
from naiwa.rigid_motion import bitmap, pixels
from naiwa.sprite_assets import ASSET_DIR, _load
from naiwa.sprite_defs import BODY, TRANSPARENT
from naiwa.whole_motion import key, loop

KINDS = ("book", "keyboard", "notebook", "image", "signals", "rising")


def test_all_complete_poses_keep_the_approved_eye_texture_only():
    reference, approved = approved_eyes()
    for kind in KINDS:
        for index in range(16):
            raw = pixels(bitmap(_load(ASSET_DIR/f"whole-{kind}-{index}.png")))
            boxes = eye_boxes(raw)
            assert boxes and len(boxes) == 2, (kind, index)
            actual = key(kind, index)
            for (x0, y0, x1, y1), (rx0, ry0, rx1, ry1) in zip(boxes, approved):
                for y in range(y0, y1+1):
                    sy = ry0+round((y-y0)*(ry1-ry0)/max(1, y1-y0))
                    for x in range(x0, x1+1):
                        sx = rx0+round((x-x0)*(rx1-rx0)/max(1, x1-x0))
                        assert actual[y][x] == reference[sy][sx], (kind, index, x, y)
            for y in range(len(raw)):
                for x in range(raw.width):
                    if not any(l <= x <= r and t <= y <= b for l, t, r, b in boxes):
                        assert actual[y][x] == raw[y][x], (kind, index, x, y)


def test_extending_a_foreground_palm_cannot_move_session_arm_roots():
    body = key("signals", 3)
    altered = [row[:] for row in body]
    _, y = body.rig["body_anchor"]
    # A connected palm changes the opaque left boundary by dozens of pixels.
    for row in altered[y-8:y+9]:
        row[2:85] = [BODY]*83
    altered = pixels(bitmap(altered))
    altered.rig = dict(body.rig)
    for left in (True, False):
        assert shoulder_anchor(body, left, 80) == shoulder_anchor(altered, left, 80)
    # Also protect legacy frames lacking authored metadata.
    legacy = PixelFrame(body.indices, body.width)
    altered.rig = None
    assert shoulder_anchor(legacy, True, 80) == shoulder_anchor(altered, True, 80)


def test_landmarks_follow_the_whole_actor_and_survive_padding_and_blinks():
    from naiwa.motion import FPS, standing_blink
    for pose in ("working", "tool", "needs_you", "done", "droop"):
        for tick in (0, 10, 20):
            frame = loop(pose, tick)
            x, y = frame.rig["body_anchor"]
            assert frame[y][x][3] == 255
            padded = frame.padded(40)
            assert padded.rig["body_anchor"] == [x+40, y]
            for blink_tick in range(152, 160):
                blink, step = standing_blink(frame, blink_tick/FPS)
                assert step
                assert blink.rig == frame.rig
                for left in (True, False):
                    assert shoulder_anchor(blink, left, 80) == shoulder_anchor(frame, left, 80)


def test_every_authored_torso_anchor_keeps_all_fan_slots_connected():
    from naiwa.multi_sprite import rotated_arm
    for kind in KINDS:
        for index in range(16):
            frame = key(kind, index)
            for left in (True, False):
                x, y = shoulder_anchor(frame, left, 0)
                for angle in (-70, -48, -32, -12, 10, 32, 48, 64):
                    for gesture in ("working", "waiting", "done", "droop"):
                        arm, _ = rotated_arm(gesture, angle, left)
                        contact = sum(abs(dx) <= 16 and abs(dy) <= 16 and
                                      0 <= x+dx < frame.width and 0 <= y+dy < len(frame) and
                                      frame[y+dy][x+dx][3] == 255 for dx, dy, _ in arm)
                        assert contact >= 60, (kind, index, left, angle, gesture, contact)


def test_eye_detection_handles_a_crouch_and_ignores_closed_eyes():
    reference, boxes = approved_eyes()
    moved = [[TRANSPARENT]*160 for _ in range(224)]
    for y, row in enumerate(reference):
        for x, colour in enumerate(row):
            if y+65 < 224 and x-10 >= 0:
                moved[y+65][x-10] = colour
    expected = [(l-10, t+65, r-10, b+65) for l, t, r, b in boxes]
    assert eye_boxes(moved) == expected
    from naiwa.multi_sprite import rest_frame
    closed = pixels(bitmap(rest_frame(2)))
    assert canonical_gaze(closed) == closed
