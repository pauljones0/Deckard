#!/usr/bin/env python3
"""Visual strip-band alignment on a color-coded test card (#418, round three).

Opens the SD+ directly with the StreamDeck library. No app instance may be
running. Paints a generated calibration card across the keys and the strip
with the four geometry parameters live-adjustable, or an actual image fitted
the way background_media fits one (key 7 flips between the two without
losing the values).

The card carries two line families, color-coded so every line identifies
itself across a bezel:

    diagonals  WARM colors (red, orange, yellow, magenta), 45 degrees.
               A vertical-gap error (gap, sy, bandh) shows as a horizontal
               jog where a diagonal crosses the bezel or the strip boundary;
               match the COLOR to know which diagonal continues where.
    verticals  COOL colors (blue, cyan, green, white), straight down.
               A span error spreads or squeezes them on the strip; an xoff
               error shifts them all sideways by the same amount.

In card mode the pattern is drawn at exact canvas scale on every repaint, so
its spacing is pixel-true and never resampled by a source fit. In image mode
the source goes through the production ImageOps.fit.

Controls, live repaint on every change:

    dial 1 turn   sx     horizontal key gap, +/- 2 per tick
    dial 2 turn   sy     vertical key gap, +/- 2 per tick
    dial 3 turn   gap    key-to-strip gap, +/- 2 per tick
    dial 4 turn   span   strip view width, +/- 4 per tick
    key 1..4      xoff   -8 / -2 / +2 / +8
    key 5 / 6     bandh  -2 / +2 (band height)
    key 7         flip card <-> image
    key 8         print the current values to stdout
    Ctrl+C        print FINAL values and blank the deck

Usage:
    hw_418_strip_align.py [IMAGE] [SX SY GAP SPAN [XOFF [BANDH]]]

IMAGE is an optional path for image mode's picture; without one, image mode
uses the deck's configured wallpaper. The numbers default to the shipped
calibration (20 36 40 516 0 72).
"""

import json
import os
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageOps
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices.StreamDeck import DialEventType
from StreamDeck.ImageHelpers import PILHelper

DECK_SETTINGS = os.path.expanduser(
    "~/.local/share/deckard/data/settings/decks/5A5101JD9NN.json")

# Warm palette for the diagonals, cool palette for the verticals. Within a
# family neighbours always differ, and the family tells the line's role.
DIAG_COLORS = [(230, 50, 50), (245, 145, 30), (240, 210, 40), (220, 60, 200)]
VERT_COLORS = [(65, 110, 255), (50, 220, 220), (60, 210, 60), (240, 240, 240)]

DIAG_STEP, DIAG_WIDTH = 70, 24
VERT_STEP, VERT_WIDTH = 45, 8


def make_card(w: int, h: int) -> Image.Image:
    """The calibration card at exact canvas size: warm 45-degree diagonals
    under cool verticals, on a dark ground."""
    img = Image.new("RGB", (w, h), (12, 12, 28))
    d = ImageDraw.Draw(img)
    for i, x0 in enumerate(range(-h - DIAG_STEP, w + h, DIAG_STEP)):
        d.line([(x0, 0), (x0 + h, h)], fill=DIAG_COLORS[i % 4], width=DIAG_WIDTH)
    for i, x in enumerate(range(22, w, VERT_STEP)):
        d.line([(x, 0), (x, h)], fill=VERT_COLORS[i % 4], width=VERT_WIDTH)
    return img


def default_image_path() -> "str | None":
    try:
        with open(DECK_SETTINGS) as f:
            path = json.load(f).get("background", {}).get("media-path")
        if path and os.path.isfile(path):
            return path
    except (OSError, ValueError):
        pass
    return None


class Params:
    def __init__(self, sx: int, sy: int, gap: int, span: int,
                 xoff: int, bandh: int) -> None:
        self.lock = threading.Lock()
        self.sx, self.sy, self.gap, self.span = sx, sy, gap, span
        self.xoff, self.bandh = xoff, bandh
        self.card_mode = True
        self.dirty = threading.Event()
        self.dirty.set()

    def bump(self, name: str, delta: int) -> None:
        with self.lock:
            value = getattr(self, name) + delta
            if name == "span":
                value = max(60, value)
            elif name == "bandh":
                value = max(8, value)
            elif name != "xoff":
                value = max(0, value)
            setattr(self, name, value)
        self.dirty.set()

    def flip_mode(self) -> None:
        with self.lock:
            self.card_mode = not self.card_mode
        self.dirty.set()

    def snapshot(self):
        with self.lock:
            return (self.sx, self.sy, self.gap, self.span,
                    self.xoff, self.bandh, self.card_mode)

    def label(self) -> str:
        sx, sy, gap, span, xoff, bandh, _ = self.snapshot()
        return f"sx={sx} sy={sy} gap={gap} span={span} xoff={xoff} bandh={bandh}"


