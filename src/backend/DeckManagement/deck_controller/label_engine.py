"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

The composition engine turns a page's and an action's declared styling into
the pixels of one input. LabelManager owns the labels: defaults injection,
the epoch-stamped memos, the scroll state, and the blit recorder that keeps
a static label off FreeType on every frame. LayoutManager owns the
foreground, and BackgroundManager the colour behind both. Each one merges a
page layer with an action layer, caches the merge, and notifies the UI port.
This module imports nothing from its sibling modules in the package.
"""
import time
from copy import copy

from PIL import Image, ImageDraw, ImageOps, ImageFont
from loguru import logger as log

from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS
from src.backend.DeckManagement.ImageHelpers import hides_background
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.DeckManagement.Subclasses.render_enums import Alignment, FillMode, LabelPosition
from src.backend import ui_port

import globals as gl

from collections.abc import Iterable
from typing import Any, TYPE_CHECKING, cast, Protocol, TypedDict
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput

    class ComposedKeyLabel(KeyLabel):
        """TYPE_CHECKING-only KeyLabel whose fields are set by inject_defaults.
        The type carries the no-None invariant without adding a runtime class."""
        text: str
        font_size: float
        font_name: str
        font_weight: int
        style: str
        color: list[int]
        outline_width: int
        outline_color: list[int]
        alignment: str

    class ComposedImageLayout(ImageLayout):
        """TYPE_CHECKING-only ImageLayout whose fields are set by inject_defaults."""
        valign: float
        halign: float
        fill_mode: str
        size: float


# Share multiline-aware textbbox measurement between layout and scroll detection.
# font.getbbox treats newlines as width and can cause false scrolling.
_label_measure_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))


class _RecordingTooLarge(Exception):
    """Signal during rasterization that glyph masks exceed the retention budget.
    The caller drops the partial recording and uses direct per-frame drawing."""


class _BitmapRecorder:
    """Record ImageDraw bitmap blits while delegating ink resolution and other core operations.
    Raise _RecordingTooLarge when operation or mask-byte limits are crossed."""
    __slots__ = ("_core", "ops", "_max_ops", "_max_bytes", "_bytes")

    def __init__(self, core: Any, max_ops: int, max_bytes: int) -> None:
        self._core = core
        self.ops: list[tuple[object, ...]] = []
        self._max_ops = max_ops
        self._max_bytes = max_bytes
        self._bytes = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._core, name)

    def draw_bitmap(self, coord: "Iterable[object]", mask: "_MaskLike", ink: object) -> int:
        # The mask is an 8-bit coverage ImagingCore, so 1 byte per pixel.
        self._bytes += mask.size[0] * mask.size[1]
        if len(self.ops) >= self._max_ops or self._bytes > self._max_bytes:
            raise _RecordingTooLarge(
                f"{len(self.ops) + 1} blits / {self._bytes} mask bytes past the "
                f"{self._max_ops}-op / {self._max_bytes}-byte budget")
        self.ops.append((tuple(coord), mask, ink))
        return 0


class _MaskLike(Protocol):
    """The one attribute the blit recorder reads off a glyph mask."""

    @property
    def size(self) -> tuple[int, int]: ...


class _ScrollFrame(TypedDict):
    """One rolling label's animation state."""

    position: int
    next_step_at: "float | None"


