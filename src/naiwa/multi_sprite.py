"""Compose generated limbs behind the approved character, on the native pixel grid."""
from dataclasses import dataclass
from functools import lru_cache
from collections.abc import Mapping
import math

from naiwa.sprite_assets import ASSET_DIR, PIXEL_LOOKUP, _load, pose_frames, render_accents
from naiwa.sprite_defs import *

WING = 80
MAX_HANDS_PER_SIDE = 8
REST_WIDTH = 240
REST_DURATIONS = (1100, 700, 180, 1000)
ARM_ANCHOR = (14, 77)

# Small UI glyphs, deliberately independent of character illustration.
GLYPHS = {
    "0": (7, 5, 5, 5, 7), "1": (2, 6, 2, 2, 7), "2": (7, 1, 7, 4, 7),
    "3": (7, 1, 7, 1, 7), "4": (5, 5, 7, 1, 1), "5": (7, 4, 7, 1, 7),
    "6": (7, 4, 7, 5, 7), "7": (7, 1, 2, 2, 2), "8": (7, 5, 7, 5, 7),
    "9": (7, 5, 7, 1, 7), "C": (7, 4, 4, 4, 7), "X": (5, 5, 2, 5, 5),
    "D": (6, 5, 5, 5, 6), "?": (7, 1, 2, 0, 2), "!": (2, 2, 2, 0, 2),
    "+": (0, 2, 7, 2, 0), "v": (0, 0, 5, 5, 2), "~": (0, 3, 6, 0, 0),
}


def glyph(canvas, text, x, y, colour):
    for char in text:
        for dy, bits in enumerate(GLYPHS.get(char, GLYPHS["?"])):
            for dx in range(3):
                if bits & (4 >> dx) and 0 <= y+dy < len(canvas) and 0 <= x+dx < len(canvas[0]):
                    canvas[y+dy][x+dx] = colour
        x += 4


@lru_cache(maxsize=4)
def rest_frame(index):
    return _load(ASSET_DIR/f"rest-{index%4}.png", REST_WIDTH, HEIGHT)


@lru_cache(maxsize=4)
def arm_frame(gesture):
    return _load(ASSET_DIR/f"arm-{gesture}.png", 80, 88)