# Horizontal canvas margin beyond the key grid, so a strip view wider than
# the grid still reads real pattern instead of black padding.
MARGIN = 160


def paint(deck, source: "Image.Image | None", params: Params) -> None:
    sx, sy, gap, span, xoff, bandh, card_mode = params.snapshot()
    rows, cols = deck.key_layout()
    key_w, key_h = deck.key_image_format()["size"]
    grid_w = key_w * cols + sx * (cols - 1)
    grid_h = key_h * rows + sy * (rows - 1)
    canvas_w = grid_w + 2 * MARGIN
    canvas_h = grid_h + gap + bandh

    if card_mode or source is None:
        canvas = make_card(canvas_w, canvas_h)
    else:
        # The production fit: aspect-preserving center crop to the canvas.
        canvas = ImageOps.fit(source.convert("RGB"), (canvas_w, canvas_h),
                              Image.Resampling.LANCZOS)

    for row in range(rows):
        for col in range(cols):
            x = MARGIN + col * (key_w + sx)
            y = row * (key_h + sy)
            tile = canvas.crop((x, y, x + key_w, y + key_h))
            deck.set_key_image(row * cols + col,
                               PILHelper.to_native_key_format(deck, tile))

    fmt = deck.touchscreen_image_format()
    tw, th = fmt["size"]
    left = MARGIN + (grid_w - span) // 2 + xoff
    band = canvas.crop((left, canvas_h - bandh, left + span, canvas_h)).resize((tw, th))
    sd = ImageDraw.Draw(band)
    text = params.label()
    sd.rectangle((0, 0, 8 + 7 * len(text), 18), fill=(0, 0, 0))
    sd.text((4, 3), text, fill=(255, 255, 255))
    deck.set_touchscreen_image(
        PILHelper.to_native_touchscreen_format(deck, band), 0, 0, tw, th)


def main() -> int:
    args = sys.argv[1:]
    image_path = None
    if args and not args[0].lstrip("-").isdigit():
        image_path = args[0]
        args = args[1:]
    numbers = [int(a) for a in args] if args else []
    defaults = [20, 36, 40, 516, 0, 72]
    numbers = (numbers + defaults[len(numbers):])[:6]

    if image_path is None:
        image_path = default_image_path()
    source = None
    if image_path is not None:
        try:
            source = Image.open(image_path)
            print(f"image mode source: {image_path} {source.size}", flush=True)
        except OSError as e:
            print(f"cannot read {image_path} ({e}); card mode only", flush=True)

    decks = DeviceManager().enumerate()
    plus = [d for d in decks if "plus" in type(d).__name__.lower()]
    if not plus:
        print(f"no SD+ found (saw: {[type(d).__name__ for d in decks]})")
        return 1

    params = Params(*numbers)
    knobs = {0: ("sx", 2), 1: ("sy", 2), 2: ("gap", 2), 3: ("span", 4)}

    deck = plus[0]
    deck.open()
    try:
        # The deck can refuse feature reports for a moment after an unclean
        # close by a previous holder, while data writes already work. Retry
        # briefly, then continue without the reset; painting needs only data
        # writes and overwrites every surface anyway.
        for attempt in range(3):
            try:
                deck.reset()
                deck.set_brightness(80)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"reset/brightness kept failing ({e}); painting anyway",
                          flush=True)
                else:
                    time.sleep(1.0)

        def on_dial(_deck, dial, event, value):
            if event is DialEventType.TURN:
                knob = knobs.get(dial)
                if knob and value:
                    field, step = knob
                    params.bump(field, step * int(value))
            elif event is DialEventType.PUSH and value:
                print(params.label(), flush=True)

        def on_key(_deck, key, pressed):
            if not pressed:
                return
            xoff_steps = {0: -8, 1: -2, 2: 2, 3: 8}
            if key in xoff_steps:
                params.bump("xoff", xoff_steps[key])
            elif key == 4:
                params.bump("bandh", -2)
            elif key == 5:
                params.bump("bandh", 2)
            elif key == 6:
                params.flip_mode()
            else:
                print(params.label(), flush=True)

        deck.set_dial_callback(on_dial)
        deck.set_key_callback(on_key)

        print("card mode: WARM diagonals judge gap/sy/bandh, COOL verticals "
              "judge span/xoff. dials: 1=sx 2=sy 3=gap 4=span; keys 1-4 xoff, "
              "5/6 bandh, 7 card<->image, 8 print; Ctrl+C ends", flush=True)
        while True:
            params.dirty.wait()
            params.dirty.clear()
            paint(deck, source, params)
            time.sleep(0.03)
    except KeyboardInterrupt:
        print(f"FINAL: {params.label()}", flush=True)
    finally:
        try:
            deck.reset()
        except Exception:
            pass
        finally:
            deck.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
