"""Screen geometry helpers that do not need a live window."""

from __future__ import annotations


def integer_scale(device_pixel_ratio: float, base: int = 1) -> int:
    """Whole physical pixels per sprite pixel, including fractional screen DPI."""
    import math
    if not math.isfinite(device_pixel_ratio) or device_pixel_ratio <= 0:
        device_pixel_ratio = 1.0
    return min(12 if base > 1 else 4, max(1, int(device_pixel_ratio * base + 0.5)))


def intersects(x: int, y: int, w: int, h: int, screen: tuple[int, int, int, int]) -> bool:
    sx, sy, sw, sh = screen
    return not (x + w <= sx or sx + sw <= x or y + h <= sy or sy + sh <= y)


def visible_position(
    x: int,
    y: int,
    w: int,
    h: int,
    screens: list[tuple[int, int, int, int]],
) -> tuple[int, int]:
    if not screens:
        return 48, 48
    def overlap(screen):
        sx, sy, sw, sh = screen
        return max(0, min(x+w, sx+sw)-max(x, sx)) * max(0, min(y+h, sy+sh)-max(y, sy))
    target = max(screens, key=overlap)
    if not overlap(target):
        target = min(screens, key=lambda s: (x+w/2-s[0]-s[2]/2)**2 + (y+h/2-s[1]-s[3]/2)**2)
        sx, sy, sw, sh = target
        x, y = sx+sw-w-24, sy+sh-h-36
    sx, sy, sw, sh = target
    return min(max(sx, x), sx+max(0, sw-w)), min(max(sy, y), sy+max(0, sh-h))
