"""Complete illustrated poses: shoulders, elbows and hands are never separated."""
from functools import lru_cache
import math
import json

from naiwa.sprite_assets import ASSET_DIR, _load
from naiwa.sprite_defs import *
from naiwa.rigid_motion import composite
from naiwa.pixel_frame import PixelFrame

KINDS = ("book", "keyboard", "notebook", "image")


@lru_cache(maxsize=1)
def pose_manifest():
    return json.loads((ASSET_DIR/"whole-poses.json").read_text(encoding="utf-8"))["frames"]


@lru_cache(maxsize=128)
def key(kind, index):
    from naiwa.rigid_motion import pixels, bitmap
    frame = pixels(bitmap(_load(ASSET_DIR/f"whole-{kind}-{index}.png", WIDTH, HEIGHT)))
    from naiwa.gaze import canonical_gaze
    frame = canonical_gaze(frame)
    frame.rig = {"kind": kind, "index": index, "dx": 0,
                 "body_anchor": pose_manifest()[kind]["body_anchors"][index]}
    return frame


def stamp(frame, **meta):
    if not isinstance(frame, PixelFrame):
        from naiwa.rigid_motion import pixels, bitmap
        frame = pixels(bitmap(frame))
    frame.rig = meta
    return frame


@lru_cache(maxsize=4)
def ground_prop(kind):
    # Reviewed prop bounds beside the left foot. Paint this under the complete
    # actor, preserving foreground feet at the object's original contact edge.
    final = key(kind, 15)
    right,top,bottom = {"book":(39,160,210),"keyboard":(41,157,210),
                       "notebook":(41,156,210),"image":(33,171,202)}[kind]
    return [[p if x < right and top <= y <= bottom else TRANSPARENT
             for x, p in enumerate(row)] for y, row in enumerate(final)]


@lru_cache(maxsize=1)
def thinking():
    from naiwa.rigid_motion import pixels,bitmap
    return stamp(pixels(bitmap(key("rising",15))),
                 **dict(key("rising",15).rig, pose="working"))


def with_ground(frame, kind):
    if not kind:
        return frame
    result = _grounded(frame.indices, frame.width, kind)
    result.rig = dict(frame.rig or {}, floor=kind)
    return result


@lru_cache(maxsize=192)
def _grounded(indices, width, kind):
    frame = PixelFrame(indices, width)
    offset = (width-WIDTH)//2
    empty = [[TRANSPARENT]*width for _ in frame]
    return composite(empty, [(ground_prop(kind),dict(dx=offset)), (frame,{})])


def loop(pose, tick, variant=0):
    if pose == "tool":
        kind = KINDS[variant]
        # Preserve one complete activity figure while it moves as a whole.
        index = 0
        base = key(kind,index)
    elif pose == "working":
        kind, index, base = "rising",15,thinking()
    elif pose == "needs_you":
        kind, index = "signals",3
        base = key(kind,index)
    elif pose in {"droop","error","stale"}:
        kind,index = "signals",15
        base = key(kind,index)
    else:
        kind,index = "signals",10
        base = key(kind,index)
    # Shift the COMPLETE connected actor by at most one native pixel; no
    # row-wise warp, limb scaling, secondary torso or transparent crossfade.
    dx = round(math.sin(tick/80*math.tau)) if pose != "done" else 0
    lift = round(9*math.sin(min(1,tick/22)*math.pi)**2) if pose == "done" else 0
    ground = [[SHADOW if p == SHADOW else TRANSPARENT for p in row] for row in base]
    actor = [[TRANSPARENT if p == SHADOW else p for p in row] for row in base]
    ax, ay = base.rig["body_anchor"]
    return stamp(composite(ground,[(actor,dict(dx=dx,dy=-lift))]),
                 kind=kind,index=index,dx=dx,pose=pose,body_anchor=[ax+dx,ay-lift])


def put_down(kind, start=0):
    poses = [key(kind,i) for i in range(start,10)]
    # The artist's rising sheet has a few poses out of order. Sort the complete
    # low-hand figures by height before the final chin gesture: no limb edits.
    # First two poses crouch lower than the placed-object endpoint and would
    # make the actor squat a second time. Continue at the matching height.
    # Atlas 8 has a different lean despite a similar height; using it between
    # 5 and 6 would lurch sideways. Explicitly play the connected rising poses.
    first = 4 if kind == "image" else 2
    rising = [key("rising",i) for i in (first,4,3,5,6,6,7,9,10,11)]
    poses += [with_ground(f,kind) for f in rising]
    poses += [with_ground(key("rising",i),kind) for i in range(12,16)]
    frames = [f for f in poses for _ in range(2)]
    # Share one complete thinking silhouette, while the object stays where it
    # was placed. The last drawn tool pose has already raised the hand to chin.
    frames[-2:] = [with_ground(thinking(),kind)]*2
    return frames


def signal_path(pose):
    indices = (range(4) if pose == "needs_you" else
               (8,9,10,10) if pose == "done" else range(12,16))
    frames = [key("signals",i) for i in indices]
    frames[0] = thinking()
    return frames
