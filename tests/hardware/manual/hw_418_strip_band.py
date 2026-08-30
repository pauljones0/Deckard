#!/usr/bin/env python3
"""With Deckard stopped and exclusive deck access, calibrate strip geometry.
Dials set sx/sy/gap/span; keys 1-4 xoff, 5/8 print, 6/7 bandh."""

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
        # Zero follows 800:100; nonzero sets an independent height
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
