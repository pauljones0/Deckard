"""The wallpaper-slideshow timing and index model.

A deck background can hold an ordered list of still images that rotate on an
interval. This class owns which image shows now, when the next one is due, and
the order they follow. It owns no image data, no file I/O and no toolkit. The
render path builds the image for current_path() and swaps it; this class only
says which path and when.

The model takes the current time from the caller, so it has no clock and no
sleep. The media-player tick passes a monotonic reading each pass, and a test
passes a value it controls. due() answers whether the interval has elapsed,
advance() moves to the next image and re-seeds the timebase, and maybe_advance()
pairs the two.

Cancellation is not a method here. The render path drops its reference to the
model when the page changes or the deck tears down, and a dropped model never
ticks again.
"""
from __future__ import annotations

import random
from collections.abc import Sequence

#: Walk the list in the order the user built it.
IN_ORDER = "in-order"
#: Walk a shuffled permutation, reshuffled each time it wraps.
SHUFFLE = "shuffle"


class Slideshow:
    """An ordered rotation over a list of image paths.

    A list of one image, or of none, never advances: the interval means
    nothing without a second image to move to. A zero or negative interval
    also never advances, so a misconfigured interval holds the first image
    rather than flickering.
    """

    def __init__(self, paths: Sequence[str], interval: float, order: str = IN_ORDER,
                 rng: "random.Random | None" = None) -> None:
        # Keep only real, non-empty path strings. A None or an empty entry in
        # the stored list must not become a frame the render path tries to
        # load.
        self.paths: list[str] = [p for p in paths if isinstance(p, str) and p]
        self.interval: float = max(0.0, float(interval))
        self.order: str = order if order in (IN_ORDER, SHUFFLE) else IN_ORDER
        # A seeded generator makes a shuffle reproducible in a test. The
        # default draws from the process generator.
        self._rng: random.Random = rng if rng is not None else random.Random()
        # A permutation of indices into self.paths. In-order is the identity;
        # shuffle is a random permutation, rebuilt on each wrap.
        self._sequence: list[int] = self._build_sequence()
        # Position within self._sequence, not within self.paths.
        self._pos: int = 0
        # Monotonic time of the last advance or the seed, or None before the
        # timebase is set. due() reads None as "not yet armed".
        self._last_advance: float | None = None
        # The page this rotation was loaded for. An opaque handle the render
        # layer sets and reads by identity; the model never looks inside it. It
        # lets the media tick refuse to advance a rotation whose page is no
        # longer active, the way the background video guards its own repaint.
        self.page: object | None = None
        # Per-image viewports, keyed by path. The render layer fills and
        # reads it; the model only carries it beside the rotation so a frame
        # advance can render the incoming image through its own view. A path
        # with no entry renders through the default view.
        self.views: dict[str, tuple[float, float, float]] = {}

    def _build_sequence(self) -> list[int]:
        indices = list(range(len(self.paths)))
        if self.order == SHUFFLE and len(indices) > 1:
            self._rng.shuffle(indices)
        return indices

    def __len__(self) -> int:
        return len(self.paths)

    @property
    def index(self) -> int:
        """The index into self.paths of the image showing now."""
        if not self._sequence:
            return 0
        return self._sequence[self._pos]

    def current_path(self) -> "str | None":
        """The path of the image showing now, or None for an empty list."""
        if not self.paths:
            return None
        return self.paths[self.index]

    def seed(self, now: float) -> None:
        """Start the interval clock from now. The render path calls this once
        it has installed the first frame, so the first swap lands one interval
        later and not at once."""
        self._last_advance = now

    def due(self, now: float) -> bool:
        """Whether the interval has elapsed since the last advance or seed.

        False for a list that cannot rotate (fewer than two images), for a
        non-positive interval, and before the timebase is seeded.
        """
        if self.interval <= 0 or len(self.paths) <= 1:
            return False
        if self._last_advance is None:
            return False
        return now - self._last_advance >= self.interval

    def advance(self, now: float) -> "str | None":
        """Move to the next image, re-seed the timebase from now, and return
        the new current path.

        The position wraps at the end of the sequence. A shuffle reshuffles on
        the wrap so the next cycle differs, and avoids replaying the image the
        last cycle ended on as the first of the new one.
        """
        if len(self.paths) <= 1:
            # Nothing to move to. Re-seed so a caller that calls advance()
            # directly does not spin, and return the one image unchanged.
            self._last_advance = now
            return self.current_path()
        self._pos += 1
        if self._pos >= len(self._sequence):
            self._pos = 0
            if self.order == SHUFFLE:
                last = self._sequence[-1]
                self._sequence = self._build_sequence()
                if len(self._sequence) > 1 and self._sequence[0] == last:
                    # Move the repeated head to the tail, so a wrap never shows
                    # the same image twice in a row.
                    self._sequence.append(self._sequence.pop(0))
        self._last_advance = now
        return self.current_path()

    def maybe_advance(self, now: float) -> "str | None":
        """Advance and return the new path when the interval has elapsed, or
        None when it has not. The render path swaps the background only on a
        non-None return."""
        if not self.due(now):
            return None
        return self.advance(now)
