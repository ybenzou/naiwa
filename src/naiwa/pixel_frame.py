"""Immutable indexed pixels with the existing row/colour sequence interface.

Keeping 160 colour references per row made cyclic GC scan millions of pointers.
Bytes are untracked; Qt can consume them without decoding every pixel again.
"""
from collections.abc import Sequence
import re

from naiwa.sprite_defs import PALETTE

COLOURS = tuple(sorted(PALETTE))
ALPHA = bytes([bool(p[3]) for p in COLOURS]+[0]*(256-len(COLOURS)))


class PixelRow(Sequence):
    __slots__ = ("indices",)

    def __init__(self, data):
        self.indices = data

    def __len__(self):
        return len(self.indices)

    def __iter__(self):
        return map(COLOURS.__getitem__, self.indices)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return list(map(COLOURS.__getitem__, self.indices[index]))
        return COLOURS[self.indices[index]]

    def __add__(self, other):
        return list(self)+list(other)

    def __radd__(self, other):
        return list(other)+list(self)

    def __eq__(self, other):
        if isinstance(other, PixelRow):
            return self.indices == other.indices
        return list(self) == other


class PixelFrame(Sequence):
    __slots__ = ("indices", "width", "rows", "runs", "alpha", "_pixels", "rig")

    def __init__(self, data, width):
        if len(data) % width:
            raise ValueError("Incomplete indexed pixel rows")
        self.indices, self.width = data, width
        self.alpha = data.translate(ALPHA)
        self._pixels = None
        self.rig = None
        self.rows = tuple(PixelRow(data[y:y+width]) for y in range(0, len(data), width))
        self.runs = tuple((y, match.start(), match.end()) for y, row in enumerate(self.rows)
                          for match in re.finditer(b"[\x01]+", row.indices.translate(ALPHA)))

    def __len__(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [r[:] for r in self.rows[index]]
        return self.rows[index]

    def __eq__(self, other):
        if self is other:
            return True
        if isinstance(other, PixelFrame):
            return self.width == other.width and self.indices == other.indices
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def padded(self, border):
        transparent = bytes([COLOURS.index((0, 0, 0, 0))])*border
        frame=PixelFrame(b"".join(transparent+r.indices+transparent for r in self.rows), self.width+border*2)
        frame.rig=dict(self.rig) if self.rig else None
        if frame.rig and "body_anchor" in frame.rig:
            x, y = frame.rig["body_anchor"]
            frame.rig["body_anchor"] = [x+border, y]
        return frame

    def materialize(self):
        if self._pixels is None:
            # Tuple rows are immutable and become untracked after a young-gen
            # collection; cached rows don't make every full GC rescan pixels.
            self._pixels = tuple(tuple(row) for row in self.rows)
        return self._pixels
