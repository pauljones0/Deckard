"""Check flow-box navigation before and after the first displayed range."""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from src.windows.AssetManager.DynamicFlowBox import DynamicFlowBox


class Child(Gtk.FlowBoxChild):
    """A pool child that records the item the factory bound onto it."""

    def __init__(self):
        super().__init__()
        self.item = None


def factory(child: Child, item: int) -> None:
    child.item = item


def pump(seconds: float = 1.0) -> None:
    """Run the main loop's pending work, so the idle rebind lands."""
    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        if not context.pending():
            time.sleep(0.01)
            continue
        while context.pending():
            context.iteration(False)


def visible_items(box: DynamicFlowBox) -> list:
    found = []
    index = 0
    while True:
        child = box.flow_box.get_child_at_index(index)
        if child is None:
            return found
        if child.get_visible():
            found.append(child.item)
        index += 1


def check_offset_exists() -> None:
    box: DynamicFlowBox = DynamicFlowBox(Child)
    assert box.current_start_index == 0, (
        "a fresh flow box must report a first-page offset before any range "
        f"has run, it reports {box.current_start_index!r}")
    print("PASS: a fresh flow box carries a first-page offset")


def check_nav_before_first_range() -> None:
    box: DynamicFlowBox = DynamicFlowBox(Child)

    # Programmatic emission reaches handlers even while buttons are insensitive.
    box.next_button.emit("clicked")
    box.back_button.emit("clicked")
    box.on_next()
    box.on_back()
    pump()

    assert box.current_start_index == 0, (
        "a nav click before the first range must leave the offset on the "
        f"first page, it moved to {box.current_start_index}")
    # Placeholders stay unbound until the first range runs the factory.
    assert visible_items(box) == [None] * box.N_ITEMS_PER_PAGE, (
        "a nav click before the first range must leave every placeholder "
        f"unbound, the pool shows {visible_items(box)}")
    print("PASS: the nav handlers do nothing on a box that has shown no range")


def check_buttons_start_insensitive() -> None:
    box: DynamicFlowBox = DynamicFlowBox(Child)
    assert not box.next_button.get_sensitive(), (
        "the Next button must be insensitive until a range makes it "
        "meaningful")
    assert not box.back_button.get_sensitive(), (
        "the Back button must be insensitive until a range makes it "
        "meaningful")
    print("PASS: both nav buttons start insensitive")


def check_nav_pages_a_loaded_box() -> None:
    box: DynamicFlowBox = DynamicFlowBox(Child)
    items = list(range(box.N_ITEMS_PER_PAGE * 2))
    box.set_item_list(items)
    box.set_factory(factory)

    box.show_range(0, box.N_ITEMS_PER_PAGE)
    pump()
    assert visible_items(box) == items[:box.N_ITEMS_PER_PAGE], (
        "the first range did not fill the pool, so the paging below would "
        "prove nothing")
    assert box.next_button.get_sensitive(), (
        "the Next button must turn sensitive with a second page waiting")
    assert not box.back_button.get_sensitive(), (
        "the Back button must stay insensitive on the first page")

    box.next_button.emit("clicked")
    pump()
    assert visible_items(box) == items[box.N_ITEMS_PER_PAGE:], (
        f"Next must show the second page, it shows {visible_items(box)}")
    assert box.current_start_index == box.N_ITEMS_PER_PAGE, (
        f"Next must move the offset to {box.N_ITEMS_PER_PAGE}, it reports "
        f"{box.current_start_index}")

    # Past the end. The guard has to refuse it, or the pool blanks.
    box.on_next()
    pump()
    assert visible_items(box) == items[box.N_ITEMS_PER_PAGE:], (
        "a step past the last page must leave the last page showing, it "
        f"shows {visible_items(box)}")

    box.back_button.emit("clicked")
    pump()
    assert visible_items(box) == items[:box.N_ITEMS_PER_PAGE], (
        f"Back must return to the first page, it shows {visible_items(box)}")

    # Before the start. The clamp has to refuse it.
    box.on_back()
    pump()
    assert visible_items(box) == items[:box.N_ITEMS_PER_PAGE], (
        "a step before the first page must leave the first page showing, it "
        f"shows {visible_items(box)}")
    assert box.current_start_index == 0, (
        f"the offset must stay on the first page, it reports "
        f"{box.current_start_index}")
    print("PASS: the nav still pages a loaded box and refuses both ends")


def main() -> int:
    fixtures.start_watchdog(120, label="scenario_flow_box_nav")

    if not fixtures.has_usable_display():
        print("SKIP: no display; DynamicFlowBox builds real GTK widgets")
        return 0

    Adw.init()
    check_offset_exists()
    check_nav_before_first_range()
    check_buttons_start_insensitive()
    check_nav_pages_a_loaded_box()
    print("ALL PASS: scenario_flow_box_nav")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
