#!/usr/bin/env python3
"""With Deckard stopped, align marker and lit key edge to a straightedge.
Record eight key edges left to right with dial presses; keys undo."""

import contextlib
import sys
import threading
import time

from PIL import Image, ImageDraw
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices.StreamDeck import DialEventType
from StreamDeck.ImageHelpers import PILHelper

# Must match hw_418_strip_align.py, so the derived xoff transfers as-is.
MARGIN = 160
KEY_W = 120


class Measurement:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.marker = 60
        self.recorded: list[int] = []
        self.passes: list[tuple[int, int, int]] = []
        self.message = ""
        self.dirty = threading.Event()
        self.dirty.set()

    def move(self, delta: int) -> None:
        with self.lock:
            self.marker = max(0, min(799, self.marker + delta))
        self.dirty.set()

    def record(self) -> None:
        with self.lock:
            if len(self.recorded) < 8:
                self.recorded.append(self.marker)
        self.dirty.set()

    def undo(self) -> None:
        with self.lock:
            if self.recorded:
                self.marker = self.recorded.pop()
        self.dirty.set()

    def snapshot(self) -> tuple[int, list[int]]:
        with self.lock:
            return self.marker, list(self.recorded)

    def reset_pass(self) -> None:
        with self.lock:
            self.recorded = []
        self.dirty.set()


def compute(taps: list[int]) -> "tuple[int, int, int, str] | None":
    """(sx, span, xoff, report) from 8 ascending edge positions, or None."""
    if len(taps) != 8:
        return None
    if any(b <= a for a, b in zip(taps, taps[1:])):
        return None
    widths = [taps[2 * i + 1] - taps[2 * i] for i in range(4)]
    gaps = [taps[2 * i + 2] - taps[2 * i + 1] for i in range(3)]
    mean_w = sum(widths) / 4.0
    if mean_w <= 0:
        return None
    # Buffer px -> canvas px conversion measured from the known 120-canvas-px
    # key width.
    canvas_per_buffer = KEY_W / mean_w
    span = 800.0 * canvas_per_buffer
    sx = (sum(gaps) / 3.0) * canvas_per_buffer
    grid_w = 4 * KEY_W + 3 * sx
    # Where the strip's left edge sits in canvas coordinates: every recorded
    # edge has a known canvas position, so average over all eight.
    lefts = []
    for i, b in enumerate(taps):
        col, is_right = divmod(i, 2)
        edge_canvas = MARGIN + col * (KEY_W + sx) + (KEY_W if is_right else 0)
        lefts.append(edge_canvas - b * canvas_per_buffer)
    left = sum(lefts) / len(lefts)
    xoff = left - MARGIN - (grid_w - span) / 2.0
    spread = (max(widths) - min(widths)) / mean_w * 100.0
    report = (
        f"widths(buf px)={widths} gaps={gaps} spread={spread:.1f}%\n"
        f"MEASURED: sx={round(sx)} span={round(span)} xoff={round(xoff)} "
        f"(bandh at square pixels would be {round(span / 8.0)})"
    )
    at_edge = [b for b in (taps[0], taps[7]) if b <= 1 or b >= 798]
    if at_edge:
        report += (f"\nNOTE: recording(s) at the strip's physical edge {at_edge}: "
                   f"that key edge lies beyond the strip; treat as a bound")
    if spread > 6.0:
        report += "\nWARNING: per-key width spread above 6%"
    return round(sx), round(span), round(xoff), report