class LabelManager:
    def __init__(self, controller_input: "ControllerInput[Any]"):
        self.controller_input = controller_input
        
        self.page_labels: dict[str, "KeyLabel"] = {}
        self.action_labels: dict[str, "KeyLabel"] = {}
        self.scroll_wait = 25
        # Stamp lock-free latch memos so a render cannot publish a pre-edit value after invalidation.
        # Content-keyed bbox, strip, and static-operation caches do not need this epoch.
        self._label_epoch: int = 0
        # Epoch-stamped widths for labels that overflow while rolling is enabled; None recomputes.
        self._scroll_widths_cache: tuple[int, dict[str, int]] | None = None
        # (epoch, bool): whether any composed label has non-empty text.
        self._has_visible_labels_cache: tuple[int, bool] | None = None
        # Cache rasterized transparent strips and anchors for scrolling labels.
        self._scroll_strips: "dict[str, tuple[tuple[object, ...], Image.Image, float, float]]" = {}
        # Cache static-label glyph blits; a None value pins that key to direct drawing.
        self._static_ops: "dict[str, tuple[tuple[object, ...], tuple[tuple[object, ...], ...] | None]]" = {}
        # Cache composed-label textbbox measurements by position and content key.
        self._bbox_cache: "dict[str, tuple[tuple[object, ...], tuple[int, int]]]" = {}
        # Cache merged page, action, and default labels under the label epoch.
        self._composed_labels_cache: tuple[int, dict[str, "ComposedKeyLabel"]] | None = None

        self.init_labels()
        # Track each rolling offset and next wall-clock step; None starts with the leading hold.
        # Wall time keeps speed stable when event wakes exceed nominal loop rate.
        self.frames: "dict[str, _ScrollFrame]" = {
            "top": {"position": 0, "next_step_at": None},
            "center": {"position": 0, "next_step_at": None},
            "bottom": {"position": 0, "next_step_at": None},
        }

    def init_labels(self) -> None:
        for position in ["top", "center", "bottom"]:
            self.page_labels[position] = KeyLabel(self.controller_input)
            self.action_labels[position] = KeyLabel(self.controller_input)
 
    def _bump_label_epoch(self) -> None:
        """Advance the epoch before dropping latch memos to reject concurrent stale publication.
        Leave content-keyed bbox, strip, and blit entries for readers to validate independently."""
        self._label_epoch += 1
        self._scroll_widths_cache = None
        self._has_visible_labels_cache = None
        self._composed_labels_cache = None

    def get_label_epoch(self) -> int:
        """Return the stamp that changes whenever composed-label appearance changes."""
        return self._label_epoch

    def invalidate_scroll_caches(self) -> None:
        """Invalidate derived label caches after any in-place attribute mutation.
        This prevents stale overflow state and strips; widths and visibility rebuild lazily."""
        self._bump_label_epoch()
        self._bbox_cache.clear()
        self._scroll_strips.clear()
        self._static_ops.clear()

    def clear_labels(self) -> None:
        self.init_labels()
        self._bump_label_epoch()
        self._scroll_strips.clear()
        self._static_ops.clear()
        self._bbox_cache.clear()

    def _on_label_written(self, position: str, update: bool,
                          notify_editor: bool = False) -> None:
        """Advance label state and clear static blits after either label store changes."""
        self._bump_label_epoch()
        self._static_ops.clear()
        if notify_editor:
            self.update_label_editor()
        if update:
            self.update_label(position)

    def set_page_label(self, position: str, label: "KeyLabel | None", update: bool = True) -> None:
        if label is None:
            label = self.page_labels[position]
            label.clear_values()
        else:
            self.page_labels[position] = label

        self._on_label_written(position, update)

    @staticmethod
    def _label_equals(a: "KeyLabel", b: "KeyLabel") -> bool:
        return (a.text == b.text and a.font_size == b.font_size
                and a.font_name == b.font_name and a.color == b.color
                and a.font_weight == b.font_weight and a.style == b.style
                and a.outline_width == b.outline_width
                and a.outline_color == b.outline_color
                and a.alignment == b.alignment)

    def set_action_label(self, position: str, label: "KeyLabel | None", update: bool = True) -> None:
        if label is None:
            label = self.action_labels[position]
            label.clear_values()
        else:
            old = self.action_labels.get(position)
            if old is not None and self._label_equals(old, label):
                return
            self.action_labels[position] = label

        self._on_label_written(position, update, notify_editor=True)

    def update_label_editor(self) -> None:
        """Forward label visual changes to the UI adapter without local widget work."""
        ui_port.get().on_input_visuals_changed(
            self.controller_input.deck_controller, self.controller_input.identifier,
            self.controller_input.state, "labels")

    def get_use_page_label_properties(self, position: str) -> dict[str, bool]:
        if self.page_labels.get(position) is None:
            return {
                "text": False,
                "color": False,
                "font-family": False,
                "font-size": False,
                "font-weight": False,
                "font-style": False,
                "outline_width": False,
                "outline_color": False,
                "alignment": False,
            }
        return {
            "text": self.page_labels[position].text is not None,
            "color": self.page_labels[position].color is not None,
            "font-family": self.page_labels[position].font_name is not None,
            "font-size": self.page_labels[position].font_size is not None,
            "font-weight": self.page_labels[position].font_weight is not None,
            "font-style": self.page_labels[position].style is not None,
            "outline_width": self.page_labels[position].outline_width is not None,
            "outline_color": self.page_labels[position].outline_color is not None,
            "alignment": self.page_labels[position].alignment is not None,
        }

    def get_composed_label(self, position: str) -> "ComposedKeyLabel":
        use_page_label_properties = self.get_use_page_label_properties(position)
        
        label = copy(self.action_labels.get(position)) or KeyLabel(self.controller_input)

        page_label = self.page_labels.get(position)
        if page_label is not None:
            if use_page_label_properties["text"]:
                label.text = page_label.text
            if use_page_label_properties["color"]:
                label.color = page_label.color
            if use_page_label_properties["font-family"]:
                label.font_name = page_label.font_name
            if use_page_label_properties["font-size"]:
                label.font_size = page_label.font_size
            if use_page_label_properties["font-weight"]:
                label.font_weight = page_label.font_weight
            if use_page_label_properties["font-style"]:
                label.style = page_label.style
            if use_page_label_properties["outline_width"]:
                label.outline_width = page_label.outline_width
            if use_page_label_properties["outline_color"]:
                label.outline_color = page_label.outline_color
            if use_page_label_properties["alignment"]:
                label.alignment = page_label.alignment

        injected = self.inject_defaults(label)
        return self.fix_invalid(injected)
    
    def get_composed_labels(self) -> dict[str, "ComposedKeyLabel"]:
        """Return epoch-memoized merged labels for all positions as shared read-only objects.
        Label edits retire the memo; font-default changes replace each LabelManager during page reload."""
        memo = self._composed_labels_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        # Capture the epoch before composition so concurrent invalidation makes this publication stale.
        epoch = self._label_epoch
        labels = {
            position: self.get_composed_label(position)
            for position in ("top", "center", "bottom")
        }
        self._composed_labels_cache = (epoch, labels)
        # Return this local generation so concurrent publication cannot swap the result mid-call.
        return labels

    
    def inject_defaults(self, label: "KeyLabel") -> "ComposedKeyLabel":
        """Fill all unset fields from app-wide defaults and return the same object as ComposedKeyLabel."""
        if label.text is None:
            label.text = ""
        if label.color is None:
            # Use a list so fallback and JSON settings have the declared runtime type.
            label.color = gl.settings_manager.font_defaults.get("font-color") or [255, 255, 255, 255]
        if label.font_name is None:
            label.font_name = gl.settings_manager.font_defaults.get("font-family") or gl.fallback_font
        if label.font_size is None:
            label.font_size = round(gl.settings_manager.font_defaults.get("font-size") or 15)
        if label.font_weight is None:
            label.font_weight = round(gl.settings_manager.font_defaults.get("font-weight") or 400)
        if label.style is None:
            label.style = gl.settings_manager.font_defaults.get("font-style") or "normal"
        if label.outline_width is None:
            label.outline_width = round(gl.settings_manager.font_defaults.get("outline-width") or 2)
        if label.outline_color is None:
            label.outline_color = gl.settings_manager.font_defaults.get("outline-color") or [0, 0, 0, 255]
        if label.alignment is None:
            label.alignment = gl.settings_manager.font_defaults.get("alignment") or "center"

        return cast("ComposedKeyLabel", label)
    
    def fix_invalid(self, label: "ComposedKeyLabel") -> "ComposedKeyLabel":
        if not isinstance(label.text, str):
            # Repair untyped plugin values even though the annotation excludes them.
            label.text = str(label.text)

        return label

    def update_label(self, position: str) -> None:
        self.controller_input.update()

    def get_available_width(self) -> int:
        return self.controller_input.get_image_size()[0]

    def get_has_visible_labels(self) -> bool:
        # Epoch-stamp visibility so passthrough cannot hide a newly nonempty label after a stale False.
        memo = self._has_visible_labels_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        epoch = self._label_epoch
        labels = self.get_composed_labels()
        visible = any(label.text not in (None, "") for label in labels.values())
        self._has_visible_labels_cache = (epoch, visible)
        return visible

    def _measure_text(self, position: str, label: "ComposedKeyLabel") -> tuple[int, int]:
        """Return cached rendered text-block size shared by scroll detection and drawing."""
        font = label.get_font()
        key = (label.text, getattr(font, "path", None), getattr(font, "size", None))
        cached = self._bbox_cache.get(position)
        if cached is not None and cached[0] == key:
            return cached[1]
        _, _, w, h = _label_measure_draw.textbbox((0, 0), label.text, font=font)
        # Cast textbbox's float annotation; integer origin and glyph bounds make these values integral.
        measured = (int(w), int(h))
        self._bbox_cache[position] = (key, measured)
        return measured

    def get_scroll_label_widths(self) -> dict[str, int]:
        """Return widths of enabled rolling labels wider than their input.
        Use the render path's multiline measurement to prevent false full-rate scrolling."""
        # Label edits invalidate by epoch; supported rolling-label changes rebuild managers by page reload.
        # Direct settings-file or plugin toggles are not supported at runtime and can remain stale until reload.
        memo = self._scroll_widths_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        epoch = self._label_epoch

        widths: dict[str, int] = {}
        rolling_labels_enabled = gl.settings_manager.app().rolling_labels
        if rolling_labels_enabled:
            available_width = self.get_available_width()
            labels = self.get_composed_labels()
            for position in labels:
                text = labels[position].text
                if text in (None, ""):
                    continue
                w, _ = self._measure_text(position, labels[position])
                if w > available_width:
                    widths[position] = w
        self._scroll_widths_cache = (epoch, widths)
        return widths

    def get_has_scroll_labels(self) -> bool:
        return bool(self.get_scroll_label_widths())

    # Define nominal rate without class-body arithmetic because import-floor tests use stub imports.
    # Methods derive wall-time cadence so event wakes do not change scroll speed.
    _NOMINAL_TICK_RATE = MEDIA_LOOP_FPS

    @property
    def SCROLL_STEP_SECONDS(self) -> float:
        return 2.0 / self._NOMINAL_TICK_RATE

    def _scroll_hold_start_seconds(self) -> float:
        return self.scroll_wait * 2.0 / self._NOMINAL_TICK_RATE

    def _scroll_hold_end_seconds(self) -> float:
        return self.scroll_wait / self._NOMINAL_TICK_RATE

    def tick_scroll_labels(self) -> bool:
        """Advance rolling-label state and report whether a visible offset changed.
        Keep rendering pure and avoid composites during holds or between steps."""
        changed = False
        now = time.monotonic()
        available_width = self.get_available_width()
        for position, w in self.get_scroll_label_widths().items():
            frame = self.frames[position]
            # Sweep from 10 pixels right of center to one pixel past 10 pixels left.
            overshoot = w - available_width + 20
            next_at = frame.get("next_step_at")
            if next_at is None:
                # A fresh label holds at the start position first.
                frame["next_step_at"] = now + self._scroll_hold_start_seconds()
                continue
            if now < next_at:
                continue
            if frame["position"] > overshoot:
                # After the trailing hold, reset and start the leading hold.
                frame["position"] = 0
                frame["next_step_at"] = now + self._scroll_hold_start_seconds()
            else:
                frame["position"] += 1
                if frame["position"] > overshoot:
                    frame["next_step_at"] = now + self._scroll_hold_end_seconds()
                else:
                    stepped = next_at + self.SCROLL_STEP_SECONDS
                    # Re-anchor after a long stall instead of advancing in a catch-up burst.
                    if stepped < now - 0.5:
                        stepped = now
                    frame["next_step_at"] = stepped
            changed = True
        return changed

    # Limit retained scroll-strip width to 4096 pixels, about 1.6 MiB at 100 pixels high.
    # Wider labels use direct drawing to prevent unbounded memory and sole-writer stalls.
    _MAX_STRIP_WIDTH = 4096
    # Also cap each RGBA strip at 4 MiB because multiline labels can bypass the width limit by height.
    _MAX_STRIP_BYTES = 4 * 1024 * 1024

    # Cap each static-label recording at 512 KiB and 512 blits to bound retained masks and writer stalls.
    # A cheap estimate rejects first; _BitmapRecorder enforces exact limits during rasterization.
    _MAX_LABEL_MASK_BYTES = 512 * 1024
    _MAX_LABEL_OPS = 512

    def _label_ops_budget_ok(self, label: "ComposedKeyLabel", w: int, h: int) -> bool:
        """Estimate whether a recording fits operation and retained-mask limits without rasterization.
        Count two passes per line and per-line stroke padding; safe overestimation precedes exact enforcement."""
        lines = label.text.count("\n") + 1
        if lines * 2 > self._MAX_LABEL_OPS:
            return False
        stroke = label.outline_width or 0
        estimated_bytes = (2 * (int(w) + 2 * stroke)
                           * (int(h) + lines * 2 * stroke))
        return estimated_bytes <= self._MAX_LABEL_MASK_BYTES

    def _composite_scroll_strip(self, image: Image.Image, position: str, label: "ComposedKeyLabel",
                                w: int, h: int, x_position: float, y_position: float) -> None:
        """Composite a cached scroll-strip window at whole-pixel offsets after baking fractional placement.
        This matches direct drawing only for opaque ink; semi-transparent ink uses different blend semantics."""
        font = label.get_font()
        outline_width = label.outline_width
        pad = outline_width + 6

        strip_width = int(w) + 2 * pad + 1
        strip_height = int(h) + 2 * pad + 1
        if strip_width > self._MAX_STRIP_WIDTH or \
                strip_width * strip_height * 4 > self._MAX_STRIP_BYTES:
            # Draw over-limit labels directly to bound retention; the byte test also catches excessive height.
            self._scroll_strips.pop(position, None)
            ImageDraw.Draw(image).text((x_position, y_position), text=label.text,
                                       font=font, anchor="mm", align=label.alignment,
                                       fill=tuple(label.color),
                                       stroke_width=outline_width,
                                       stroke_fill=tuple(label.outline_color))
            return

        ay_base = pad + h / 2
        dy = (y_position - ay_base) % 1.0
        key = (label.text, getattr(font, "path", None), label.font_size,
               tuple(label.color), outline_width, tuple(label.outline_color),
               label.alignment, w, h, dy)
        cached = self._scroll_strips.get(position)
        if cached is None or cached[0] != key:
            ax = pad + w / 2
            ay = ay_base + dy
            # Use transparent outer-ink RGB so antialiased strip edges match direct drawing.
            edge = tuple(label.outline_color[:3]) if outline_width > 0 else tuple(label.color[:3])
            strip = Image.new("RGBA", (int(w) + 2 * pad + 1, int(h) + 2 * pad + 1), edge + (0,))
            ImageDraw.Draw(strip).text((ax, ay), text=label.text, font=font,
                                       anchor="mm", align=label.alignment,
                                       fill=tuple(label.color),
                                       stroke_width=outline_width,
                                       stroke_fill=tuple(label.outline_color))
            cached = (key, strip, ax, ay)
            self._scroll_strips[position] = cached
        _, strip, ax, ay = cached

        px = round(x_position - ax)
        py = round(y_position - ay)
        # Crop to a nonnegative destination for straight-alpha OVER.
        # Masked paste would underwrite alpha on antialiased edges.
        crop_left, crop_top = max(0, -px), max(0, -py)
        crop_right = min(strip.width, image.width - px)
        crop_bottom = min(strip.height, image.height - py)
        if crop_right <= crop_left or crop_bottom <= crop_top:
            return
        if (crop_left, crop_top, crop_right, crop_bottom) != (0, 0, strip.width, strip.height):
            window = strip.crop((crop_left, crop_top, crop_right, crop_bottom))
        else:
            window = strip
        if image.mode == "RGBA":
            image.alpha_composite(window, (px + crop_left, py + crop_top))
        else:
            image.paste(window, (px + crop_left, py + crop_top), window)

    def _draw_static_label(self, image: Image.Image, draw: ImageDraw.ImageDraw,
                           position: str, label: "ComposedKeyLabel", w: int, h: int,
                           x_position: float, y_position: float, anchor: str) -> None:
        """Replay cached glyph blits in original order for exact static-label rendering on RGB or RGBA.
        Draw directly without caching when limits fail, mode differs, or PIL bypasses draw_bitmap."""
        if image.mode not in ("RGB", "RGBA") or \
                int(w) + 2 * (label.outline_width + 6) + 1 > self._MAX_STRIP_WIDTH or \
                not self._label_ops_budget_ok(label, w, h):
            self._static_ops.pop(position, None)
            if media_prof:
                media_prof.count("label_ops_fallback")
            draw.text((x_position, y_position), text=label.text, font=label.get_font(),
                      anchor=anchor, align=label.alignment, fill=tuple(label.color),
                      stroke_width=label.outline_width,
                      stroke_fill=tuple(label.outline_color))
            return

        font = label.get_font()
        # Key absolute coordinates and target geometry so resize or remeasurement cannot replay stale blits.
        key = (label.text, getattr(font, "path", None), label.font_size,
               tuple(label.color), label.outline_width, tuple(label.outline_color),
               label.alignment, anchor, x_position, y_position,
               image.size, image.mode, w, h)
        cached = self._static_ops.get(position)
        if cached is None or cached[0] != key:
            ops = self._record_label_blits(
                image, label, font, (x_position, y_position), anchor)
            # Memoize recording failure to avoid repeating an expensive attempt on each frame.
            self._static_ops[position] = (key, ops)
            # Count only replayable recordings as misses; failed recordings count as fallbacks below.
            if media_prof and ops is not None:
                media_prof.count("label_ops_miss")
        else:
            ops = cached[1]
            if media_prof and ops is not None:
                media_prof.count("label_ops_hit")

        if ops is None:
            # Draw directly after a memoized recording failure.
            if media_prof:
                media_prof.count("label_ops_fallback")
            draw.text((x_position, y_position), text=label.text, font=font,
                      anchor=anchor, align=label.alignment, fill=tuple(label.color),
                      stroke_width=label.outline_width,
                      stroke_fill=tuple(label.outline_color))
            return

        core = draw.draw
        for coord, mask, ink in ops:
            core.draw_bitmap(coord, mask, ink)

    def _record_label_blits(self, image: Image.Image, label: "ComposedKeyLabel", font: "ImageFont.FreeTypeFont",
                            xy: tuple[float, float], anchor: str) -> "tuple[tuple[object, ...], ...] | None":
        """Record draw.text mask blits on a full-size, mode-matched blank probe; require nonempty ops and no residue.
        Return None when PIL bypasses the core; RGB black residue can evade detection, but app targets are RGBA."""
        try:
            probe_image = Image.new(image.mode, image.size)
            probe = ImageDraw.Draw(probe_image)
            recorder = _BitmapRecorder(probe.draw, self._MAX_LABEL_OPS,
                                       self._MAX_LABEL_MASK_BYTES)
            probe.draw = recorder
            probe.text(xy, text=label.text, font=font, anchor=anchor,
                       align=label.alignment, fill=tuple(label.color),
                       stroke_width=label.outline_width,
                       stroke_fill=tuple(label.outline_color))
            ops = tuple(recorder.ops)
            residue = probe_image.getbbox()
        except _RecordingTooLarge as too_large:
            # Drop a partial over-budget recording and let the caller pin direct drawing.
            log.info(f"Label blit recording exceeded the retention budget "
                     f"({too_large}); falling back to the per-frame draw for "
                     f"this label")
            return None
        except Exception:
            # Use log.opt(exception=True); loguru treats exc_info as formatting and loses the traceback.
            log.opt(exception=True).warning(
                "Label blit recording failed; falling back to the per-frame "
                "draw for this label")
            return None
        if not ops or residue is not None:
            # Empty ops or probe residue means PIL used an unmodeled path, so replay would lose all or part.
            log.warning(
                f"Label blit recording did not intercept the whole draw "
                f"({len(ops)} ops, probe residue {residue}); falling back to "
                f"the per-frame draw for this label")
            return None
        return ops

    def add_labels_to_image(self, image: Image.Image) -> Image.Image:
        # image = image.rotate(self.deck.get_rotation()*-1)
        if not self.get_has_visible_labels():
            # Return the original image when empty; ControllerKey handles input identity without closing it.
            return image

        draw = ImageDraw.Draw(image)

        labels = self.get_composed_labels()
        scroll_widths = self.get_scroll_label_widths()
        for label in labels:
            text = labels[label].text
            if text in [None, ""]:
                continue

            alignment = labels[label].alignment

            w, h = self._measure_text(label, labels[label])

            if label == LabelPosition.TOP:
                y_position = h/2 + 3
            elif label == LabelPosition.BOTTOM:
                y_position = image.height - h/2 - 3
            else:
                y_position = (image.height - 0) / 2

            if label in scroll_widths:
                # Composite current scroll state without advancing it, so incidental paints do not alter animation.
                start = image.width / 2 - (image.width - w) / 2 + 10
                x_position = start - self.frames[label]["position"]
                self._composite_scroll_strip(image, label, labels[label], w, h,
                                             x_position, y_position)
                continue

            padding = 3
            if alignment == Alignment.LEFT:
                x_position = padding
                anchor_x = "l"
            elif alignment == Alignment.RIGHT:
                x_position = image.width - padding
                anchor_x = "r"
            else:  # center (default)
                x_position = image.width / 2
                anchor_x = "m"

            anchor = anchor_x + "m"

            self._draw_static_label(image, draw, label, labels[label], w, h,
                                    x_position, y_position, anchor)

        del draw

        # Return a copy so ControllerKey can close its mutated input while preserving the labelled result.
        # Touchscreen callers do not close the input, but only labelled inputs reach this copy.
        return image.copy()


