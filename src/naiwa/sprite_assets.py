"""Load the newly illustrated high-density Naiwa sprites on a shared pixel grid.

Source identity images are references only. Runtime PNGs are redrawn artwork,
compiled to a fixed palette and foot anchor; they need no generation service.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import struct
import zlib

from naiwa.sprite_defs import *

ASSET_DIR = Path(__file__).with_name("assets")
PIXEL_LOOKUP = {bytes(p): p for p in PALETTE}


def _blank(width=WIDTH, height=HEIGHT):
    return [[TRANSPARENT for _ in range(width)] for _ in range(height)]


def _load(path: Path, width=WIDTH, height=HEIGHT):
    from PySide6.QtGui import QImage
    image = QImage(str(path))
    if image.isNull() or (image.width(), image.height()) != (width, height):
        raise ValueError(f"Invalid Naiwa sprite: {path}")
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    data, stride = bytes(image.constBits()), image.bytesPerLine()
    try:
        frame = [[PIXEL_LOOKUP[data[y*stride+x*4:y*stride+x*4+4]] for x in range(width)] for y in range(height)]
    except KeyError:
        raise ValueError(f"Sprite exceeds the fixed palette: {path}") from None
    return frame


@lru_cache(maxsize=5)
def pose_frames(pose: str):
    if pose not in POSES:
        raise ValueError(f"Unknown pose: {pose}")
    return [_load(ASSET_DIR/f"{pose}-{index}.png") for index in range(len(DURATIONS[pose]))]


def render_frame(pose: str, frame: int):
    frames = pose_frames(pose)
    return frames[frame % len(frames)]


def frame_at(pose: str, elapsed_ms: int) -> int:
    durations = DURATIONS[pose]
    cursor = max(0, elapsed_ms) % sum(durations)
    for index, duration in enumerate(durations):
        if cursor < duration:
            return index
        cursor -= duration
    return 0


@lru_cache(maxsize=4)
def render_gem(frame: int):
    canvas = _blank()
    cx, cy = WIDTH-12, round(HEIGHT*.58)-(frame % 2)*3
    for dy in range(-6, 7):
        span = 6-abs(dy)
        for dx in range(-span, span+1):
            canvas[cy+dy][cx+dx] = OUTLINE if abs(dx) == span else CYAN if frame % 2 == 0 else PINK
    return canvas


@lru_cache(maxsize=20)
def render_accents(pose: str, frame: int):
    """Two tiny code-native Memphis shapes on the input-transparent layer."""
    canvas = _blank()
    if pose == "droop":
        return canvas
    top = 64-(frame % 2 if pose == "done" else 0)
    for dy in range(6):
        for x in range(3+dy//2, 10-dy//2):
            canvas[top+dy][x] = CYAN
    for y in range(top+12, top+15):
        for x in range(6, 9):
            canvas[y][x] = PINK
    return canvas


def body_bottom(canvas) -> int:
    return max((y for y, row in enumerate(canvas) if any(p[3] == 255 for p in row)), default=-1)


def is_body_pixel(pixel) -> bool:
    return pixel[3] == 255


def hit(canvas, x: int, y: int) -> bool:
    return 0 <= y < len(canvas) and 0 <= x < len(canvas[y]) and is_body_pixel(canvas[y][x])


def render_placeholder():
    # Explicit debugging mode only; the pet itself always loads the illustrated PNGs.
    canvas = _blank()
    for y in range(24, FOOT_Y+1):
        radius = round(17+(y-24)*.21)
        centre = ANCHOR[0]+round((y-24)*.035)
        for x in range(centre-radius, centre+radius+1):
            canvas[y][x] = OUTLINE if x in (centre-radius, centre+radius) or y in (24, FOOT_Y) else BODY
    for y in range(FOOT_Y+4, FOOT_Y+9):
        for x in range(ANCHOR[0]-44, ANCHOR[0]+45):
            canvas[y][x] = SHADOW
    return canvas


def save_png_canvas(path: Path, canvas, scale: int = 1) -> None:
    if type(scale) is not int or scale < 1:
        raise ValueError("PNG scale must be a positive integer")
    height, width = len(canvas)*scale, len(canvas[0])*scale
    raw = bytearray()
    for row in canvas:
        for _ in range(scale):
            raw.append(0)
            for pixel in row:
                raw.extend(bytes(pixel)*scale)
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag+data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def save_png(path: Path, canvas, scale: int = 1) -> None:
    save_png_canvas(path, canvas, scale)


def export_contact_sheet(path: Path) -> None:
    sheet = _blank(WIDTH*4, HEIGHT*len(POSES))
    for row, pose in enumerate(POSES):
        for col, frame in enumerate(pose_frames(pose)):
            accents = render_accents(pose, col)
            for y, pixels in enumerate(frame):
                sheet[row*HEIGHT+y][col*WIDTH:(col+1)*WIDTH] = [
                    accent if pixel == TRANSPARENT and accent != TRANSPARENT else pixel
                    for pixel, accent in zip(pixels, accents[y])
                ]
    save_png_canvas(path, sheet, 2)


def export_assets(directory: Path) -> None:
    from naiwa.multi_sprite import rest_frame, arm_frame, REST_WIDTH, REST_DURATIONS, ARM_ANCHOR
    directory.mkdir(parents=True, exist_ok=True)
    for pose in POSES:
        for index, frame in enumerate(pose_frames(pose)):
            save_png(directory/f"{pose}-{index}.png", frame, 1)
    export_contact_sheet(directory/"contact.png")
    for index in range(4):
        save_png(directory/f"rest-{index}.png", rest_frame(index))
    for gesture in ("working", "waiting", "done", "droop"):
        save_png(directory/f"arm-{gesture}.png", arm_frame(gesture))
    from naiwa.motion import asset
    for kind in ("rise", "tool", "stretch", "read", "type", "write"):
        for index in range(6):
            save_png(directory/f"motion-{kind}-{index}.png", asset(kind, index))
    from naiwa.choreography import key
    for kind in ("book", "keyboard", "notebook", "chin"):
        for index in range(6):
            save_png(directory/f"bridge-{kind}-{index}.png", key(kind, index))
    from naiwa.whole_motion import key as whole_key
    for kind in ("book","keyboard","notebook","image","signals","rising"):
        for index in range(16):
            save_png(directory/f"whole-{kind}-{index}.png",whole_key(kind,index))
    import shutil
    if directory.resolve() != ASSET_DIR.resolve():
        for name in ("motion-loops.json", "physical-clips.json", "whole-poses.json", "bridges.json"):
            pack = ASSET_DIR/name
            if pack.exists():
                shutil.copyfile(pack, directory/name)
    (directory/"manifest.json").write_text(json.dumps({
        "character": "奶娃（奶蛙二创形象）", "width": WIDTH, "height": HEIGHT,
        "anchor": ANCHOR, "durations_ms": DURATIONS, "palette": sorted(PALETTE),
        "sprite_version": 3, "art": "imagegen redraw, original thinking face proportions",
        "composition_version": 2,
        "motion_version": 4, "animation_fps": 25, "motion_pack": "motion-loops.json",
        "physical_pack": "physical-clips.json", "physical_frames": 208,
        "motion": {"rise": 6, "tool": 6, "stretch": 6, "read": 6, "type": 6, "write": 6,
                   "tool_variants": ["reading", "typing", "writing", "inspecting"],
                   "idle_variants": ["blinking", "dozing", "stretching"],
                   "transition": "complete illustrated poses; crouch, place, release, rise and think"},
        "rest": {"width": REST_WIDTH, "height": HEIGHT, "anchor": [REST_WIDTH//2, FOOT_Y],
                 "durations_ms": REST_DURATIONS},
        "arms": {"width": 80, "height": 88, "anchor": ARM_ANCHOR,
                 "gestures": ["working", "waiting", "done", "droop"], "extend_retract_ms": 240},
    }, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
