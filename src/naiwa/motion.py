"""Continuous pixel-grid animation of the approved, generated sprite artwork.

Playback uses complete connected illustrated poses, never separate limb rigs.
Historical mesh helpers remain for baseline comparison scripts only; regression
tests forbid them in playback and in current animation compilation.
"""
from functools import lru_cache
import base64
import hashlib
import json
import math
import zlib

from naiwa.sprite_assets import ASSET_DIR, _load, pose_frames
from naiwa.sprite_defs import *
from naiwa.pixel_frame import PixelFrame

FPS = 25
TRANSITION_FRAMES = 16
TOOL_GAP_SECONDS = .36


def padded_frame(frame):
    return frame.padded(40) if isinstance(frame, PixelFrame) else [[TRANSPARENT]*40+row+[TRANSPARENT]*40 for row in frame]


@lru_cache(maxsize=42)
def asset(kind, index):
    width = 160 if kind in {"tool", "read", "type", "write"} else 240
    return _load(ASSET_DIR/f"motion-{kind}-{index}.png", width, HEIGHT)


@lru_cache(maxsize=48)
def registration(kind, index):
    frame = asset(kind, index)
    return _bounds(frame)


def _bounds(frame):
    spans = []
    for row in frame:
        xs = [x for x, p in enumerate(row) if p[3] == 255]
        spans.append((min(xs), max(xs)) if xs else None)
    ys = [y for y, span in enumerate(spans) if span]
    return spans, min(ys), max(ys)


OPAQUE = tuple(p for p in PALETTE if p[3] == 255)
MATERIALS = (
    {BODY, LIGHT, HIGHLIGHT, SHADE, DEEP, BODY_MID},
    {BELLY, BELLY_SHADE, BELLY_MID}, {HAND, HAND_LIGHT, HAND_DARK},
    {EYE, EYE_LIGHT, EYE_SHADE, EYE_DARK}, {CYAN, CYAN_DARK},
)
MATERIAL_INDEX = {colour: i for i, group in enumerate(MATERIALS) for colour in group}
for _colour_value in sorted(PALETTE-set(MATERIAL_INDEX)):
    MATERIAL_INDEX[_colour_value] = len(set(MATERIAL_INDEX.values()))


def _material_fields(frame):
    """Signed contour distances keep material handoffs spatially coherent."""
    labels = bytes(MATERIAL_INDEX[p] for row in frame for p in row)
    return _fields_from_labels(labels, len(frame[0]))


@lru_cache(maxsize=1)
def _contour_manifest():
    path = ASSET_DIR/"motion-contours.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    expected = hashlib.sha256((ASSET_DIR/"motion-loops.json").read_bytes()).hexdigest()
    physical = hashlib.sha256((ASSET_DIR/"physical-clips.json").read_bytes()).hexdigest()
    if (raw.get("version") != 1 or raw.get("motion_sha256") != expected or
            raw.get("physical_sha256") != physical or
            raw.get("materials") != [MATERIAL_INDEX[p] for p in sorted(PALETTE)]):
        return None  # Local regenerated artwork can safely use the fallback.
    return raw


@lru_cache(maxsize=24)
def _fields_from_labels(labels, width):
    manifest = _contour_manifest()
    key = hashlib.blake2s(labels, digest_size=16).hexdigest()
    if manifest and key in manifest["frames"]:
        data = zlib.decompress(base64.b64decode(manifest["frames"][key]))
        size = width*HEIGHT
        if len(data) != size*len(set(MATERIAL_INDEX.values())):
            raise ValueError("Invalid precompiled material contours")
        return tuple(data[i:i+size] for i in range(0, len(data), size))
    return generate_material_fields(labels, width)