class LayoutManager:
    def __init__(self, controller_input: "ControllerInput[Any]"):
        self.controller_input = controller_input

        self.action_layout = ImageLayout()
        self.page_layout = ImageLayout()

        # Atomically cache token, layout key, resized static foreground, and cover verdict.
        # Source-image identity catches in-place re-decode; the verdict expires with the resize inputs.
        self._fg_cache: "tuple[object, tuple[object, ...], Image.Image, bool] | None" = None

    def clear(self) -> None:
        self.action_layout = ImageLayout()
        self.page_layout = ImageLayout()
        self._fg_cache = None

    def get_use_page_layout_properties(self) -> dict[str, bool]:
        return {
            "valign": self.page_layout.valign is not None,
            "halign": self.page_layout.halign is not None,
            "fill-mode": self.page_layout.fill_mode is not None,
            "size": self.page_layout.size is not None
        }
    
    def get_composed_layout(self) -> "ComposedImageLayout":
        use_page_layout_properties = self.get_use_page_layout_properties()
        
        layout = copy(self.action_layout) or ImageLayout()

        page_layout = self.page_layout
        if use_page_layout_properties["valign"]:
            layout.valign = page_layout.valign
        if use_page_layout_properties["halign"]:
            layout.halign = page_layout.halign
        if use_page_layout_properties["fill-mode"]:
            layout.fill_mode = page_layout.fill_mode
        if use_page_layout_properties["size"]:
            layout.size = page_layout.size

        return self.inject_defaults(layout)
    
    def inject_defaults(self, layout: ImageLayout) -> "ComposedImageLayout":
        """Fill every unset field in place and return the same object as ComposedImageLayout."""
        if layout.valign is None:
            layout.valign = 0
        if layout.halign is None:
            layout.halign = 0
        if layout.fill_mode is None:
            if isinstance(self.controller_input.identifier, Input.Key):
                layout.fill_mode = FillMode.COVER
            else:
                layout.fill_mode = FillMode.CONTAIN
        if layout.size is None:
            layout.size = 1

        return cast("ComposedImageLayout", layout)
    
    def set_page_layout(self, layout: ImageLayout, update: bool = True) -> None:
        self.page_layout = layout

        if update:
            self.update()

    def set_action_layout(self, layout: ImageLayout, update: bool = True) -> None:
        self.action_layout = layout

        if update:
            self.update()

    def update(self) -> None:
        self.controller_input.update()
        ui_port.get().on_input_visuals_changed(
            self.controller_input.deck_controller, self.controller_input.identifier,
            self.controller_input.state, "layout")

    def get_covering_foreground(self) -> "tuple[object, ...] | None":
        """Return the identity-comparable cache entry only when its last paste covered the background.
        Static-image paths publish it, and no-paste paths clear it."""
        cached = self._fg_cache
        if cached is None or not cached[3]:
            return None
        return cached

    def foreground_proved_bare(self, cache_token: object) -> bool:
        """Return whether this token's cached paste left background visible.
        Return false without a matching entry because absence proves neither result."""
        cached = self._fg_cache
        return cached is not None and cached[0] is cache_token and not cached[3]

    def add_image_to_background(self, image: Image.Image | None, background: Image.Image, cache_token: object = None) -> Image.Image:
        if image is None:
            # Clear cover evidence when no foreground paste occurs.
            self._fg_cache = None
            return background
        layout = self.get_composed_layout()

        width, height = background.size
        image_size = (int(width * layout.size), int(height * layout.size))

        if 0 in image_size:
            self._fg_cache = None
            return background.copy()

        # Key by layout, source-image identity and geometry so in-place re-decode cannot reuse stale pixels.
        # The live asset token prevents id reuse; background size pins margins and the cover verdict.
        fg_key = (layout.fill_mode, layout.halign, layout.valign, image_size,
                  id(image), image.size, background.size)
        image_resized = None
        if cache_token is not None:
            cached = self._fg_cache
            if cached is not None and cached[0] is cache_token and cached[1] == fg_key:
                image_resized = cached[2]
                if media_prof:
                    media_prof.count("fg_cache_hit")

        resized = image_resized is None
        if image_resized is None:
            if layout.fill_mode == FillMode.STRETCH:
                image_resized = image.resize(image_size, Image.Resampling.HAMMING)
            elif layout.fill_mode == FillMode.COVER:
                image_resized = ImageOps.cover(image, image_size, Image.Resampling.HAMMING)
            else:
                image_resized = ImageOps.contain(image, image_size, Image.Resampling.HAMMING)

        halign = layout.halign
        valign = layout.valign

        left_margin = int((background.width - image_resized.width) * (halign + 1) / 2)
        top_margin = int((background.height - image_resized.height) * (valign + 1) / 2)

        # Cache the cover scan with the resize after computing its required margins.
        if cache_token is None:
            # Without a token, clear cover evidence because no stable cache identity exists.
            self._fg_cache = None
        elif resized:
            self._fg_cache = (cache_token, fg_key, image_resized,
                              hides_background(image_resized, left_margin, top_margin,
                                               background.size))
            if media_prof:
                media_prof.count("fg_cache_miss")

        final_image = background.copy()

        if image_resized.has_transparency_data:
            final_image.paste(image_resized, (left_margin, top_margin), image_resized)
        else:
            final_image.paste(image_resized, (left_margin, top_margin))

        return final_image
    