def paint(deck, meas: Measurement) -> None:
    marker, recorded = meas.snapshot()
    n = len(recorded)
    _rows, cols = deck.key_layout()
    target_col, target_right = divmod(min(n, 7), 2)

    for col in range(cols):
        top = Image.new("RGB", (120, 120), (25, 25, 35))
        deck.set_key_image(col, PILHelper.to_native_key_format(deck, top))

        is_target = col == target_col and n < 8
        img = Image.new("RGB", (120, 120), (30, 60, 160) if is_target else (35, 35, 45))
        d = ImageDraw.Draw(img)
        if is_target:
            if target_right:
                d.rectangle((112, 0, 119, 119), fill=(255, 255, 255))
            else:
                d.rectangle((0, 0, 7, 119), fill=(255, 255, 255))
        deck.set_key_image(cols + col, PILHelper.to_native_key_format(deck, img))

    fmt = deck.touchscreen_image_format()
    tw, th = fmt["size"]
    band = Image.new("RGB", (tw, th), (10, 10, 20))
    sd = ImageDraw.Draw(band)
    # Faint dots at every recorded position, then the live marker.
    for r in recorded:
        sd.line([(r, 0), (r, th)], fill=(80, 80, 60), width=1)
    sd.line([(marker, 0), (marker, th)], fill=(40, 255, 60), width=3)
    sd.polygon([(marker - 6, 0), (marker + 6, 0), (marker, 12)], fill=(40, 255, 60))
    if n < 8:
        edge = "RIGHT" if target_right else "LEFT"
        sd.text((8, th - 34),
                f"{n + 1}/8 key {target_col + 1} {edge}: dial1 coarse, dial2 fine, "
                f"press a dial to record  x={marker}",
                fill=(255, 255, 255))
    if meas.message:
        sd.text((8, th - 16), meas.message, fill=(120, 255, 120))
    deck.set_touchscreen_image(
        PILHelper.to_native_touchscreen_format(deck, band), 0, 0, tw, th)


def main() -> int:
    seed_sy = int(sys.argv[1]) if len(sys.argv) > 1 else 34
    seed_gap = int(sys.argv[2]) if len(sys.argv) > 2 else 62

    decks = DeviceManager().enumerate()
    plus = [d for d in decks if "plus" in type(d).__name__.lower()]
    if not plus:
        print(f"no SD+ found (saw: {[type(d).__name__ for d in decks]})")
        return 1

    meas = Measurement()
    deck = plus[0]
    deck.open()
    try:
        for attempt in range(3):
            try:
                deck.reset()
                deck.set_brightness(80)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"reset/brightness kept failing ({e}); painting anyway", flush=True)
                else:
                    time.sleep(1.0)

        def on_dial(_deck, dial, event, value):
            if event is DialEventType.TURN and value:
                step = 8 if dial == 0 else 1
                meas.move(step * int(value))
            elif event is DialEventType.PUSH and value:
                meas.record()
                _, recorded = meas.snapshot()
                print(f"recorded {len(recorded)}/8: x={recorded[-1]}", flush=True)

        def on_key(_deck, _key, pressed):
            if pressed:
                meas.undo()
                _, recorded = meas.snapshot()
                print(f"undo; recorded now {recorded}", flush=True)

        deck.set_dial_callback(on_dial)
        deck.set_key_callback(on_key)

        print("null-marker mode: dial1 coarse / dial2 fine, dial press records, "
              "any key undoes; Ctrl+C ends", flush=True)
        while True:
            meas.dirty.wait()
            meas.dirty.clear()
            _, recorded = meas.snapshot()
            if len(recorded) == 8:
                result = compute(recorded)
                if result is None:
                    print(f"recordings not ascending, restarting pass: {recorded}", flush=True)
                    meas.message = "not left-to-right; pass restarted"
                else:
                    sx, span, xoff, report = result
                    print(report, flush=True)
                    print(f"restart alignment with:\n"
                          f"hw_418_strip_align.py {sx} {seed_sy} {seed_gap} "
                          f"{span} {xoff} {round(span / 8)}", flush=True)
                    meas.message = f"MEASURED sx={sx} span={span} xoff={xoff}"
                    meas.passes.append((sx, span, xoff))
                meas.reset_pass()
                continue
            paint(deck, meas)
            time.sleep(0.03)
    except KeyboardInterrupt:
        for i, (sx, span, xoff) in enumerate(meas.passes):
            print(f"pass {i + 1}: sx={sx} span={span} xoff={xoff}", flush=True)
    finally:
        with contextlib.suppress(Exception):
            deck.reset()
        deck.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