def generate_material_fields(labels, width):
    """Offline native-grid contours; fallback for an interrupted transition."""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter, QRegion
    area = QRect(0, 0, width, HEIGHT)
    regions = [QRegion() for _ in set(MATERIAL_INDEX.values())]
    for y in range(HEIGHT):
        row = labels[y*width:(y+1)*width]
        start, previous = 0, row[0]
        for x in range(1, width+1):
            current = row[x] if x < width else None
            if current != previous:
                regions[previous] += QRegion(start, y, x-start, 1)
                start, previous = x, current
    fields = []
    for region in regions:
        image = QImage(width, HEIGHT, QImage.Format.Format_Grayscale8)
        image.fill(QColor(116, 116, 116))
        if not region.isEmpty():
            painter = QPainter(image)
            expanded = [region]
            for step in range(12):
                r = expanded[-1]
                expanded.append((r | r.translated(1, 0) | r.translated(-1, 0) |
                                 r.translated(0, 1) | r.translated(0, -1)) & QRegion(area))
            for step in range(12, -1, -1):
                painter.setClipRegion(expanded[step])
                painter.fillRect(area, QColor(128-step, 128-step, 128-step))
            eroded = region
            for step in range(1, 13):
                painter.setClipRegion(eroded)
                painter.fillRect(area, QColor(128+step, 128+step, 128+step))
                eroded &= (eroded.translated(1, 0) & eroded.translated(-1, 0) &
                           eroded.translated(0, 1) & eroded.translated(0, -1))
                if eroded.isEmpty():
                    break
            painter.end()
        data, stride = bytes(image.constBits()), image.bytesPerLine()
        fields.append(b"".join(data[y*stride:y*stride+width] for y in range(HEIGHT)))
    return tuple(fields)


@lru_cache(maxsize=8192)
def _colour(a, b, step, total):
    if a == b:
        return a
    if a[3] != 255 or b[3] != 255:
        return a if step*2 < total else b
    family = next((group for group in MATERIALS if a in group and b in group), None)
    if family is None:
        return a if step*2 < total else b
    rgb = tuple(round((a[i]*(total-step)+b[i]*step)/total) for i in range(3))
    return min(family, key=lambda p: sum((p[i]-rgb[i])**2 for i in range(3)))


def _eye_landmarks(frame):
    """Match the two approved green eyes before changing their texture."""
    if len(frame[0]) != WIDTH:
        return None
    from naiwa.gaze import eye_boxes
    return eye_boxes(frame)


def standing_blink(frame, now):
    """An occasional blink using the existing closed-eye texture, eyes only."""
    if len(frame[0]) != WIDTH:
        return frame, 0
    cycle = int(now*FPS) % 173
    step = {152: 1, 153: 2, 154: 3, 155: 4, 156: 4, 157: 3, 158: 2, 159: 1}.get(cycle, 0)
    if not step:
        return frame, 0
    eyes = _eye_landmarks(frame)
    if not eyes:
        return frame, 0
    closed = pose_frames("idle")[3]
    reference = _eye_landmarks(pose_frames("idle")[0])
    result = [r[:] for r in frame]
    for (x0, y0, x1, y1), (rx0, ry0, rx1, ry1) in zip(eyes, reference):
        for y in range(y0, y0+round((y1-y0+1)*step/4)):
            sy = ry0+round((y-y0)*(ry1-ry0)/max(1, y1-y0))
            for x in range(x0, x1+1):
                sx = rx0+round((x-x0)*(rx1-rx0)/max(1, x1-x0))
                result[y][x] = closed[sy][sx]
    from naiwa.rigid_motion import pixels, bitmap
    result = pixels(bitmap(result))
    result.rig = getattr(frame, "rig", None)
    return result, step


def _segment_map(start, end, source_start, source_end):
    return [round(source_start+(p-start)/max(1, end-start)*(source_end-source_start))
            for p in range(start, end+1)]