class BackgroundManager:
    def __init__(self, controller_input: "ControllerInput[Any]"):
        self.controller_input = controller_input
        
        self.action_color: list[int] | None = None
        self.page_color: list[int] | None = None

    def set_action_color(self, color: list[int], update: bool = True) -> None:
        self.action_color = color
        if isinstance(color, list) and len(color) == 3:
            self.action_color.append(255)

        if update:
            self.update()

    def set_page_color(self, color: list[int] | None, update: bool = True, update_ui: bool = True) -> None:
        self.page_color = color
        if isinstance(color, list) and len(color) == 3:
            color.append(255)

        if update:
            self.update(ui=update_ui)

    def update(self, ui: bool = True) -> None:
        self.controller_input.update()
        if ui:
            ui_port.get().on_input_visuals_changed(
                self.controller_input.deck_controller, self.controller_input.identifier,
                self.controller_input.state, "background")

    def get_color_is_set(self, color: list[int] | None) -> bool:
        return color not in [None, [None]*3, [None]*4]

    def get_use_page_background(self) -> bool:
        return self.get_color_is_set(self.page_color)
    
    def get_composed_color(self) -> list[int]:
        # Keep explicit None checks so static analysis proves non-None return values.
        page_color = self.page_color
        action_color = self.action_color
        if self.get_use_page_background() and page_color is not None and self.get_color_is_set(page_color):
            return page_color
        elif action_color is not None and self.get_color_is_set(action_color):
            return action_color
        else:
            return [0] * 4
