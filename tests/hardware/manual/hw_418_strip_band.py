#!/usr/bin/env python3
"""Visual calibration of the SD+ key spacing and strip band (#418).

Opens the SD+ directly with the StreamDeck library. No app instance may be
running. Paints one continuous pattern across the keys and the strip the way
background_media composes an extended background, with four free variables:

    sx    horizontal key gap in canvas pixels
    sy    vertical key gap in canvas pixels
    gap   vertical canvas gap between the bottom key row and the strip band
    span  horizontal width of the strip's physical view, in canvas pixels,
          centered on the key grid; the band height follows as 100*span/800

The shipped code uses sx=sy=20, gap=sy, span=key-grid width (540 at sx=20).

Pattern: diagonal stripes reveal the gaps (a wrong gap jogs a stripe at a
bezel); the cyan vertical lines through each key column center reveal the
span (a wrong span bends or rescales them on the strip). Stripes that read
WIDER on the strip than on the keys mean the strip magnifies: the span is
too small. Same width everywhere means the scale is right.

Controls, live repaint on every change:

    dial 1 turn   sx    +/- 2 per tick
    dial 2 turn   sy    +/- 2 per tick
    dial 3 turn   gap   +/- 2 per tick
    dial 4 turn   span  +/- 8 per tick
    top-row keys  xoff  -8 / -2 / +2 / +8 (the strip view's horizontal
                  offset from centered-on-the-grid; + moves the view right,
                  so the content slides left)
    keys 6 and 7  bandh -2 / +2 (the band height in canvas pixels; the first
                  press leaves the follow-the-span auto mode, shown with a
                  trailing * on the label)
    dial press, key 5 or key 8: print the current values to stdout

The strip's top-left corner shows the values continuously. Ctrl+C ends,
prints the final values, and blanks the deck.

Start from other values:  hw_418_strip_band.py SX SY GAP SPAN [XOFF]
"""

import sys
import threading
import time

from PIL import Image, ImageDraw
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices.StreamDeck import DialEventType
from StreamDeck.ImageHelpers import PILHelper

MARGIN = 240  # horizontal canvas margin so a span wider than the grid fits


def build_canvas(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), (10, 10, 30))
    d = ImageDraw.Draw(img)
    for x0 in range(-h, w + h, 60):
        d.line([(x0, 0), (x0 + h, h)], fill=(240, 200, 40), width=12)
    return img


class Params:
    def __init__(self, sx: int, sy: int, gap: int, span: int, xoff: int = 0,
                 bandh: int = 0) -> None:
        self.lock = threading.Lock()
        self.sx, self.sy, self.gap, self.span, self.xoff = sx, sy, gap, span, xoff
        # Height of the band in canvas pixels. 0 = follow the span at the
        # strip's own 800:100 aspect. A nonzero value decouples the vertical
        # scale, for the case where the strip is anamorphic against the keys.
        self.bandh = bandh
        self.dirty = threading.Event()
        self.dirty.set()

    def bump(self, name: str, delta: int) -> None:
        with self.lock:
            if name == "bandh" and self.bandh <= 0:
                # Leave auto mode from the currently shown height.
                self.bandh = max(1, round(100 * self.span / 800))
            value = getattr(self, name) + delta
            if name == "span":
                value = max(60, value)
            elif name != "xoff":
                value = max(0, value)
            setattr(self, name, value)
        self.dirty.set()

    def snapshot(self) -> tuple[int, int, int, int, int, int]:
        with self.lock:
            bandh = self.bandh if self.bandh > 0 else max(1, round(100 * self.span / 800))
            return self.sx, self.sy, self.gap, self.span, self.xoff, bandh

    def label(self) -> str:
        sx, sy, gap, span, xoff, bandh = self.snapshot()
        auto = "*" if self.bandh <= 0 else ""
        return (f"sx={sx} sy={sy} gap={gap} span={span} xoff={xoff} "
                f"bandh={bandh}{auto}")


def paint(deck, params: Params) -> None:
    sx, sy, gap, span, xoff, slice_h = params.snapshot()
    rows, cols = deck.key_layout()
    key_w, key_h = deck.key_image_format()["size"]
    grid_w = key_w * cols + sx * (cols - 1)
    grid_h = key_h * rows + sy * (rows - 1)
    canvas_w = grid_w + 2 * MARGIN
    canvas_h = grid_h + gap + slice_h
    canvas = build_canvas(canvas_w, canvas_h)

    d = ImageDraw.Draw(canvas)
    for col in range(cols):
        cx = MARGIN + col * (key_w + sx) + key_w // 2
        d.line([(cx, 0), (cx, canvas_h)], fill=(80, 220, 255), width=8)

    for row in range(rows):
        for col in range(cols):
            x = MARGIN + col * (key_w + sx)
            y = row * (key_h + sy)
            tile = canvas.crop((x, y, x + key_w, y + key_h))
            deck.set_key_image(row * cols + col,
                               PILHelper.to_native_key_format(deck, tile))

    fmt = deck.touchscreen_image_format()
    tw, th = fmt["size"]
    strip_x = MARGIN + (grid_w - span) // 2 + xoff
    band = canvas.crop((strip_x, grid_h + gap, strip_x + span,
                        grid_h + gap + slice_h)).resize((tw, th))
    sd = ImageDraw.Draw(band)
    sd.text((6, 4), params.label(), fill=(255, 60, 60))
    deck.set_touchscreen_image(
        PILHelper.to_native_touchscreen_format(deck, band), 0, 0, tw, th)


def main() -> int:
    decks = DeviceManager().enumerate()
    plus = [d for d in decks if "plus" in type(d).__name__.lower()]
    if not plus:
        print(f"no SD+ found (saw: {[type(d).__name__ for d in decks]})")
        return 1

    args = [int(a) for a in sys.argv[1:7]] if len(sys.argv) >= 5 else [20, 20, 20, 540]
    params = Params(*args)
    knobs = {0: ("sx", 2), 1: ("sy", 2), 2: ("gap", 2), 3: ("span", 8)}

    deck = plus[0]
    deck.open()
    try:
        deck.reset()
        deck.set_brightness(80)

        def on_dial(_deck, dial, event, value):
            # TURN arrives with a signed tick count; PUSH with a bool.
            if event is DialEventType.TURN:
                knob = knobs.get(dial)
                if knob and value:
                    field, step = knob
                    params.bump(field, step * int(value))
            elif event is DialEventType.PUSH and value:
                print(params.label(), flush=True)

        def on_key(_deck, key, pressed):
            # Top-row keys nudge the strip's horizontal offset: coarse at the
            # edges, fine in the middle. Bottom-row keys print the values.
            if not pressed:
                return
            xoff_steps = {0: -8, 1: -2, 2: 2, 3: 8}
            bandh_steps = {5: -2, 6: 2}
            if key in xoff_steps:
                params.bump("xoff", xoff_steps[key])
            elif key in bandh_steps:
                params.bump("bandh", bandh_steps[key])
            else:
                print(params.label(), flush=True)

        deck.set_dial_callback(on_dial)
        deck.set_key_callback(on_key)

        print("dials: 1=sx 2=sy 3=gap 4=span; top keys nudge xoff (-8/-2/+2/+8); "
              "bottom keys or dial press print values; Ctrl+C ends", flush=True)
        while True:
            params.dirty.wait()
            params.dirty.clear()
            paint(deck, params)
            time.sleep(0.03)  # coalesce fast dial ticks between repaints
    except KeyboardInterrupt:
        print(f"FINAL: {params.label()}", flush=True)
    finally:
        try:
            deck.reset()
        finally:
            deck.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
