"""Pins the fake deck model presets: geometry, identity and the default shape.

A fake deck takes a model, which is the whole device shape: the key grid, the
image format of every surface, the dial and touch-button counts, and the
identity the USB layer reads. This scenario checks four things.

  (1) Every named preset reports what the driver's own device class reports for
      that model. The expected values come from the library classes themselves,
      so a preset that drifts from the hardware it names fails here.
  (2) No preset carries the Elgato vendor id, and no fake deck carries a
      transport. The USB reset acts on vendor plus product id and falls back to
      "the one device of this model on the bus", so a fake that claimed the
      Elgato identity would aim a real reset at real hardware on the same host.
      The liveness probe must also keep taking its fallback arm for a fake.
  (3) A real controller builds and loads a page over a Mini, an XL and an SD+,
      and its input registry matches each geometry.
  (4) The default shape is what it always was. A frozen literal snapshot pins
      every value a scenario can read off the deck, the input registry of a
      default controller is the 2x4 grid with four dials and one touchscreen
      that the suite assumes, and the key-event remapper still behaves exactly
      as the Neo dispatch scenario proves it does on this same shape.

No hardware is involved. Import fixtures first.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import dataclasses
import os
import re

import globals as gl

from StreamDeck.DeviceManager import USBProductIDs, USBVendorIDs
from StreamDeck.Devices.StreamDeckMini import StreamDeckMini
from StreamDeck.Devices.StreamDeckNeo import StreamDeckNeo
from StreamDeck.Devices.StreamDeckOriginal import StreamDeckOriginal
from StreamDeck.Devices.StreamDeckOriginalV2 import StreamDeckOriginalV2
from StreamDeck.Devices.StreamDeckPedal import StreamDeckPedal
from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus
from StreamDeck.Devices.StreamDeckXL import StreamDeckXL

import cli_args
from src.backend.DeckManagement import usb_reset
from src.backend.DeckManagement.BetterDeck import BetterDeck, device_is_on_bus
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.FakeDeck import (
    DEFAULT_FAKE_DECK_MODEL,
    FAKE_DECK_MODELS,
    FakeDeck,
    fake_deck_model,
)

# The device class each preset models, and the product id the USB layer reads
# off that model.
MODEL_SOURCES = {
    "original": (StreamDeckOriginal, USBProductIDs.USB_PID_STREAMDECK_ORIGINAL),
    "mk2": (StreamDeckOriginalV2, USBProductIDs.USB_PID_STREAMDECK_MK2),
    "mini": (StreamDeckMini, USBProductIDs.USB_PID_STREAMDECK_MINI),
    "xl": (StreamDeckXL, USBProductIDs.USB_PID_STREAMDECK_XL),
    "plus": (StreamDeckPlus, USBProductIDs.USB_PID_STREAMDECK_PLUS),
    "neo": (StreamDeckNeo, USBProductIDs.USB_PID_STREAMDECK_NEO),
    "pedal": (StreamDeckPedal, USBProductIDs.USB_PID_STREAMDECK_PEDAL),
}

# What a fake deck of the default shape has always answered. These are
# literals on purpose: the whole suite stands on this shape, and a change to it
# is a change to every scenario that never asked for a model.
DEFAULT_SURFACE = {
    "deck_type": None,
    "key_layout": [2, 4],
    "key_count": 8,
    "key_states_len": 8,
    "key_image_format": {"size": (72, 72), "format": "JPEG", "flip": (True, True),
                         "rotation": 0},
    "is_touch": True,
    "is_visual": True,
    "dial_count": 4,
    "dial_states_len": 4,
    "touchscreen_image_format": {"size": (800, 100), "format": "JPEG",
                                 "flip": (False, False), "rotation": 0},
    "screen_image_format": {"size": (0, 0), "format": "JPEG", "flip": (False, False),
                            "rotation": 0},
    "touch_key_count": 0,
    "vendor_id": 0,
    "product_id": 0,
    "firmware": "fake-1.0",
    "is_open": True,
    "connected": True,
    "is_fake": True,
}

ROTATIONS = (0, 90, 180, 270)


def _surface(cls, prefix: str) -> "dict":
    """The image-format dict a device class reports for one of its surfaces."""
    return {
        "size": (getattr(cls, f"{prefix}_PIXEL_WIDTH"), getattr(cls, f"{prefix}_PIXEL_HEIGHT")),
        "format": getattr(cls, f"{prefix}_IMAGE_FORMAT"),
        "flip": getattr(cls, f"{prefix}_FLIP"),
        "rotation": getattr(cls, f"{prefix}_ROTATION"),
    }


def check_presets_match_the_hardware() -> None:
    """Every named preset answers what its device class answers."""
    assert set(MODEL_SOURCES) | {"default"} == set(FAKE_DECK_MODELS), (
        f"the presets are {sorted(FAKE_DECK_MODELS)} but this scenario knows "
        f"{sorted(set(MODEL_SOURCES) | {'default'})}; add the new preset here with the "
        f"device class it models")

    for name, (cls, product_id) in MODEL_SOURCES.items():
        deck = FakeDeck(serial_number=f"model-{name}", model=name)

        assert tuple(deck.key_layout()) == (cls.KEY_ROWS, cls.KEY_COLS), (
            f"{name}: key layout {deck.key_layout()}, expected "
            f"{(cls.KEY_ROWS, cls.KEY_COLS)}")
        assert deck.key_count() == cls.KEY_COUNT, (
            f"{name}: key count {deck.key_count()}, expected {cls.KEY_COUNT}")
        assert len(deck.key_states()) == cls.KEY_COUNT + cls.TOUCH_KEY_COUNT, (
            f"{name}: {len(deck.key_states())} key states, expected "
            f"{cls.KEY_COUNT + cls.TOUCH_KEY_COUNT} (the grid keys plus the touch buttons)")
        assert deck.touch_key_count() == cls.TOUCH_KEY_COUNT, (
            f"{name}: touch key count {deck.touch_key_count()}, expected "
            f"{cls.TOUCH_KEY_COUNT}")

        assert deck.key_image_format() == _surface(cls, "KEY"), (
            f"{name}: key image format {deck.key_image_format()}, expected "
            f"{_surface(cls, 'KEY')}")
        assert deck.touchscreen_image_format() == _surface(cls, "TOUCHSCREEN"), (
            f"{name}: touchscreen format {deck.touchscreen_image_format()}, expected "
            f"{_surface(cls, 'TOUCHSCREEN')}")
        assert deck.screen_image_format() == _surface(cls, "SCREEN"), (
            f"{name}: screen format {deck.screen_image_format()}, expected "
            f"{_surface(cls, 'SCREEN')}")

        assert deck.dial_count() == cls.DIAL_COUNT, (
            f"{name}: dial count {deck.dial_count()}, expected {cls.DIAL_COUNT}")
        assert len(deck.dial_states()) == cls.DIAL_COUNT, (
            f"{name}: {len(deck.dial_states())} dial states, expected {cls.DIAL_COUNT}")
        assert deck.is_touch() is cls.DECK_TOUCH, (
            f"{name}: is_touch() {deck.is_touch()!r}, expected {cls.DECK_TOUCH!r}")
        assert deck.is_visual() is cls.DECK_VISUAL, (
            f"{name}: is_visual() {deck.is_visual()!r}, expected {cls.DECK_VISUAL!r}")
        assert deck.deck_type() == cls.DECK_TYPE, (
            f"{name}: deck type {deck.deck_type()!r}, expected {cls.DECK_TYPE!r}")
        assert deck.product_id() == product_id, (
            f"{name}: product id {deck.product_id():#06x}, expected {product_id:#06x}")

    # The grid geometry the presets exist for is genuinely varied: no two of
    # these three share a key count, so a scenario that runs over all of them
    # cannot pass by accident on one hardcoded grid.
    counts = {name: FakeDeck(serial_number=f"grid-{name}", model=name).key_count()
              for name in ("mini", "xl", "plus")}
    assert len(set(counts.values())) == 3, f"expected three distinct key counts, got {counts}"
    print("PASS: every preset reports the geometry and identity of the device it models")


def check_no_preset_claims_the_elgato_identity() -> None:
    """A fake deck never steers the USB reset, and never leaves the liveness
    fallback arm."""
    assert usb_reset.ELGATO_VENDOR_ID == USBVendorIDs.USB_VID_ELGATO, (
        f"the vendor id the reset guards, {usb_reset.ELGATO_VENDOR_ID:#06x}, is not the one "
        f"the driver knows, {USBVendorIDs.USB_VID_ELGATO:#06x}")

    for name in FAKE_DECK_MODELS:
        deck = FakeDeck(serial_number=f"identity-{name}", model=name)

        assert deck.vendor_id() != usb_reset.ELGATO_VENDOR_ID, (
            f"{name}: the preset reports the Elgato vendor id. The USB reset would then "
            f"accept this fake and, with no serial match, reset the one real deck of this "
            f"model on the bus")
        assert deck.vendor_id() == 0, (
            f"{name}: vendor id {deck.vendor_id()!r}, expected 0")
        assert usb_reset.reset_wedged_deck(deck) is None, (
            f"{name}: a fake deck reached the USB reset")

        # device_is_on_bus reads a transport off the deck. A fake carries none,
        # so the probe must fall back to the deck's own connected().
        assert getattr(deck, "device", None) is None, (
            f"{name}: a fake deck grew a transport, which puts it on the filtered "
            f"enumeration arm of the liveness probe")
        assert device_is_on_bus(deck) is deck.connected() is True, (
            f"{name}: liveness probe answered {device_is_on_bus(deck)!r}")

    print("PASS: no preset claims the Elgato identity, and every fake stays on the "
          "liveness fallback arm")


def check_model_selection_rules() -> None:
    """What a caller asks for beats what an earlier session persisted."""
    serial = "layout-persisted"
    gl.settings_manager.save_deck_settings(serial, {"key-layout": [3, 5]})

    plain = FakeDeck(serial_number=serial)
    assert plain.key_layout() == [3, 5], (
        f"with no model named, the persisted layout must still win; got {plain.key_layout()}")

    with_model = FakeDeck(serial_number=serial, model="xl")
    assert with_model.key_layout() == [4, 8], (
        f"a named model must outrank the persisted layout; got {with_model.key_layout()}")

    layout_arg = FakeDeck(serial_number="layout-arg", model="xl", key_layout=[1, 2])
    assert layout_arg.key_layout() == [1, 2], (
        f"an explicit key layout must outrank the model's grid; got {layout_arg.key_layout()}")

    named = FakeDeck(serial_number="type-arg", model="xl", deck_type="Fake Deck")
    assert named.deck_type() == "Fake Deck", (
        f"an explicit deck type must outrank the model's; got {named.deck_type()!r}")

    # A model of a caller's own, for a shape no hardware carries.
    odd = dataclasses.replace(FAKE_DECK_MODELS["xl"], name="xl-two-dials", dial_count=2)
    custom = FakeDeck(serial_number="custom-model", model=odd)
    assert custom.dial_count() == 2 and custom.key_count() == 32, (
        f"a caller's own model must be taken as given; got {custom.dial_count()} dials and "
        f"{custom.key_count()} keys")

    try:
        FakeDeck(serial_number="unknown-model", model="stream-deck-4000")
    except ValueError as e:
        assert "stream-deck-4000" in str(e) and "xl" in str(e), (
            f"the refusal must name the model and list the presets; got {e}")
    else:
        raise AssertionError("an unknown model name was accepted")

    assert fake_deck_model(None) is DEFAULT_FAKE_DECK_MODEL, (
        "no model named must resolve to the default shape")
    assert fake_deck_model("  XL  ") is FAKE_DECK_MODELS["xl"], (
        "a preset name must resolve whatever its spacing and case")

    # The flag's own help text lists the presets, and cli_args cannot import
    # the table (it stays importable before globals). Pin the two together.
    flag = [a for a in cli_args.argparser._actions if "--fake-deck-model" in a.option_strings]
    assert len(flag) == 1, "the --fake-deck-model flag is gone"
    listed = re.search(r"Shape of a fake deck: (.+?)\.", flag[0].help or "")
    assert listed is not None, f"cannot read the preset list out of the help: {flag[0].help!r}"
    names = {n for n in re.split(r"[,\s]+", listed.group(1)) if n and n != "or"}
    assert names == set(FAKE_DECK_MODELS), (
        f"the flag's help lists {sorted(names)}, the presets are {sorted(FAKE_DECK_MODELS)}")

    print("PASS: model selection, the caller's own model, the refusal and the flag help "
          "all hold")


def _settle_keys(controller, deck, page, key_count: int) -> None:
    """Load one page and wait for every key of the grid to reach the device."""
    deck.clear_journal()
    controller.load_page(page, allow_reload=True)

    def settled():
        return all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count))

    assert fixtures.wait_until(settled, timeout=15), (
        f"only {sum(deck.last_op_for(f'key:{k}') is not None for k in range(key_count))} of "
        f"{key_count} keys painted")


def check_controller_over_each_geometry() -> None:
    """A real controller builds its input registry and paints a page at each
    geometry."""
    media = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "model_bg.png"),
                                   color=(12, 200, 90))

    for name in ("mini", "xl", "plus"):
        model = FAKE_DECK_MODELS[name]
        rows, cols = model.key_layout
        controller = fixtures.make_headless_controller(serial=f"model-ctl-{name}", model=name)
        try:
            deck = fixtures.raw_deck(controller)
            assert controller.deck.key_count() == rows * cols, (
                f"{name}: the controller sees {controller.deck.key_count()} keys, expected "
                f"{rows * cols}")

            keys = controller.inputs[Input.Key]
            dials = controller.inputs[Input.Dial]
            touchscreens = controller.inputs[Input.Touchscreen]
            assert len(keys) == rows * cols, (
                f"{name}: {len(keys)} key inputs, expected {rows * cols}")
            assert {str(k.identifier.json_identifier) for k in keys} == {
                f"{x}x{y}" for y in range(rows) for x in range(cols)}, (
                f"{name}: the key inputs do not cover the {rows}x{cols} grid: "
                f"{sorted(str(k.identifier.json_identifier) for k in keys)}")
            assert len(dials) == model.dial_count, (
                f"{name}: {len(dials)} dial inputs, expected {model.dial_count}")
            assert len(touchscreens) == (1 if model.is_touch else 0), (
                f"{name}: {len(touchscreens)} touchscreen inputs, expected "
                f"{1 if model.is_touch else 0}")
            assert controller.get_input(Input.Key(f"{cols - 1}x{rows - 1}")) is not None, (
                f"{name}: the last key of the grid resolves to no input")

            page = gl.page_manager.get_page(
                fixtures.seed_page_with_background(f"ModelPage{name}", media), controller)
            _settle_keys(controller, deck, page, rows * cols)

            painted = {e[3] for e in deck.ops_by_name("set_key_image")}
            assert painted == {f"key:{k}" for k in range(rows * cols)}, (
                f"{name}: the page painted {sorted(painted)}, expected every key of the grid")
            assert controller.deck.key_image_format()["size"] == model.key_image.size, (
                f"{name}: the encoder was handed {controller.deck.key_image_format()['size']}, "
                f"expected {model.key_image.size}")
        finally:
            fixtures.teardown(controller)

    print("PASS: a controller builds and paints a page over a Mini, an XL and an SD+")


def check_default_shape_is_unchanged() -> None:
    """The default shape, value by value, and the remapper on top of it."""
    deck = FakeDeck(serial_number="golden-default")

    got = {
        "deck_type": deck.deck_type(),
        "key_layout": deck.key_layout(),
        "key_count": deck.key_count(),
        "key_states_len": len(deck.key_states()),
        "key_image_format": deck.key_image_format(),
        "is_touch": deck.is_touch(),
        "is_visual": deck.is_visual(),
        "dial_count": deck.dial_count(),
        "dial_states_len": len(deck.dial_states()),
        "touchscreen_image_format": deck.touchscreen_image_format(),
        "screen_image_format": deck.screen_image_format(),
        "touch_key_count": deck.touch_key_count(),
        "vendor_id": deck.vendor_id(),
        "product_id": deck.product_id(),
        "firmware": deck.get_firmware_version(),
        "is_open": deck.is_open(),
        "connected": deck.connected(),
        "is_fake": deck.is_fake,
    }
    assert got == DEFAULT_SURFACE, (
        "the default fake deck changed shape. Every scenario that names no model stands on "
        "this table.\n"
        + "\n".join(f"  {k}: {got[k]!r}, expected {DEFAULT_SURFACE[k]!r}"
                    for k in DEFAULT_SURFACE if got[k] != DEFAULT_SURFACE[k]))
    assert deck.key_states() == [False] * 8 and deck.dial_states() == [False] * 4, (
        "the default deck must report every key and dial released")

    controller = fixtures.make_headless_controller(serial="golden-ctl")
    try:
        assert len(controller.inputs[Input.Key]) == 8, (
            f"{len(controller.inputs[Input.Key])} key inputs on the default deck, expected 8")
        assert len(controller.inputs[Input.Dial]) == 4, (
            f"{len(controller.inputs[Input.Dial])} dial inputs on the default deck, expected 4")
        assert len(controller.inputs[Input.Touchscreen]) == 1, (
            f"{len(controller.inputs[Input.Touchscreen])} touchscreen inputs on the default "
            f"deck, expected 1")
    finally:
        fixtures.teardown(controller)

    # The key-event remapper, on the default shape and with nothing patched.
    # The Neo dispatch scenario proves these two properties over a grid it pins
    # by hand; the default deck already carries that grid, so the same
    # assertions must hold over it untouched.
    faulty = fixtures.FaultyFakeDeck(serial_number="golden-remap")
    better = BetterDeck(faulty)
    dispatched: "list[int]" = []
    better.set_key_callback(lambda _deck, key, _state: dispatched.append(key))
    grid_total = faulty.key_count()
    for rotation in ROTATIONS:
        better.set_rotation(rotation)
        for physical in range(grid_total):
            dispatched.clear()
            faulty.fire_key_event(physical, True)
            expected = better.get_logical_index(physical)
            assert dispatched == [expected], (
                f"rotation {rotation} physical {physical} dispatched {dispatched}, expected "
                f"[{expected}]")
            assert expected is not None and 0 <= expected < grid_total, (
                f"rotation {rotation} physical {physical} maps to out-of-grid {expected}")
        dispatched.clear()
        for physical in range(grid_total):
            faulty.fire_key_event(physical, True)
        assert sorted(dispatched) == list(range(grid_total)), (
            f"rotation {rotation}: the grid keys are not a bijection: {sorted(dispatched)}")
        for physical in (grid_total, grid_total + 1):
            dispatched.clear()
            faulty.fire_key_event(physical, True)
            assert not dispatched, (
                f"rotation {rotation}: index {physical} past the grid dispatched "
                f"{dispatched}")

    print("PASS: the default shape, the default input registry and the key remapper are "
          "unchanged")


def main() -> None:
    fixtures.start_watchdog(80, label="scenario_fake_deck_models")
    fixtures._install_integration_globals()

    check_presets_match_the_hardware()
    check_no_preset_claims_the_elgato_identity()
    check_model_selection_rules()
    check_default_shape_is_unchanged()
    check_controller_over_each_geometry()

    print("PASS: scenario_fake_deck_models")


if __name__ == "__main__":
    main()
