"""Physical prop clips, compiled from the generated key atlas on the pixel grid."""
from functools import lru_cache
import base64
import json
import zlib

from naiwa.sprite_assets import ASSET_DIR, _load
from naiwa.sprite_defs import *
from naiwa.pixel_frame import PixelFrame

KINDS = ("book", "keyboard", "notebook", "image")


@lru_cache(maxsize=24)
def key(kind, index):
    return _load(ASSET_DIR/f"bridge-{kind}-{index}.png", WIDTH, HEIGHT)


def generated_clip(kind):
    """Whole illustrated actor crouches, places the object and stands up."""
    from naiwa.whole_motion import put_down
    return put_down(kind)


def generated_chin():
    from naiwa.whole_motion import key, thinking
    return [key("book",i) if i < 15 else thinking()
            for i in range(8,16) for _ in range(2)]


@lru_cache(maxsize=1)
def _pack():
    raw = json.loads((ASSET_DIR/"physical-clips.json").read_text(encoding="utf-8"))
    if raw.get("version") != 1 or raw["palette"] != [list(p) for p in sorted(PALETTE)]:
        raise ValueError("Invalid physical clip palette")
    palette = sorted(PALETTE)
    clips = {}
    for kind, item in raw["clips"].items():
        data = zlib.decompress(base64.b64decode(item["data"]))
        if len(data) != WIDTH*HEIGHT*item["count"] or max(data) >= len(palette):
            raise ValueError("Invalid physical clip frame")
        frames = [PixelFrame(data[at:at+WIDTH*HEIGHT], WIDTH)
                  for at in range(0, len(data), WIDTH*HEIGHT)]
        for frame, meta in zip(frames,item.get("poses",[])):
            frame.rig = meta
        clips[kind] = tuple(frames)
    return clips


def clip(kind):
    return _pack()[kind]
