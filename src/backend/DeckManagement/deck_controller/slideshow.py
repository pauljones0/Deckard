"""Track wallpaper-slideshow order, current path, and caller-supplied timing.

The model owns no images, I/O, toolkit, clock, sleep, or cancellation."""
from __future__ import annotations

import random
from collections.abc import Sequence

#: Walk the list in the order the user built it.
IN_ORDER = "in-order"
#: Walk a shuffled permutation, reshuffled each time it wraps.
SHUFFLE = "shuffle"


class Slideshow:
    """Rotate image paths in order or by shuffled cycles.
    Fewer than two paths or a nonpositive interval never advances."""

    def __init__(self, paths: Sequence[str], interval: float, order: str = IN_ORDER,
                 rng: "random.Random | None" = None) -> None:
        # Remove only non-string and empty entries; the render path checks files later.
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
        # Keep an opaque page identity so media ticks reject an inactive page's rotation.
        self.page: object | None = None
        # Per-image viewports keyed by path; missing entries use the default view.
        # The render layer reads them when each frame enters the rotation.
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
        """Seed after installing the first frame so the first swap waits one interval."""
        self._last_advance = now

    def due(self, now: float) -> bool:
        """Return whether the seeded interval elapsed.
        Return false for fewer than two images, nonpositive interval, or no seed."""
        if self.interval <= 0 or len(self.paths) <= 1:
            return False
        if self._last_advance is None:
            return False
        return now - self._last_advance >= self.interval

    def advance(self, now: float) -> "str | None":
        """Advance, reseed, and return the current path, wrapping at sequence end.
        Shuffle rebuilds each cycle and prevents the boundary image from repeating."""
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
        """Return the advanced path when due, or None when no background swap is needed."""
        if not self.due(now):
            return None
        return self.advance(now)
