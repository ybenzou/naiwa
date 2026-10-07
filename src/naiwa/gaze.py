"""Register the approved eye texture on complete poses, on the native grid."""
from functools import lru_cache

from naiwa.pixel_frame import COLOURS, PixelFrame
from naiwa.sprite_defs import EYE, EYE_LIGHT, EYE_SHADE, EYE_DARK

GREEN = {EYE, EYE_LIGHT, EYE_SHADE, EYE_DARK}


def eye_boxes(frame):
    # Connected eye rings also work when a crouch puts both eyes left of x=53
    # or below y=80. Restrict the search to the head, excluding green props.
    top = next((y for y, row in enumerate(frame) if any(p[3] == 255 for p in row)), None)
    if top is None:
        return None
    remaining = {(x, y) for y in range(top, min(len(frame), top+65))
                 for x, p in enumerate(frame[y]) if p in GREEN}
    components = []
    while remaining:
        point = remaining.pop()
        stack, component = [point], [point]
        while stack:
            x, y = stack.pop()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                neighbour = x+dx, y+dy
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    stack.append(neighbour)
                    component.append(neighbour)
        if len(component) >= 6:
            components.append(component)
    if len(components) < 2:
        return None
    boxes = [(min(x for x, y in points), min(y for x, y in points),
              max(x for x, y in points), max(y for x, y in points))
             for points in sorted(components, key=len, reverse=True)[:2]]
    return sorted(boxes)


@lru_cache(maxsize=1)
def approved_eyes():
    from naiwa.sprite_assets import pose_frames
    reference = pose_frames("idle")[0]
    return reference, eye_boxes(reference)


def canonical_gaze(frame):
    """Keep head anatomy; replace only eye interiors with one agreed gaze."""
    boxes = eye_boxes(frame)
    if not boxes:
        return frame
    reference, reference_boxes = approved_eyes()
    data = bytearray(frame.indices)
    lookup = {colour: i for i, colour in enumerate(COLOURS)}
    for (x0, y0, x1, y1), (rx0, ry0, rx1, ry1) in zip(boxes, reference_boxes):
        for y in range(y0, y1+1):
            sy = ry0+round((y-y0)*(ry1-ry0)/max(1, y1-y0))
            for x in range(x0, x1+1):
                sx = rx0+round((x-x0)*(rx1-rx0)/max(1, x1-x0))
                data[y*frame.width+x] = lookup[reference[sy][sx]]
    result = PixelFrame(bytes(data), frame.width)
    result.rig = frame.rig
    return result
