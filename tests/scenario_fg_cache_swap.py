"""Check foreground-cache invalidation after an in-place source-image swap."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import types

from PIL import Image

from fixtures import start_watchdog

from src.backend.DeckManagement.DeckController import LayoutManager
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout


RED = (200, 30, 30, 255)
GREEN = (30, 200, 30, 255)


class _FakeAsset:
    """Provide a stable identity whose backing image can change."""

    def __init__(self, image: Image.Image):
        self.image = image


def _make_layout_manager() -> LayoutManager:
    # Supply a complete layout so composition does not use the input identifier.
    controller_input = types.SimpleNamespace(identifier=None)
    lm = LayoutManager(controller_input)
    lm.action_layout = ImageLayout(valign=0, halign=0, fill_mode="stretch", size=1.0)
    return lm


def _dominant(img: Image.Image) -> tuple:
    """The single solid colour of a flat image, read at the centre pixel."""
    return img.convert("RGBA").getpixel((img.width // 2, img.height // 2))


def main() -> int:
    start_watchdog(30, "fg_cache_swap")

    lm = _make_layout_manager()
    background = Image.new("RGBA", (72, 72), (0, 0, 0, 0))

    # Equal source sizes isolate image identity as the only changed cache input.
    red_src = Image.new("RGBA", (144, 144), RED)
    green_src = Image.new("RGBA", (144, 144), GREEN)

    asset = _FakeAsset(red_src)

    # Populate the cache with the red source.
    out1 = lm.add_image_to_background(asset.image, background, cache_token=asset)
    if _dominant(out1) != RED:
        print(f"FAIL(setup): first composite is not RED: {_dominant(out1)}")
        return 1

    # Swap pixels while keeping asset identity, layout, and source size unchanged.
    asset.image = green_src
    out2 = lm.add_image_to_background(asset.image, background, cache_token=asset)

    got = _dominant(out2)
    if got != GREEN:
        print(f"FAIL: composite after an in-place image swap served the stale "
              f"cached foreground: got {got}, expected GREEN {GREEN} -- "
              f"_fg_cache keyed only on asset+layout, not the backing image")
        return 1

    print("PASS: _fg_cache invalidates on an in-place source-image swap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