def tween(a, b, step, total, bounds_a=None, bounds_b=None, *, transition=False, fields=None, landmarks=None):
    """Register scan-line meshes before palette-limited texture interpolation."""
    if step <= 0:
        return a
    if step >= total:
        return b
    width = len(a[0])
    if len(b[0]) != width:
        raise ValueError("Animation meshes must have the same canvas size")
    pixels_a = a.materialize() if isinstance(a, PixelFrame) else a
    pixels_b = b.materialize() if isinstance(b, PixelFrame) else b
    sa, ta, ba = bounds_a or _bounds(a)
    sb, tb, bb = bounds_b or _bounds(b)
    mix = step/total
    if transition and fields is None:
        fields = (_material_fields(a), _material_fields(b))
    top, bottom = round(ta*(1-mix)+tb*mix), round(ba*(1-mix)+bb*mix)
    eyes_a, eyes_b = landmarks if landmarks else (_eye_landmarks(a), _eye_landmarks(b)) if transition else (None, None)
    vertical = None
    if eyes_a and eyes_b:
        # Head, eyes, shoulder and soles travel independently. Stretching every
        # scanline from the head top to the feet misregistered the eyes mid-pose.
        anchors_a = (ta, eyes_a[1][1], eyes_a[1][3], eyes_a[0][3], 95, 192, ba)
        anchors_b = (tb, eyes_b[1][1], eyes_b[1][3], eyes_b[0][3], 95, 192, bb)
        if all(x < y for values in (anchors_a, anchors_b) for x, y in zip(values, values[1:])):
            middle = [round(x*(1-mix)+y*mix) for x, y in zip(anchors_a, anchors_b)]
            vertical = {}
            for at in range(len(middle)-1):
                aa = _segment_map(middle[at], middle[at+1], anchors_a[at], anchors_a[at+1])
                ab = _segment_map(middle[at], middle[at+1], anchors_b[at], anchors_b[at+1])
                vertical.update((y, (ya, yb)) for y, ya, yb in zip(range(middle[at], middle[at+1]+1), aa, ab))
    result = [[TRANSPARENT]*width for _ in range(HEIGHT)]
    for y in range(top, bottom+1):
        ratio = (y-top)/max(1, bottom-top)
        ya, yb = round(ta+ratio*(ba-ta)), round(tb+ratio*(bb-tb))
        if vertical:
            ya, yb = vertical[y]
        la, ra = sa[ya] or (width//2, width//2)
        lb, rb = sb[yb] or (width//2, width//2)
        left, right = round(la*(1-mix)+lb*mix), round(ra*(1-mix)+rb*mix)
        horizontal = None
        if vertical and y < 80:
            xa = [la]+[min(ra, max(la, eye[edge])) for eye in eyes_a for edge in (0, 2)]+[ra]
            xb = [lb]+[min(rb, max(lb, eye[edge])) for eye in eyes_b for edge in (0, 2)]+[rb]
            middle = [round(x*(1-mix)+z*mix) for x, z in zip(xa, xb)]
            horizontal = {}
            for at in range(len(middle)-1):
                aa = _segment_map(middle[at], middle[at+1], xa[at], xa[at+1])
                ab = _segment_map(middle[at], middle[at+1], xb[at], xb[at+1])
                horizontal.update((x, (ax, bx)) for x, ax, bx in zip(range(middle[at], middle[at+1]+1), aa, ab))
        for x in range(max(0, left), min(width-1, right)+1):
            fraction = (x-left)/max(1, right-left)
            ax, bx = horizontal[x] if horizontal else (round(la+fraction*(ra-la)), round(lb+fraction*(rb-lb)))
            pa, pb = pixels_a[ya][ax], pixels_b[yb][bx]
            fa, fb = MATERIAL_INDEX[pa], MATERIAL_INDEX[pb]
            if transition and fa != fb:
                source_at, target_at = ya*width+ax, yb*width+bx
                da, db = fields
                old = da[fa][source_at]-da[fb][source_at]
                new = db[fb][target_at]-db[fa][target_at]
                result[y][x] = pb if old*(total-step) < new*step else pa
            else:
                result[y][x] = _colour(pa, pb, step, total)
    for y in range(FOOT_Y+4, FOOT_Y+9):
        result[y] = [p if p == SHADOW else TRANSPARENT for p in b[y]]
    return result


@lru_cache(maxsize=400)
def sequence(kind, tick):
    # A reading pause, brisk alternating typing, and a writing stroke/rest.
    # Each cycle joins at zero velocity; different tools no longer share a beat.
    progress = tick/80
    if kind == "type":
        progress *= 2
    elif kind == "read":
        progress = max(0, min(1, (progress-.12)/.76))
    elif kind == "write":
        progress = progress**1.35
    position = (1-math.cos(progress*math.tau))*2.5
    i = min(4, int(position))
    part = round((position-i)*12)
    return tween(asset(kind, i), asset(kind, i+1), part, 12,
                 registration(kind, i), registration(kind, i+1))


def _warp(frame, breath=0.0, lean=0.0, lift=0.0):
    width = len(frame[0])
    result = [[TRANSPARENT]*width for _ in range(HEIGHT)]
    for y in range(HEIGHT):
        # Feet stay planted during breathing; the whole body lifts only for a hop.
        sy = round(FOOT_Y+(y+lift-FOOT_Y)/(1+breath/200))
        if not 0 <= sy <= FOOT_Y:
            continue
        shift = round(lean*max(0, (FOOT_Y-sy)/190))
        for x in range(max(0, shift), min(width, width+shift)):
            p = frame[sy][x-shift]
            if p[3] == 255:
                result[y][x] = p
    for y in range(FOOT_Y+4, FOOT_Y+9):
        result[y] = [p if p == SHADOW else TRANSPARENT for p in frame[y]]
    return result


REST_EYES = ((54, 99, 87, 130), (13, 126, 35, 150))
BLINK_STEPS = {64: 1, 65: 2, 66: 3, 67: 4, 68: 4, 69: 3, 70: 2, 71: 1}


def _blink_rest(frame, step):
    """Reveal the generated closed-eye texture progressively, in its own region."""
    if not step:
        return frame
    from naiwa.multi_sprite import rest_frame
    closed = rest_frame(2)
    result = [row[:] for row in frame]
    for x0, y0, x1, y1 in REST_EYES:
        bottom = y0+round((y1-y0)*step/4)
        for y in range(y0, bottom):
            for x in range(x0, x1):
                if frame[y][x][3] == 255 and closed[y][x][3] == 255:
                    result[y][x] = closed[y][x]
    return result


def _breathe(frame, phase, reclining=False):
    """A small torso breath: face, hands supporting the chin and feet stay registered."""
    start, end = (160, 198) if reclining else (95, 192)
    result = [row[:] for row in frame]
    for y in range(start, end):
        displacement = .8*math.sin(phase)*math.sin((y-start)/(end-start)*math.pi)**2
        result[y] = frame[round(y-displacement)][:]
    return result


@lru_cache(maxsize=1)
def _waiting_layers():
    """Extract the approved open palm and wrist; retain the head underneath."""
    original = pose_frames("needs_you")[0]
    clean = pose_frames("idle")[0]
    under = [row[:] for row in original]
    arm = [[TRANSPARENT]*WIDTH for _ in range(HEIGHT)]
    hand = {(x, y) for y in range(76, 134) for x in range(50)
            if original[y][x] in MATERIALS[2]}
    for y in range(75, 137):
        for x in range(50):
            palm = (x, y) in hand or original[y][x] == OUTLINE and any(
                (x+dx, y+dy) in hand for dx in (-1, 0, 1) for dy in (-1, 0, 1))
            wrist = 105 <= y <= 132 and 18 <= x <= 39 and original[y][x][3] == 255
            if palm or wrist:
                arm[y][x] = original[y][x]
                under[y][x] = clean[y][x]
    return under, arm


@lru_cache(maxsize=80)
def _waiting_wave(tick):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QImage, QTransform
    from naiwa.sprite_assets import PIXEL_LOOKUP
    under, arm = _waiting_layers()
    # Two small beckons, followed by a still open palm; no full-pose repaint.
    progress = min(1, tick/42)
    angle = 7*math.sin(progress*math.tau*2)*math.sin(progress*math.pi)**2
    transform = QTransform().translate(32, 123).rotate(angle).translate(-32, -123)
    data = b"".join(bytes(p) for row in arm for p in row)
    image = QImage(data, WIDTH, HEIGHT, WIDTH*4, QImage.Format.Format_RGBA8888)
    matrix = QImage.trueMatrix(transform, WIDTH, HEIGHT)
    origin = transform.map(QPointF(0, 0))-matrix.map(QPointF(0, 0))
    ox, oy = round(origin.x()), round(origin.y())
    image = image.transformed(transform, Qt.TransformationMode.FastTransformation).convertToFormat(QImage.Format.Format_RGBA8888)
    data, stride = bytes(image.constBits()), image.bytesPerLine()
    result = [r[:] for r in under]
    for y in range(image.height()):
        dy = y+oy
        if not 75 <= dy < 192:
            continue
        for x in range(image.width()):
            dx, at = x+ox, y*stride+x*4
            if 0 <= dx < WIDTH and data[at+3] == 255:
                result[dy][dx] = PIXEL_LOOKUP[data[at:at+4]]
    return result


@lru_cache(maxsize=1)
def _rest_arm_layers():
    """Separate the existing resting arm; borrow the generated clean belly below it."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QPolygon, QRegion
    from naiwa.multi_sprite import rest_frame
    original = rest_frame(0)
    clean = asset("stretch", 3)
    region = QRegion(QPolygon([QPoint(x, y) for x, y in (
        (147, 119), (160, 123), (164, 140), (160, 153), (143, 170),
        (134, 179), (113, 178), (101, 171), (101, 155), (111, 146), (118, 133))]))
    under = [row[:] for row in original]
    arm = [[TRANSPARENT]*240 for _ in range(HEIGHT)]
    for y in range(119, 180):
        for x in range(101, 165):
            if region.contains(QPoint(x, y)) and original[y][x][3] == 255:
                arm[y][x] = original[y][x]
                if clean[y][x][3] == 255:
                    under[y][x] = clean[y][x]
    return under, arm


@lru_cache(maxsize=80)
def _raised_rest_arm(tick):
    """A foreground hand arc that reaches the temple without crossing either eye."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QImage, QTransform
    from naiwa.sprite_assets import PIXEL_LOOKUP
    _, arm = _rest_arm_layers()
    angle = 65*(1-math.cos(tick/80*math.tau))/2
    transform = QTransform().translate(164, 127).rotate(angle).translate(-164, -127)
    raw = b"".join(bytes(p) for row in arm for p in row)
    image = QImage(raw, 240, HEIGHT, 240*4, QImage.Format.Format_RGBA8888)
    matrix = QImage.trueMatrix(transform, image.width(), image.height())
    transformed = image.transformed(transform, Qt.TransformationMode.FastTransformation).convertToFormat(QImage.Format.Format_RGBA8888)
    origin = transform.map(QPointF(0, 0))-matrix.map(QPointF(0, 0))
    ox, oy = round(origin.x()), round(origin.y())
    data, stride = bytes(transformed.constBits()), transformed.bytesPerLine()
    result = [[TRANSPARENT]*240 for _ in range(HEIGHT)]
    for y in range(transformed.height()):
        dy = y+oy
        if not 0 <= dy < HEIGHT:
            continue
        for x in range(transformed.width()):
            dx = x+ox
            at = y*stride+x*4
            if 0 <= dx < 240 and data[at+3] == 255:
                result[dy][dx] = PIXEL_LOOKUP[data[at:at+4]]
    return result


@lru_cache(maxsize=80)
def _rest_stretch(tick):
    """Draw the rigid hand above the head; the registered face stays underneath."""
    under, _ = _rest_arm_layers()
    arm = _raised_rest_arm(tick)
    return [[hand if hand[3] == 255 else body for body, hand in zip(body_row, hand_row)]
            for body_row, hand_row in zip(under, arm)]


def generate_frame(pose, tick, variant=0):
    if pose == "rest":
        from naiwa.multi_sprite import rest_frame
        if variant == 2:
            return _rest_stretch(tick)
        return _blink_rest(rest_frame(0), 4 if variant == 1 else BLINK_STEPS.get(tick, 0))
    if pose in {"tool","working","needs_you","done","droop","error","stale"}:
        from naiwa.whole_motion import loop
        return loop(pose,tick,variant)
    return pose_frames("idle")[0]


def generate_rise(tick, reverse=False):
    from naiwa.rigid_motion import rigid_between
    from naiwa.whole_motion import thinking
    from naiwa.multi_sprite import rest_frame
    progress = min(1, max(0, tick/22))
    position = (progress*progress*(3-2*progress))*5
    if reverse:
        position = 5-position
    keys = [rest_frame(0)]+[asset("rise", i) for i in range(1,5)]+[padded_frame(thinking())]
    i = min(4, int(position))
    return rigid_between(keys[i], keys[i+1], round((position-i)*12), 12)


@lru_cache(maxsize=1)
def _dense_manifest():
    path = ASSET_DIR/"motion-loops.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("version") != 1 or raw.get("palette") != [list(p) for p in sorted(PALETTE)]:
        raise ValueError("Invalid precompiled motion palette")
    return raw


@lru_cache(maxsize=20)
def _dense_loop(key):
    manifest = _dense_manifest()
    if not manifest or key not in manifest["loops"]:
        return None
    item = manifest["loops"][key]
    data = zlib.decompress(base64.b64decode(item["data"]))
    width, count = item["width"], item["count"]
    if width not in {160, 240} or len(data) != count*width*HEIGHT or max(data, default=0) >= len(PALETTE):
        raise ValueError("Invalid precompiled animation frame")
    return data, width, count


def prepare_animation():
    """Validate/decompress the small local pack before the desktop window opens."""
    manifest = _dense_manifest()
    if manifest:
        for key in manifest["loops"]:
            _dense_loop(key)
    from naiwa.choreography import clip
    from naiwa.whole_motion import thinking, key, KINDS, with_ground
    for kind in (*KINDS,"chin"):
        clip(kind)
    for kind in ("signals","rising"):
        for index in range(16):
            key(kind,index)
    thinking()
    for kind in KINDS:
        with_ground(animated_frame("working",0),kind)


def _dense_frame(key, index):
    loop = _dense_loop(key)
    if not loop:
        return None
    data, width, count = loop
    start = min(max(0, index), count-1)*width*HEIGHT
    frame = PixelFrame(data[start:start+width*HEIGHT], width)
    meta = _dense_manifest()["loops"][key].get("poses",[])
    if meta:
        frame.rig = meta[min(max(0,index),count-1)]
    return frame


@lru_cache(maxsize=192)
def animated_frame(pose, tick, variant=0):
    frame = _dense_frame(f"{pose}-{variant}", tick)
    return frame if frame is not None else generate_frame(pose, tick, variant)


@lru_cache(maxsize=64)
def rise_frame(tick, reverse=False):
    frame = _dense_frame("rise", 22-tick if reverse else tick)
    return frame if frame is not None else generate_rise(tick, reverse)


@lru_cache(maxsize=1024)
def _frame_identity(pose, tick, variant):
    """Identical static pixels share a render key even at different loop times."""
    loop = _dense_loop(f"{pose}-{variant}")
    if not loop:
        return tick
    data, width, count = loop
    start = min(max(0, tick), count-1)*width*HEIGHT
    return hashlib.blake2s(memoryview(data)[start:start+width*HEIGHT], digest_size=16).digest()


class MotionPlayer:
    def __init__(self):
        self.pose = None
        self.since = 0.0
        self.previous = None
        self.transition = None
        self.last = None
        self.revision = 0
        self._rise_tick = None
        self._rise_start = 0.0
        self._variant = 0
        self._variant_since = None
        self._variant_previous = None
        self._variant_from = 0
        self._last_tool_seen = None
        self._pose_blend = self._variant_blend = None
        self._physical = None
        self._physical_since = 0.0
        self._physical_target = None
        self._visual_pose = None
        self._visual_variant = 0
        self._lie_settle = None
        self._lie_settle_since = 0.0
        self._ground_kind = None

    def sample(self, requested, now, tool_variant=None):
        if self._lie_settle is not None:
            if requested == "rest":
                join_tick = int((now-self._lie_settle_since)*FPS+1e-7)
                if join_tick < TRANSITION_FRAMES:
                    self.last = self._lie_settle.sample(join_tick)
                    return self.last, (self.revision, "put-down-before-lie", join_tick)
                self._lie_settle = None
                self.transition = "lie"
                self._rise_start, self.since = 22, now
            else:
                self._lie_settle = None
                self.transition = None
                # A new task can interrupt the preparatory movement at its pixels.
                self.pose = "working"
        if (requested == "rest" and self.pose not in {None, "rest"} and
                self.last is not None and self._rise_tick is None and self._lie_settle is None):
            frame = self.last
            if len(frame[0]) == WIDTH:
                frame = padded_frame(frame)
            self._lie_settle = PoseBlend(frame, rise_frame(22))
            self._lie_settle_since = now
            self._physical = None
            self._pose_blend = self._variant_blend = None
            self._variant_since = self._variant_previous = None
            self._variant = self._variant_from = 0
            self.pose, self.transition = "rest", "put-down-before-lie"
            self.revision += 1
            self.last = frame
            return frame, (self.revision, "put-down-before-lie", 0)
        if requested == "tool":
            self._last_tool_seen = now
        if self.pose == "tool" and requested == "working" and (
                now-self.since < 1.0 or self._last_tool_seen is not None and now-self._last_tool_seen < TOOL_GAP_SECONDS):
            requested = "tool"
            tool_variant = self._variant
        destination = (requested, tool_variant if requested == "tool" and tool_variant is not None else 0)
        if self._physical is not None:
            if requested in {"working", "tool"}:
                # Finish the prop currently in our hands. Coalesce rapid tool
                # switches to the latest target instead of restarting a morph.
                self.pose, self._variant = destination
                move_tick = int((now-self._physical_since)*FPS+1e-7)
                self._physical.retarget(destination, move_tick)
                self._physical_target = self._physical.target
                if move_tick < self._physical.count:
                    self.last = self._physical.sample(move_tick)
                    return self.last, (self.revision, "physical", move_tick)
                self.last = self._physical.end
                self._ground_kind = (getattr(self.last,"rig",None) or {}).get("floor")
                self._visual_pose, self._visual_variant = self._physical_target
                self._physical = None
                self.transition = None
                self.since = now
            else:
                self._physical = None  # Waiting/error/completion remain immediate.
                self._visual_pose = None
        visual = (self._visual_pose or self.pose, self._visual_variant if self._visual_pose else self._variant)
        if (self.last is not None and visual[0] in {"working", "tool"} and
                requested in {"working", "tool"} and visual != destination and
                self._rise_tick is None and self.transition not in {"rise", "lie"}):
            self._physical = PhysicalMove(self.last, visual, destination)
            self._physical_since, self._physical_target = now, destination
            self.pose, self._variant = destination
            self.since = now if visual[0] != requested else self.since
            self._variant_since = None
            self.transition = "physical"
            self.revision += 1
            return self.last, (self.revision, "physical", 0)
        if requested != self.pose:
            old = self.pose
            if old == "tool" and requested not in {"tool","rest"}:
                from naiwa.whole_motion import KINDS
                self._ground_kind = KINDS[self._variant]
            self.previous = self.last
            self.pose, self.since = requested, now
            # One transition starts from the displayed frame. Previously a new
            # tool pose and its variant each applied a separate whole-body morph.
            self._variant = tool_variant if requested == "tool" and tool_variant is not None else 0
            self._variant_since = None
            self._pose_blend = self._variant_blend = None
            self.revision += 1
            self.transition = ("rise" if old == "rest" else "lie" if requested == "rest" and old else
                               "pose" if old else None)
            if self._rise_tick is not None:
                self.transition = "lie" if requested == "rest" else "rise"
                self._rise_start = self._rise_tick
            else:
                self._rise_start = 22 if self.transition == "lie" else 0
        elapsed = max(0, now-self.since)
        tick = int(elapsed*FPS+1e-7)
        if self.transition in {"rise", "lie"}:
            direction = -1 if self.transition == "lie" else 1
            cursor = self._rise_start+tick*direction
            if 0 <= cursor <= 22:
                self._rise_tick = cursor
                self.last = rise_frame(round(cursor))
                return self.last, (self.revision, self.transition, round(cursor))
            self._rise_tick = None
            # The lie endpoint already IS the resting loop's complete figure.
            # Reusing a cached standing PoseBlend here replayed that gesture,
            # brought the actor upright, then dropped it into rest a second time.
            self.transition = None if self.pose == "rest" else "settle"
            self._pose_blend = self._variant_blend = None
            self._variant_since = self._variant_previous = None
            self.previous = self.last
            self.since = now
            elapsed, tick = 0, 0
        rest_time = elapsed % 48
        rest_variant = 1 if 16 <= rest_time < 32 else 2 if 32 <= rest_time < 35.2 else 0
        variant = (rest_variant if self.pose == "rest" else
                   tool_variant if self.pose == "tool" and tool_variant is not None else 0)
        if variant != self._variant:
            self._variant_from = self._variant
            self._variant = variant
            self._variant_since = now
            self._variant_previous = self.last
            self._variant_blend = None
            if self.pose != "rest":
                # A tool switch can interrupt a pose switch. Start once from the
                # displayed pixels, rather than morphing that frame twice.
                self.transition = None
                self._pose_blend = None
            self.revision += 1
        animation_tick = (max(0, tick-TRANSITION_FRAMES)
                          if self.pose != "rest" and self.transition in {"pose", "settle"} else tick)
        if self.pose != "rest" and self._variant_since is not None:
            animation_tick = max(0, int((now-self._variant_since)*FPS+1e-7)-TRANSITION_FRAMES)
        index = min(animation_tick, 22) if self.pose == "done" else animation_tick % 80
        frame = animated_frame(self.pose, index, variant)
        if self.pose != "tool" and self.pose != "rest" and self._ground_kind:
            from naiwa.whole_motion import with_ground
            frame = with_ground(frame,self._ground_kind)
        if self.transition in {"pose", "settle"} and tick < TRANSITION_FRAMES and self.previous is not None:
            if len(self.previous[0]) == 240 and len(frame[0]) == 160:
                frame = padded_frame(frame)
            if len(self.previous[0]) == len(frame[0]):
                if tick == 0:
                    frame = self.previous
                else:
                    if self._pose_blend is None:
                        self._pose_blend = PoseBlend(self.previous, frame)
                    frame = self._pose_blend.sample(tick)
        if self._variant_since is not None and self._variant_previous is not None:
            change_tick = int((now-self._variant_since)*FPS+1e-7)
            if self.pose == "rest" and change_tick < 8:
                # Eyes close/open in their own texture region. Morphing the
                # whole lying silhouette here would deform the stationary face.
                if change_tick == 0:
                    frame = self._variant_previous
                elif self._variant == 1:
                    frame = _blink_rest(animated_frame("rest", index, 0), round(change_tick/2))
                elif self._variant_from == 1:
                    frame = _blink_rest(frame, 4-round(change_tick/2))
            elif self.pose != "rest" and change_tick < TRANSITION_FRAMES:
                if len(self._variant_previous[0]) == 240 and len(frame[0]) == 160:
                    frame = padded_frame(frame)
                if change_tick == 0:
                    frame = self._variant_previous
                else:
                    if self._variant_blend is None:
                        self._variant_blend = PoseBlend(self._variant_previous, frame)
                    frame = self._variant_blend.sample(change_tick)
        self.last = frame
        self._visual_pose, self._visual_variant = self.pose, variant
        pose_tick = tick if self.transition in {"pose", "settle"} and tick < TRANSITION_FRAMES else None
        variant_tick = None
        if self._variant_since is not None:
            change_tick = int((now-self._variant_since)*FPS+1e-7)
            if change_tick < (8 if self.pose == "rest" else TRANSITION_FRAMES):
                variant_tick = change_tick
        return frame, (self.revision, self.pose, _frame_identity(self.pose, index, variant),
                       variant, pose_tick, variant_tick)


class PhysicalMove:
    """Play complete poses: close, crouch, place, release, rise, think."""
    def __init__(self, source, old, target):
        from naiwa.whole_motion import KINDS
        from naiwa.choreography import clip
        self.source = source
        self.join = 0
        meta = getattr(source,"rig",None) or {}
        start = min(2,meta.get("index",0))
        self.prefix = [source]*2
        self.floor = meta.get("floor")
        if old[0] == "tool":
            self.floor = KINDS[old[1]]
            self.prefix += list(clip(self.floor))[start*2:]
        self.target = None
        self.retarget(target,0)

    def retarget(self,target,tick):
        if target == self.target or self.target is not None and tick >= len(self.prefix):
            return
        from naiwa.whole_motion import KINDS, with_ground
        from naiwa.choreography import clip
        self.target = target
        self.end = animated_frame(target[0],0,target[1])
        tail = list(reversed(clip(KINDS[target[1]]))) if target[0] == "tool" else []
        if target[0] != "tool" and self.floor:
            self.end = with_ground(self.end,self.floor)
        if len(self.source[0]) == 240:
            tail = [padded_frame(frame) for frame in tail]
            self.end = padded_frame(self.end)
        prefix = self.prefix
        if len(self.source[0]) == 240:
            prefix = [padded_frame(f) if len(f[0]) == WIDTH else f for f in prefix]
        self.frames = prefix + tail + [self.end]
        self.count = len(self.frames)
        self.sample.cache_clear()

    @lru_cache(maxsize=4)
    def sample(self,tick):
        return self.frames[min(max(0,tick),self.count-1)]


class PoseBlend:
    """Authored whole-pose paths for gestures; one opaque figure per frame."""
    def __init__(self,a,b):
        from naiwa.whole_motion import signal_path, with_ground, thinking
        from naiwa.choreography import clip
        self.a,self.b = a,b
        ma,mb = getattr(a,"rig",None) or {},getattr(b,"rig",None) or {}
        self.frames = None
        target = mb.get("pose")
        if len(a[0]) == WIDTH and len(b[0]) == WIDTH and target:
            path = []
            floor = ma.get("floor")
            if ma.get("kind") in {"book","keyboard","notebook","image"} and ma.get("index",15)<3:
                floor = ma["kind"]
                path += list(clip(floor))[ma.get("index",0)*2:]
            if target == "working":
                if ma.get("kind") == "signals":
                    oldpose = ma.get("pose","needs_you")
                    path += list(reversed(signal_path(oldpose)))
                path += [thinking()]
            elif target in {"needs_you","done","droop","error","stale"}:
                if ma.get("kind") == "signals":
                    path += list(reversed(signal_path(ma.get("pose","needs_you"))))
                path += signal_path(target)
            if path:
                self.frames = [with_ground(f,floor) if floor and f.rig.get("kind")=="signals" else f for f in path]

    def sample(self,tick):
        if tick <= 0:
            return self.a
        if tick >= TRANSITION_FRAMES:
            return self.b
        if self.frames:
            return self.frames[min(len(self.frames)-1,round(tick/TRANSITION_FRAMES*(len(self.frames)-1)))]
        from naiwa.rigid_motion import rigid_between
        return rigid_between(self.a,self.b,tick,TRANSITION_FRAMES)
