import struct

from naiwa.sprites import (BELLY, HAND, MOUTH, TEETH, EYE, FOOT_Y, PALETTE, PLANTED,
    POSES, SHADOW, DURATIONS, WIDTH, HEIGHT, EYE_SHADE, EYE_LIGHT, EYE_DARK, PUPIL,
    body_bottom, frame_at, hit, pose_frames, save_png)


def test_frames_have_consistent_anchor_palette_and_transparency():
    for pose in PLANTED:
        frames = pose_frames(pose)
        assert 2 <= len(frames) <= 4
        assert len(frames) == len(DURATIONS[pose])
        for frame in frames:
            assert body_bottom(frame) == FOOT_Y
            assert not any(pixel[3] == 255 for pixel in frame[0])
            assert not any(row[0][3] == 255 or row[-1][3] == 255 for row in frame)
            assert set(p for row in frame for p in row) <= PALETTE
            assert any(p == BELLY for row in frame for p in row)
            assert any(p == HAND for row in frame for p in row)
        if pose != "done":
            assert any(p == EYE for row in frames[0] for p in row)


def test_laugh_has_a_mouth_teeth_belly_motion_and_planted_feet():
    frames = pose_frames("done")
    assert any(p == MOUTH for row in frames[0] for p in row)
    assert any(p == TEETH for row in frames[0] for p in row)
    assert frames[0] != frames[1]
    for frame in frames:
        assert body_bottom(frame) == body_bottom(pose_frames("idle")[0])
        assert any(p == SHADOW for row in frame for p in row)
    assert set(POSES) == {"idle", "working", "needs_you", "done", "droop"}


def test_blink_is_brief_and_shadow_is_not_a_body_hit():
    durations = DURATIONS["idle"]
    assert frame_at("idle", sum(durations[:3])) == 3
    assert frame_at("idle", sum(durations)) == 0
    assert durations[3] <= 120
    frame = pose_frames("idle")[0]
    for y, row in enumerate(frame):
        for x, pixel in enumerate(row):
            if pixel == SHADOW:
                assert not hit(frame, x, y)


def test_png_is_exported_at_integer_scale(tmp_path):
    path = tmp_path / "pet.png"
    frame = pose_frames("idle")[0]
    save_png(path, frame, scale=3)
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (len(frame[0])*3, len(frame)*3)


def test_dense_thinking_face_keeps_the_approved_asymmetric_eye_proportions():
    from collections import deque
    assert (WIDTH, HEIGHT) == (160, 224)
    frame = pose_frames("working")[0]
    eye_colours = {EYE, EYE_SHADE, EYE_LIGHT, EYE_DARK, PUPIL}
    remaining = {(x, y) for y, row in enumerate(frame[:HEIGHT//2])
                 for x, colour in enumerate(row) if colour in eye_colours}
    groups = []
    while remaining:
        start = remaining.pop()
        queue, group = deque([start]), [start]
        while queue:
            x, y = queue.popleft()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = (x+dx, y+dy)
                    if neighbour in remaining:
                        remaining.remove(neighbour)
                        queue.append(neighbour)
                        group.append(neighbour)
        if any(frame[y][x] == EYE for x, y in group):
            groups.append(group)
    left, right = sorted(sorted(groups, key=len, reverse=True)[:2], key=lambda g: min(x for x, _ in g))
    left_width = max(x for x, _ in left)-min(x for x, _ in left)+1
    right_width = max(x for x, _ in right)-min(x for x, _ in right)+1
    assert right_width >= left_width*1.3
    assert sum(y for _, y in right)/len(right) < sum(y for _, y in left)/len(left)
    crown = min(y for y, row in enumerate(frame) if any(p[3] == 255 for p in row))
    assert min(y for _, y in right) >= crown+4