@lru_cache(maxsize=256)
def transformed_arm(gesture, angle, left, extension=12):
    """Nearest-neighbour affine placement; no synthesized limb pixels or blur."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QImage, QTransform
    image = _arm_image(gesture)
    factor = 1.1*extension/12
    if extension == 0:
        return None, (0, 0)
    transform = QTransform().rotate(angle+51).scale(factor, factor)
    matrix = QImage.trueMatrix(transform, image.width(), image.height())
    anchor = matrix.map(QPointF(*ARM_ANCHOR))
    rotated = image.transformed(transform, Qt.TransformationMode.FastTransformation).convertToFormat(QImage.Format.Format_RGBA8888)
    ax, ay = round(anchor.x()), round(anchor.y())
    if left:
        rotated = rotated.transformed(QTransform().scale(-1, 1), Qt.TransformationMode.FastTransformation)
        ax = rotated.width()-1-ax
    return rotated, (-ax, -ay)


@lru_cache(maxsize=256)
def rotated_arm(gesture, angle, left, extension=12):
    rotated, (ox, oy) = transformed_arm(gesture, angle, left, extension)
    if rotated is None:
        return [], (0, 0)
    data, stride = bytes(rotated.constBits()), rotated.bytesPerLine()
    rotation = math.radians(angle+51)
    cosine, sine, scale = math.cos(rotation), math.sin(rotation), 1.1*extension/12
    pixels = []
    for y in range(rotated.height()):
        for x in range(rotated.width()):
            at = y*stride+x*4
            if data[at+3] == 255:
                pixels.append((x+ox, y+oy, PIXEL_LOOKUP[data[at:at+4]]))
    px, py = 48, -59
    hand = (round((px*cosine-py*sine)*scale), round((px*sine+py*cosine)*scale))
    return pixels, (-hand[0] if left else hand[0], hand[1])


@lru_cache(maxsize=4)
def _arm_image(gesture):
    from PySide6.QtGui import QImage
    frame = arm_frame(gesture)
    data = b"".join(bytes(p) for row in frame for p in row)
    return QImage(data, 80, 88, 80*4, QImage.Format.Format_RGBA8888).copy()


@lru_cache(maxsize=256)
def placed_arm(gesture, angle, left, extension, x, y):
    pixels, _ = rotated_arm(gesture, angle, left, extension)
    # Reuse coordinate tuples. Allocating thousands per arm per frame triggered
    # garbage-collector pauses even when the underlying sprite was cached.
    width = WIDTH+WING*2
    return {(y+dy)*width+x+dx: colour for dx, dy, colour in pixels
            if 0 <= x+dx < width and 0 <= y+dy < HEIGHT}


@lru_cache(maxsize=64)
def tag_pixels(x, y):
    return tuple((y+dy)*(WIDTH+WING*2)+x+dx for dy in range(12) for dx in range(72))


class PixelOwners(Mapping):
    """Public (x,y) hit keys backed by untracked integer pixel offsets."""
    def __init__(self, offsets, width):
        self.offsets, self.width = offsets, width

    def __len__(self):
        return len(self.offsets)

    def __iter__(self):
        return ((at % self.width, at // self.width) for at in self.offsets)

    def __getitem__(self, point):
        x, y = point
        if not 0 <= x < self.width or not 0 <= y < HEIGHT:
            raise KeyError(point)
        return self.offsets[y*self.width+x]

    def values(self):
        return self.offsets.values()


@dataclass
class Composition:
    canvas: list
    anchor: tuple[int, int]
    accents: list
    owners: dict[tuple[int, int], str]
    overflow: dict[str, int]
    labels: tuple = ()
    arm_draws: tuple = ()
    body: list | None = None
    body_offset: int = 0


def spread_angles(count):
    """Spread hands beside the torso, keeping lower arms clear of the hip."""
    if count <= 0:
        return []
    if count == 1:
        return [8]
    if count == 2:
        return [-14, 30]
    low, high = -28, 36
    return [round(low+(high-low)*i/(count-1)) for i in range(count)]


def composition_key(agents):
    return tuple((a.source, a.session_id, a.number, a.pose, a.wait_kind, a.workspace_name) for a in agents)


def shoulder_anchor(body, left, offset):
    """Attach to the torso centre, independent of foreground palms and props."""
    meta = getattr(body, "rig", None)
    if meta and "body_anchor" in meta:
        x, y = meta["body_anchor"]
        # The three-quarter torso is wider on the right. Root overlap stays
        # inside the flank, rather than burying most of the arm in the belly.
        return offset+x+(-34 if left else 42), y
    # Legacy lying/rising artwork has no authored landmark. Use its centre
    # column, never an outer alpha boundary that follows an extended palm.
    centre = len(body[0])//2
    for y in sorted(range(98, 160), key=lambda y: abs(y-113)):
        spans, start = [], None
        for x, colour in enumerate(body[y]+[TRANSPARENT]):
            if colour[3] == 255 and start is None:
                start = x
            elif colour[3] != 255 and start is not None:
                spans.append((start, x-1))
                start = None
        if not spans:
            continue
        central = [span for span in spans if span[0] <= centre <= span[1]]
        if not central:
            continue
        x0, x1 = central[0]
        if x1-x0 < 35:
            continue
        x = centre+(-34 if left else 42)
        return offset+max(x0+12, min(x1-12, x)), y
    raise ValueError("Character has no opaque torso for an arm attachment")


@lru_cache(maxsize=32)
def compose(pose, index, agents=(), tick=0):
    return _compose(pose, index, agents, tick)


def compose_motion(pose, body, agents=(), tick=0, arm_states=()):
    return _compose(pose, 0, agents, tick, body, arm_states)


def _compose(pose, index, agents, tick, body=None, arm_states=()):
    if pose == "rest":
        frame = body if body is not None else rest_frame(index)
        return Composition(frame, (len(frame[0])//2, FOOT_Y),
                           [[TRANSPARENT]*REST_WIDTH for _ in range(HEIGHT)], {}, {})
    if not agents:
        frame = body if body is not None else pose_frames(pose)[index]
        return Composition(frame, (len(frame[0])//2, FOOT_Y), render_accents("working" if pose == "tool" else "droop" if pose in {"error", "stale"} else pose, 0), {}, {})
    width = WIDTH+WING*2
    body = body if body is not None else pose_frames(pose)[index]
    body_offset = WING+(WIDTH-len(body[0]))//2
    canvas = [[TRANSPARENT]*width for _ in range(HEIGHT)]
    accents = [[TRANSPARENT]*width for _ in range(HEIGHT)]
    owners, overflow, labels, arm_draws = {}, {}, [], []
    states = {(source, session): (shown, extent) for source, session, shown, extent in arm_states}
    for source, left in (("cursor", True), ("codex", False), ("demo", True)):
        rows = [a for a in agents if a[0] == source]
        # When the fan is full, waiting/error/completion must remain visible.
        visible = sorted(rows, key=lambda a: (a[3] not in {"needs_you", "error", "done", "stopped"}, a[2]))[:MAX_HANDS_PER_SIDE]
        visible.sort(key=lambda a: a[2])
        overflow[source] = max(0, len(rows)-len(visible))
        # Space whoever is actually visible. Numbered slots put both current
        # left hands on the steep upward angles, which the head covers.
        slots = spread_angles(len(visible))
        if not visible:
            continue
        shoulder_x, shoulder_y = shoulder_anchor(body, left, body_offset)
        pending_labels = []
        lane_x = 3 if left else WING+WIDTH+4
        lane_blocked = set()
        for row, angle in zip(visible, slots):
            _, session, number, phase, wait_kind, workspace_name = row
            gesture = "waiting" if phase in {"needs_you", "stopped"} else "done" if phase == "done" else "droop" if phase in {"stale", "error"} else "working"
            gesture, extension = states.get((source, session), (gesture, 12))
            moving_phase = ("needs_you" if gesture == "waiting" else "done" if gesture == "done"
                            else "error" if gesture == "droop" else "tool" if phase == "tool" else "working")
            base_angle = angle
            wave = math.sin((tick/80+number*.13)*math.tau)
            if moving_phase in {"working", "tool", "needs_you"}:
                angle += round(wave*(4 if moving_phase == "tool" else 3 if moving_phase == "needs_you" else 2)*2)/2
            elif moving_phase == "done":
                angle -= 8+round(wave*2*2)/2
            arm, hand = rotated_arm(gesture, angle, left, extension)
            arm_draws.append((gesture, angle, left, extension, shoulder_x, shoulder_y))
            key = f"{source}:{session}"
            for at, colour in placed_arm(gesture, angle, left, extension, shoulder_x, shoulder_y).items():
                y, x = divmod(at, width)
                canvas[y][x] = colour
                owners[at] = key
            label_arm, label_hand = rotated_arm("working", base_angle, left)
            hx, hy = shoulder_x+label_hand[0], shoulder_y+label_hand[1]
            # Leave a clear caption band above the arm's outer pixels. Labels
            # use a settled gesture so the idle wave cannot shake the text.
            outer_y = [shoulder_y+dy for dx, dy, _ in label_arm
                       if lane_x <= shoulder_x+dx < lane_x+72]
            for y in outer_y:
                lane_blocked.update(range(y-4, y+5))
            label = f"{ {'cursor': 'C', 'codex': 'X', 'demo': 'D'}[source]}{number}"
            symbol = "?" if phase == "needs_you" and wait_kind == "input" else "!" if phase in {"needs_you", "error"} else "v" if phase in {"done", "stopped"} else "~" if phase == "stale" else "+" if phase == "tool" else ""
            colour = PINK if phase in {"needs_you", "error"} else HAND_LIGHT if phase == "stale" else CYAN
            pending_labels.append(((min(outer_y)-16) if outer_y else hy-28,
                                   key, workspace_name, label+symbol, colour))
        # Name tags remain wholly outside the approved body/face. Align each side
        # into a narrow lane and resolve vertical collisions before drawing.
        placed, previous_y, used = [], -14, set()
        for target, key, name, suffix, colour in sorted(pending_labels):
            candidates = sorted(range(2, 185), key=lambda y: (abs(y-target), y))
            by = next((y for y in candidates if not any(
                row in lane_blocked or row in used for row in range(y-2, y+14))), None)
            if by is None:
                by = max(2, target, previous_y+15)
            placed.append((by, key, name, suffix, colour))
            used.update(range(by-2, by+14))
            previous_y = by
        shift = max(0, max(by for by, *_ in placed)+13-197)
        for by, key, name, suffix, colour in placed:
            by -= shift
            bx, tag_width, tag_height = (3 if left else WING+WIDTH+4), 72, 12
            for at in tag_pixels(bx, by):
                y, x = divmod(at, width)
                canvas[y][x] = OUTLINE
                owners[at] = key
            # Qt renders Unicode UI names on this geometry using a bitmap font
            # strategy; the illustration compositor never depends on a GUI app.
            labels.append((key, name, suffix, (bx, by, tag_width, tag_height), colour))
            if not name:
                glyph(canvas, suffix, bx+3, by+3, colour)
    # Copy registered opaque runs, avoiding a dict pop on every torso pixel.
    body_pixels = body.materialize() if hasattr(body, "materialize") else body
    for y, start, end in opaque_runs(body):
        canvas[y][start+body_offset:end+body_offset] = body_pixels[y][start:end]
    body_width = len(body[0])
    if hasattr(body, "alpha"):
        alpha = body.alpha
        owners = {at: key for at, key in owners.items()
                  if not 0 <= at % width-body_offset < body_width or not alpha[(at//width)*body_width+at%width-body_offset]}
    else:
        owners = {at: key for at, key in owners.items()
                  if not 0 <= at % width-body_offset < body_width or not body[at//width][at%width-body_offset][3]}
    marks = render_accents("working" if pose == "tool" else "droop" if pose in {"error", "stale"} else pose, 0)
    for y, row in enumerate(marks):
        accents[y][WING:WING+WIDTH] = row
    return Composition(canvas, (WING+ANCHOR[0], FOOT_Y), accents, PixelOwners(owners, width), overflow, tuple(labels),
                       tuple(arm_draws), body, body_offset)


_RUN_CACHE = {}


def opaque_runs(body):
    if hasattr(body, "runs"):
        return body.runs
    identity = id(body)
    cached = _RUN_CACHE.get(identity)
    if cached is not None and cached[0] is body:
        return cached[1]
    runs = []
    for y, row in enumerate(body):
        start = None
        for x, p in enumerate(row+[TRANSPARENT]):
            if p[3] and start is None:
                start = x
            elif not p[3] and start is not None:
                runs.append((y, start, x))
                start = None
    if len(_RUN_CACHE) >= 256:
        del _RUN_CACHE[next(iter(_RUN_CACHE))]
    _RUN_CACHE[identity] = (body, runs)
    return runs
