"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import uuid
from dataclasses import dataclass

from StreamDeck.DeviceManager import USBVendorIDs

import globals as gl
from typing import Any, cast


@dataclass(frozen=True)
class SurfaceFormat:
    """Device image format for a key, touchscreen, or screen surface.
    Defaults match the driver base class for a surface the model lacks."""

    size: tuple[int, int] = (0, 0)
    format: str = ""
    flip: tuple[bool, bool] = (False, False)
    rotation: int = 0

    def as_dict(self) -> "dict[str, Any]":
        """The image-format dict a device class returns for this surface."""
        return {"size": self.size, "format": self.format,
                "flip": self.flip, "rotation": self.rotation}


@dataclass(frozen=True)
class FakeDeckModel:
    """Device geometry, surfaces, controls, and USB identity for a fake deck.
    Validation rejects Elgato IDs and missing key or touchscreen sizes for enabled features."""

    name: str
    key_layout: tuple[int, int]
    key_format: SurfaceFormat = SurfaceFormat()
    dial_count: int = 0
    is_touch: bool = False
    touchscreen_format: SurfaceFormat = SurfaceFormat()
    touch_key_count: int = 0
    screen_format: SurfaceFormat = SurfaceFormat()
    is_visual: bool = True
    deck_type: "str | None" = None
    vendor_id: int = 0
    product_id: int = 0

    def __post_init__(self) -> None:
        if self.vendor_id == USBVendorIDs.USB_VID_ELGATO:
            raise ValueError(
                f"Fake deck model {self.name!r} carries the Elgato vendor id. The USB reset "
                f"accepts that identity and, with no serial match, resets the one device of "
                f"this product id on the bus, which is real hardware. Leave vendor_id at 0.")
        if self.is_touch and min(self.touchscreen_format.size) <= 0:
            raise ValueError(
                f"Fake deck model {self.name!r} takes touch events on a touchscreen of size "
                f"{self.touchscreen_format.size}. Give it a touchscreen image size, or set "
                f"is_touch to False.")
        if self.is_visual and min(self.key_format.size) <= 0:
            raise ValueError(
                f"Fake deck model {self.name!r} is a visual deck whose keys are "
                f"{self.key_format.size}. The image helpers divide by the key size, so this "
                f"fails as a deck that will not initialize. Give it a key image size, or set "
                f"is_visual to False.")


# Stable synthetic default: SD+ controls with an Original key image format.
# Use a named preset when real model geometry is required.
DEFAULT_FAKE_DECK_MODEL = FakeDeckModel(
    name="default",
    key_layout=(2, 4),
    key_format=SurfaceFormat(size=(72, 72), format="JPEG", flip=(True, True)),
    dial_count=4,
    is_touch=True,
    touchscreen_format=SurfaceFormat(size=(800, 100), format="JPEG"),
    screen_format=SurfaceFormat(format="JPEG"),
)

# Real model fields match the driver; Original and MK.2 share a type name but
# differ in key image format and product ID.
FAKE_DECK_PRESETS: "tuple[FakeDeckModel, ...]" = (
    DEFAULT_FAKE_DECK_MODEL,
    FakeDeckModel(
        name="original",
        key_layout=(3, 5),
        key_format=SurfaceFormat(size=(72, 72), format="BMP", flip=(True, True)),
        deck_type="Stream Deck Original",
        product_id=0x0060,
    ),
    FakeDeckModel(
        name="mk2",
        key_layout=(3, 5),
        key_format=SurfaceFormat(size=(72, 72), format="JPEG", flip=(True, True)),
        deck_type="Stream Deck Original",
        product_id=0x0080,
    ),
    FakeDeckModel(
        name="mini",
        key_layout=(2, 3),
        key_format=SurfaceFormat(size=(80, 80), format="BMP", flip=(False, True),
                                rotation=90),
        deck_type="Stream Deck Mini",
        product_id=0x0063,
    ),
    FakeDeckModel(
        name="xl",
        key_layout=(4, 8),
        key_format=SurfaceFormat(size=(96, 96), format="JPEG", flip=(True, True)),
        deck_type="Stream Deck XL",
        product_id=0x006C,
    ),
    FakeDeckModel(
        name="plus",
        key_layout=(2, 4),
        key_format=SurfaceFormat(size=(120, 120), format="JPEG"),
        dial_count=4,
        is_touch=True,
        touchscreen_format=SurfaceFormat(size=(800, 100), format="JPEG"),
        deck_type="Stream Deck +",
        product_id=0x0084,
    ),
    FakeDeckModel(
        name="neo",
        key_layout=(2, 4),
        key_format=SurfaceFormat(size=(96, 96), format="JPEG", flip=(True, True)),
        touch_key_count=2,
        screen_format=SurfaceFormat(size=(248, 58), format="JPEG", flip=(True, True)),
        deck_type="Stream Deck Neo",
        product_id=0x009A,
    ),
    FakeDeckModel(
        name="pedal",
        key_layout=(1, 3),
        is_visual=False,
        deck_type="Stream Deck Pedal",
        product_id=0x0086,
    ),
)

FAKE_DECK_MODELS: "dict[str, FakeDeckModel]" = {model.name: model for model in FAKE_DECK_PRESETS}


def fake_deck_model(model: "str | FakeDeckModel | None") -> FakeDeckModel:
    """Resolve a preset name or model, using the default for None.
    Unknown names raise with the available presets instead of changing geometry."""
    if model is None:
        return DEFAULT_FAKE_DECK_MODEL
    if isinstance(model, FakeDeckModel):
        return model
    try:
        return FAKE_DECK_MODELS[model.strip().casefold()]
    except KeyError:
        raise ValueError(
            f"Unknown fake deck model {model!r}. The models are: "
            f"{', '.join(sorted(FAKE_DECK_MODELS))}") from None


class FakeDeck:
    def __init__(self, serial_number: "str | None" = None, deck_type: "str | None" = None,
                 key_layout: list[int] | None = None,
                 model: "str | FakeDeckModel | None" = None) -> None:
        if serial_number is None:
            # The settings store is keyed by the deck serial and refuses a
            # None key, so the settings read below failed anyway.
            raise ValueError("FakeDeck needs a serial number")
        self.serial_number: str = serial_number
        self.model: FakeDeckModel = fake_deck_model(model)
        self._deck_type = deck_type if deck_type is not None else self.model.deck_type

        self.is_fake = True

        requested_layout = key_layout if key_layout is not None else list(self.model.key_layout)
        if model is None:
            # Without a named model, persisted grid settings override key_layout.
            self._key_layout = gl.settings_manager.get_deck_settings(self.serial_number).get("key-layout", requested_layout)
        else:
            # A named model ignores persisted geometry; key_layout can override its grid.
            self._key_layout = requested_layout

        # Keep one ID per instance so deck discovery can detect an already loaded deck.
        self._id = str(uuid.uuid4())

    def deck_type(self) -> "str | None":
        return self._deck_type
    def get_serial_number(self) -> str:
        return self.serial_number
    def key_layout(self) -> "list[int]":
        return cast("list[int]", self._key_layout)
    def is_open(self) -> bool:
        return True
    def reset(self) -> None:
        return
    def key_count(self) -> int:
        return self.key_layout()[0] * self.key_layout()[1]
    def set_key_callback(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_dial_callback(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_touchscreen_callback(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_brightness(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_key_image(self, *args: Any, **kwargs: Any) -> None:
        return
    def key_states(self) -> "list[bool]":
        # The grid keys and the touch buttons, in the driver's own order. A
        # Neo reports its two touch buttons through the key states as well.
        return [False] * (self.key_count() + self.model.touch_key_count)
    def key_image_format(self) -> "dict[str, Any]":
        return self.model.key_format.as_dict()
    def id(self) -> str:
        return self._id
    def connected(self) -> bool:
        return True
    def __enter__(self) -> "FakeDeck":
        return self
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return True
    
    def set_key_layout(self, layout: list[int]) -> None:
        """
        Sets and saves a new key layout
        """
        self._key_layout = layout

        settings = gl.settings_manager.get_deck_settings(self.serial_number)
        settings["key-layout"] = layout
        gl.settings_manager.save_deck_settings(self.serial_number, settings)

    def open(self, *args: Any, **kwargs: Any) -> None:
        return
    
    def close(self) -> None:
        return
    
    def is_visual(self) -> bool:
        return self.model.is_visual

    def is_touch(self) -> bool:
        return self.model.is_touch

    def dial_count(self) -> int:
        return self.model.dial_count

    def touchscreen_image_format(self) -> dict[str, Any]:
        return self.model.touchscreen_format.as_dict()

    def set_touchscreen_image(self, *args: Any, **kwargs: Any) -> None:
        return

    def set_key_color(self, *args: Any, **kwargs: Any) -> None:
        return

    def screen_image_format(self) -> dict[str, Any]:
        return self.model.screen_format.as_dict()

    def set_screen_image(self, *args: Any, **kwargs: Any) -> None:
        return

    def touch_key_count(self) -> int:
        return self.model.touch_key_count

    def get_firmware_version(self) -> str:
        return "fake-1.0"

    def vendor_id(self) -> int:
        return self.model.vendor_id

    def product_id(self) -> int:
        return self.model.product_id

    def set_poll_frequency(self, hz: float) -> None:
        return

    def dial_states(self) -> "list[bool]":
        return [False] * self.model.dial_count
