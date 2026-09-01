"""Check deck-name fallbacks, duplicate suffixes, and live-title updates.
Most stack checks use unbound methods over display-free stand-ins.
"""
import fixtures  # noqa: F401  (must be first: see fixtures.py docstring)

import os  # noqa: E402

import globals as gl  # noqa: E402

from locales.LocaleManager import LocaleManager  # noqa: E402
from src.backend.settings_store import (  # noqa: E402
    DECK_NAME_MAX_LENGTH,
    UNNAMED_DECK,
    DeckSettings,
)
from src.windows.mainWindow.elements.DeckSettings.DeckGroup import DeckName  # noqa: E402
from src.windows.mainWindow.elements.DeckStack import DeckStack  # noqa: E402


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(REPO_ROOT, "locales", "locales.csv")

NAME_KEY = "deck.deck-group.name"


class _FakeDeck:
    """Return a model and current device serial, with optional read failures.
    The device serial defaults to the controller's memoized serial.
    """

    def __init__(self, deck_type, device_serial, raises=False, serial_raises=False):
        self._deck_type = deck_type
        self._device_serial = device_serial
        self._raises = raises
        self._serial_raises = serial_raises

    def deck_type(self):
        if self._raises:
            raise OSError("the device went away")
        return self._deck_type

    def get_serial_number(self):
        if self._serial_raises:
            raise OSError("no device serial")
        return self._device_serial


class _FakeController:
    """A deck controller as the stack reads one: a handle and a serial."""

    def __init__(self, serial, deck_type="Stream Deck MK.2", deck_raises=False,
                 serial_raises=False, device_serial=None, device_serial_raises=False):
        self.deck = _FakeDeck(
            deck_type, device_serial if device_serial is not None else serial,
            raises=deck_raises, serial_raises=device_serial_raises)
        self._serial = serial
        self._serial_raises = serial_raises

    def serial_number(self):
        if self._serial_raises:
            raise OSError("no serial")
        return self._serial


class _FakeStackPage:
    """A Gtk.StackPage as refresh_page_title uses one."""

    def __init__(self, title: str):
        self.title = title

    def set_title(self, title: str) -> None:
        self.title = title


class _FakeStack:
    """Run real DeckStack methods over the plain state that they read."""

    base_title = DeckStack.base_title
    _settings_serial = DeckStack._settings_serial
    unique_title = DeckStack.unique_title
    get_page_attributes = DeckStack.get_page_attributes
    refresh_page_title = DeckStack.refresh_page_title

    def __init__(self):
        self.deck_names: list[str] = []
        self.deck_numbers: list[str] = []
        self.deck_attributes: dict = {}
        #: stack-child name -> the child object, as Gtk.Stack holds them.
        self.children: dict[str, object] = {}
        #: child object -> its page.
        self.pages: dict[int, _FakeStackPage] = {}

    def add_child(self, name: str, title: str) -> _FakeStackPage:
        child = object()
        self.children[name] = child
        page = _FakeStackPage(title)
        self.pages[id(child)] = page
        return page

    def get_child_by_name(self, name: str):
        return self.children.get(name)

    def get_page(self, child):
        return self.pages[id(child)]


def name_deck(serial: str, name: str) -> None:
    settings = gl.settings_manager.deck(serial)
    settings.set_top_level_value("name", name)
    settings.save()


def check_display_name_always_returns_text() -> None:
    """Require every input to resolve to a non-empty switcher label."""
    empties = (None, "", "   ", 0, [], {})
    for stored in empties:
        for model in (None, "", "  ", 17):
            for serial in (None, "", "  "):
                data = {} if stored is None else {"name": stored}
                got = DeckSettings(data, serial).display_name(model)
                assert isinstance(got, str), (
                    f"display_name({stored!r}, {model!r}, serial={serial!r}) gave "
                    f"{got!r} ({type(got).__name__}), and the caller has no label"
                )
                assert got.strip(), (
                    f"display_name({stored!r}, {model!r}, serial={serial!r}) gave an "
                    f"empty label"
                )
                assert got == UNNAMED_DECK, (
                    f"with nothing to read, the label must be {UNNAMED_DECK!r}, got {got!r}"
                )
    print("PASS: display_name answers a non-empty string for every input")


def check_display_name_precedence() -> None:
    """The chosen name wins, then the model name, then the serial."""
    assert DeckSettings({"name": "Studio"}, "S1").display_name("Stream Deck MK.2") == "Studio"
    assert DeckSettings({}, "S1").display_name("Stream Deck MK.2") == "Stream Deck MK.2"
    assert DeckSettings({}, "S1").display_name(None) == "S1"
    assert DeckSettings({"name": ""}, "S1").display_name("Stream Deck MK.2") == "Stream Deck MK.2", (
        "an empty chosen name means no chosen name, so the model name shows"
    )
    assert DeckSettings({"name": "   "}, "S1").display_name("Stream Deck MK.2") == "Stream Deck MK.2", (
        "a name of spaces is an empty label and must fall back too"
    )
    print("PASS: the chosen name, then the model name, then the serial")


def check_display_name_trims_and_caps() -> None:
    view = DeckSettings({"name": "  Studio  "}, "S1")
    assert view.display_name("Stream Deck MK.2") == "Studio", (
        "surrounding space must go, or the switcher label carries it"
    )
    long_name = "x" * (DECK_NAME_MAX_LENGTH + 40)
    got = DeckSettings({"name": long_name}, "S1").display_name(None)
    assert len(got) == DECK_NAME_MAX_LENGTH, (
        f"a hand-edited name of {len(long_name)} characters must be cut to "
        f"{DECK_NAME_MAX_LENGTH}, got {len(got)}"
    )
    print("PASS: the label is trimmed and cut to the cap")


def check_name_cap_excludes_fallbacks() -> None:
    """Apply the length cap only to user names, not device-provided fallbacks."""
    long_model = "Fake Deck 1 (Stream Deck Original)"
    assert len(long_model) > DECK_NAME_MAX_LENGTH, "pick a model name past the cap"
    got = DeckSettings({}, "S1").display_name(long_model)
    assert got == long_model, (
        f"a long model name must reach the switcher whole, got {got!r}"
    )
    long_serial = "S" * (DECK_NAME_MAX_LENGTH + 10)
    assert DeckSettings({}, long_serial).display_name(None) == long_serial, (
        "a long serial fallback must not be cut either"
    )
    print("PASS: the cap cuts the chosen name only, not the model or the serial")


def check_reading_the_name_writes_nothing() -> None:
    data: dict = {}
    DeckSettings(data, "S1").display_name("Stream Deck MK.2")
    assert data == {}, f"resolving a label persisted a name nobody chose: {data}"
    print("PASS: resolving a label writes no name into the settings")


def check_base_title_reads_the_settings() -> None:
    stack = _FakeStack()
    name_deck("base-1", "Studio")
    assert stack.base_title(_FakeController("base-1"), "base-1") == "Studio"
    assert stack.base_title(_FakeController("base-2"), "base-2") == "Stream Deck MK.2", (
        "a deck with no chosen name must show its model name"
    )
    print("PASS: the base title reads the chosen name and falls back to the model")


def check_base_title_uses_live_serial() -> None:
    """Read the name by current device serial, with the memoized serial as fallback.
    The two serials can differ during boot under USB contention.
    """
    stack = _FakeStack()
    # The device now reports "fresh-1"; the memoized stack serial is "cached-1".
    name_deck("fresh-1", "Studio")
    controller = _FakeController("cached-1", device_serial="fresh-1")
    got = stack.base_title(controller, "cached-1")
    assert got == "Studio", (
        f"base_title must read the name under the fresh device serial, got {got!r}"
    )
    # No name under the memoized serial, so a reader keyed on it sees none.
    assert DeckSettings(gl.settings_manager.get_deck_settings("cached-1"), "cached-1").get("name") == "", (
        "the memoized serial must hold no name, or this test proves nothing"
    )

    # When the fresh read fails, the memoized serial is the only key there is.
    name_deck("cached-2", "Fallback")
    controller = _FakeController("cached-2", device_serial_raises=True)
    assert stack.base_title(controller, "cached-2") == "Fallback", (
        "a failed device-serial read must fall back to the memoized serial key"
    )
    print("PASS: the title reads the name under the fresh device serial")


def check_base_title_handles_dead_device() -> None:
    """A model read that raises still gives a title, and never None."""
    stack = _FakeStack()
    got = stack.base_title(_FakeController("dead-1", deck_raises=True), "dead-1")
    assert got == "dead-1", (
        f"a deck whose model read raised must fall back to its serial, got {got!r}"
    )
    name_deck("dead-2", "Studio")
    got = stack.base_title(_FakeController("dead-2", deck_raises=True), "dead-2")
    assert got == "Studio", "a chosen name needs no device read at all"
    got = stack.base_title(_FakeController("dead-3", deck_type=None), "dead-3")
    assert got == "dead-3", (
        f"a deck whose model name is None must fall back to its serial, got {got!r}"
    )
    print("PASS: a dead or nameless device still resolves to a title")


def check_chosen_name_gets_duplicate_suffix() -> None:
    """Two decks under one chosen name read "Name" and "Name (2)"."""
    stack = _FakeStack()
    for serial in ("dup-1", "dup-2", "dup-3"):
        name_deck(serial, "Studio")

    titles = []
    for serial in ("dup-1", "dup-2", "dup-3"):
        attr = stack.get_page_attributes(_FakeController(serial))
        assert attr is not None, f"{serial} got no attributes"
        titles.append(attr[1])

    assert titles == ["Studio", "Studio (2)", "Studio (3)"], (
        f"the duplicate suffix must apply to a chosen name, got {titles}"
    )
    print("PASS: identically named decks read Studio, Studio (2), Studio (3)")


def check_model_digits_survive_duplicate_suffix() -> None:
    stack = _FakeStack()
    titles = [
        stack.get_page_attributes(_FakeController(f"mk-{i}"))[1]
        for i in range(3)
    ]
    assert titles == ["Stream Deck MK.2", "Stream Deck MK.2 (2)", "Stream Deck MK.2 (3)"], (
        f"the suffix must go after the whole model name, never into it, got {titles}"
    )
    print("PASS: a second MK.2 reads MK.2 (2) and never MK.3")


def check_title_namespace_collisions() -> None:
    """A chosen name equal to another deck's model name still gets a suffix."""
    stack = _FakeStack()
    name_deck("mix-2", "Stream Deck MK.2")
    first = stack.get_page_attributes(_FakeController("mix-1"))[1]
    second = stack.get_page_attributes(_FakeController("mix-2", deck_type="Stream Deck XL"))[1]
    assert (first, second) == ("Stream Deck MK.2", "Stream Deck MK.2 (2)"), (
        f"a chosen name must share the one taken list with the model names, got "
        f"{(first, second)}"
    )
    print("PASS: a chosen name and a model name share one taken list")


def check_attributes_are_cached_per_controller() -> None:
    stack = _FakeStack()
    controller = _FakeController("cache-1")
    first = stack.get_page_attributes(controller)
    second = stack.get_page_attributes(controller)
    assert first == second, f"one controller took two titles: {first} then {second}"
    assert stack.deck_names == ["Stream Deck MK.2"], (
        f"a repeat read took a second title from the list: {stack.deck_names}"
    )
    print("PASS: a controller keeps the title it was given")


def check_titles_are_nonempty_strings() -> None:
    """The tuple's title is a string, whatever the device answers."""
    stack = _FakeStack()
    cases = (
        _FakeController("t-1", deck_type=None),
        _FakeController("t-2", deck_type=""),
        _FakeController("t-3", deck_raises=True),
        _FakeController("t-4"),
    )
    for controller in cases:
        attr = stack.get_page_attributes(controller)
        assert attr is not None, "a readable serial must give attributes"
        _number, title = attr
        assert isinstance(title, str) and title.strip(), (
            f"{controller._serial}: the stack title was {title!r}"
        )
    print("PASS: every stack title is a non-empty string")


def check_only_unreadable_serial_returns_none() -> None:
    """Return no attributes only when the stack-child serial is unreadable."""
    stack = _FakeStack()
    assert stack.get_page_attributes(_FakeController("x", serial_raises=True)) is None
    assert stack.deck_names == [], "a deck that got no attributes took a title"
    assert stack.deck_numbers == [], "a deck that got no attributes took a number"
    print("PASS: an unreadable serial gives no attributes and takes nothing")


def check_rename_retitles_the_live_child() -> None:
    stack = _FakeStack()
    controller = _FakeController("live-1")
    _number, title = stack.get_page_attributes(controller)
    page = stack.add_child("live-1", title)

    name_deck("live-1", "Studio")
    stack.refresh_page_title(controller)

    assert page.title == "Studio", f"the live child kept the title {page.title!r}"
    assert stack.deck_attributes[controller] == ("live-1", "Studio"), (
        f"the recorded attributes still hold {stack.deck_attributes[controller]}"
    )
    assert stack.deck_names == ["Studio"], (
        f"the old title stayed in the taken list: {stack.deck_names}"
    )
    print("PASS: a rename retitles the live child and frees the old title")


def check_repeated_renames_gain_no_suffix() -> None:
    """A deck must not collide with the title it is giving up."""
    stack = _FakeStack()
    controller = _FakeController("loop-1")
    _number, title = stack.get_page_attributes(controller)
    page = stack.add_child("loop-1", title)

    for chosen in ("A", "B", "A", "C"):
        name_deck("loop-1", chosen)
        stack.refresh_page_title(controller)
        assert page.title == chosen, (
            f"after {chosen!r} the child reads {page.title!r} -- the deck collided "
            f"with a title it had already given up"
        )
        assert stack.deck_names == [chosen], (
            f"the taken list grew across renames: {stack.deck_names}"
        )
    print("PASS: repeated renames gain no suffix")


def check_taken_name_rename_adds_suffix() -> None:
    stack = _FakeStack()
    first = _FakeController("two-1")
    second = _FakeController("two-2", deck_type="Stream Deck XL")
    name_deck("two-1", "Studio")
    stack.get_page_attributes(first)
    _number, title = stack.get_page_attributes(second)
    page = stack.add_child("two-2", title)

    name_deck("two-2", "Studio")
    stack.refresh_page_title(second)

    assert page.title == "Studio (2)", (
        f"a rename onto a taken title must take a suffix, got {page.title!r}"
    )
    assert stack.deck_attributes[first] == ("two-1", "Studio"), (
        "the deck that held the title must keep it"
    )
    print("PASS: a rename onto a taken title takes the suffix")


def check_absent_child_rename_records_title() -> None:
    """No live child costs the retitle and nothing else."""
    stack = _FakeStack()
    controller = _FakeController("absent-1")
    stack.get_page_attributes(controller)
    name_deck("absent-1", "Studio")
    stack.refresh_page_title(controller)
    assert stack.deck_attributes[controller] == ("absent-1", "Studio"), (
        "the new title must be recorded even with no child to set it on"
    )
    print("PASS: a rename with no live child still records the title")


def check_unknown_controller_rename_is_noop() -> None:
    stack = _FakeStack()
    stack.refresh_page_title(_FakeController("unknown-1"))
    assert stack.deck_names == [] and stack.deck_attributes == {}, (
        "a controller the stack never saw must change nothing"
    )
    print("PASS: renaming a controller the stack never saw changes nothing")


def check_empty_name_uses_model_title() -> None:
    stack = _FakeStack()
    controller = _FakeController("clear-1")
    _number, title = stack.get_page_attributes(controller)
    page = stack.add_child("clear-1", title)

    name_deck("clear-1", "Studio")
    stack.refresh_page_title(controller)
    assert page.title == "Studio"

    name_deck("clear-1", "")
    stack.refresh_page_title(controller)
    assert page.title == "Stream Deck MK.2", (
        f"clearing the name must give the model title back, got {page.title!r}"
    )
    print("PASS: clearing the name gives the model title back")


class _FakeNameRow:
    """Run real name-row apply and load methods over their display-free state."""

    deck_stack = DeckName.deck_stack
    on_apply = DeckName.on_apply
    load_default = DeckName.load_default

    def __init__(self, serial: str, stack, text: str = ""):
        self.deck_serial_number = serial
        self.text = text
        self.controller = _FakeController(serial)
        self.settings_page = _Bag(
            deck_controller=self.controller,
            deck_stack_child=_Bag(deck_stack=stack),
        )
        self.connects = 0
        self.disconnects = 0

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text

    def connect_signal(self) -> None:
        self.connects += 1

    def disconnect_signal(self) -> None:
        self.disconnects += 1


class _Bag:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _RecordedSaves:
    """Record saves because the unit settings manager exposes its live dictionary."""

    def __init__(self):
        self.saves: list[tuple[str, dict]] = []

    def __enter__(self) -> "_RecordedSaves":
        self._manager = gl.settings_manager
        self._original = self._manager.save_deck_settings

        def recording(serial, settings):
            self.saves.append((serial, dict(settings)))
            self._original(serial, settings)

        self._manager.save_deck_settings = recording
        return self

    def __exit__(self, *_exc: object) -> None:
        del self._manager.save_deck_settings


def check_apply_writes_trims_and_retitles() -> None:
    stack = _FakeStack()
    row = _FakeNameRow("apply-1", stack)
    stack.get_page_attributes(row.controller)
    page = stack.add_child("apply-1", stack.deck_attributes[row.controller][1])

    row.text = "  Studio  "
    with _RecordedSaves() as recorder:
        row.on_apply(None)

    assert recorder.saves == [("apply-1", {"name": "Studio"})], (
        f"apply must save the trimmed name to this deck's settings, the saves "
        f"were {recorder.saves}"
    )
    stored = gl.settings_manager.get_deck_settings("apply-1").get("name")
    assert stored == "Studio", (
        f"apply must save the trimmed name, the file holds {stored!r}"
    )
    assert row.text == "Studio", (
        f"the row must show what was saved, it shows {row.text!r}"
    )
    assert page.title == "Studio", (
        f"apply must retitle the live child, it reads {page.title!r}"
    )
    print("PASS: apply saves the trimmed name and retitles the live child")


def check_apply_clears_the_name() -> None:
    stack = _FakeStack()
    row = _FakeNameRow("apply-2", stack)
    stack.get_page_attributes(row.controller)
    page = stack.add_child("apply-2", stack.deck_attributes[row.controller][1])

    row.text = "Studio"
    row.on_apply(None)
    row.text = "    "
    row.on_apply(None)

    assert gl.settings_manager.get_deck_settings("apply-2").get("name") == "", (
        "a row of spaces must clear the name rather than store the spaces"
    )
    assert page.title == "Stream Deck MK.2", (
        f"clearing the name must give the model title back, got {page.title!r}"
    )
    print("PASS: a row of spaces clears the name")


def check_apply_survives_a_missing_stack() -> None:
    """A row whose page has no stack still saves. It loses the live retitle."""
    row = _FakeNameRow("apply-3", None)
    row.settings_page = _Bag(deck_controller=row.controller, deck_stack_child=None)
    row.text = "Studio"
    with _RecordedSaves() as recorder:
        row.on_apply(None)
    assert recorder.saves == [("apply-3", {"name": "Studio"})], (
        f"the name must reach the file whether or not there is a stack to "
        f"retitle, the saves were {recorder.saves}"
    )
    print("PASS: apply saves with no stack to retitle")


def check_load_shows_the_stored_name() -> None:
    stack = _FakeStack()
    name_deck("load-1", "Studio")
    row = _FakeNameRow("load-1", stack, text="stale")
    row.load_default()
    assert row.text == "Studio", f"the row opened showing {row.text!r}"
    assert row.disconnects == 1 and row.connects == 1, (
        f"the load must leave the row wired: {row.disconnects} off, {row.connects} on"
    )

    # A hand-edited file can hold a value that is not a string.
    gl.settings_manager.save_deck_settings("load-2", {"name": 17})
    row = _FakeNameRow("load-2", stack, text="stale")
    row.load_default()
    assert row.text == "", f"a non-string stored name must show empty, got {row.text!r}"

    row = _FakeNameRow("load-3", stack)
    row.load_default()
    assert row.text == "", "a deck with no chosen name must open on an empty row"
    assert gl.settings_manager.get_deck_settings("load-3") == {}, (
        "opening the page must not write a name into the settings"
    )
    print("PASS: the row opens on the stored name and writes nothing")


def check_locale_key_is_filled() -> None:
    lm = LocaleManager(CSV_PATH)
    row = lm.locale_data.get(NAME_KEY)
    assert row is not None, f"locales.csv carries no row for {NAME_KEY}"
    assert len(lm.available_locales) >= 5, (
        f"expected at least the five shipped locales, found {lm.available_locales}"
    )
    for language in lm.available_locales:
        assert row.get(language), f"{language} has no label for the name row"
        assert row[language].strip(), f"{language} has an empty label for the name row"
    for language in lm.available_locales:
        lm.set_language(language)
        assert lm.get(NAME_KEY) != NAME_KEY, (
            f"{language}: the row renders the raw key"
        )
    print("PASS: the name row's label is filled for every shipped locale")


def check_the_row_holds_no_timeout() -> None:
    """Require immediate apply because the row has no hook to cancel deferred writes."""
    source_path = os.path.join(
        REPO_ROOT, "src", "windows", "mainWindow", "elements", "DeckSettings",
        "DeckGroup.py",
    )
    with open(source_path) as source_file:
        source = source_file.read()
    start = source.index("class DeckName(")
    end = source.index("class Rotation(")
    row_source = source[start:end]
    assert "timeout_add" not in row_source and "idle_add" not in row_source, (
        "the name row armed a main-loop source; it must write on apply instead"
    )
    assert f'gl.lm.get("{NAME_KEY}")' in row_source, (
        "the name row must look its label up by the locale key"
    )
    assert 'self.connect("map", self.load_default)' in row_source, (
        "the name row must reload on map, or a rename made elsewhere never shows"
    )
    print("PASS: the name row writes on apply and arms no main-loop source")


def check_real_name_row_save_flow() -> None:
    """Build a real DeckName to check its cap, apply handler, and map reload.
    This check needs libadwaita and a display.
    """
    if not fixtures.has_usable_display():
        print("SKIP: no usable display; the real name row is not built")
        return

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    gl.lm = LocaleManager(CSV_PATH)
    from src.windows.mainWindow.elements.DeckSettings.DeckGroup import DeckName

    stack = _FakeStack()
    controller = _FakeController("real-1")
    _number, title = stack.get_page_attributes(controller)
    page = stack.add_child("real-1", title)
    settings_page = _Bag(deck_controller=controller,
                         deck_stack_child=_Bag(deck_stack=stack))

    row = DeckName(settings_page, "real-1")
    try:
        assert row.get_max_length() == DECK_NAME_MAX_LENGTH, (
            f"the row must refuse more than the switcher can show, its cap is "
            f"{row.get_max_length()}"
        )
        assert row._apply_handler_id is not None, (
            "construction must wire the apply handler, or no name ever saves"
        )
        # A second load, as a page re-map runs, must leave the handler wired.
        row.load_default()
        assert row._apply_handler_id is not None, (
            "a reload must leave the apply handler wired"
        )

        row.set_text("  Studio  ")
        row.emit("apply")
        stored = gl.settings_manager.get_deck_settings("real-1").get("name")
        assert stored == "Studio", (
            f"emitting apply on the real row must save the trimmed name, the "
            f"file holds {stored!r}"
        )
        assert page.title == "Studio", (
            f"emitting apply must retitle the live child, it reads {page.title!r}"
        )
    finally:
        row.disconnect_signal()
    print("PASS: a real name row wires its apply handler and saves through it")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_deck_display_name")
    fixtures.install_stub_globals()

    check_display_name_always_returns_text()
    check_display_name_precedence()
    check_display_name_trims_and_caps()
    check_name_cap_excludes_fallbacks()
    check_reading_the_name_writes_nothing()

    check_base_title_reads_the_settings()
    check_base_title_uses_live_serial()
    check_base_title_handles_dead_device()
    check_chosen_name_gets_duplicate_suffix()
    check_model_digits_survive_duplicate_suffix()
    check_title_namespace_collisions()
    check_attributes_are_cached_per_controller()
    check_titles_are_nonempty_strings()
    check_only_unreadable_serial_returns_none()

    check_rename_retitles_the_live_child()
    check_repeated_renames_gain_no_suffix()
    check_taken_name_rename_adds_suffix()
    check_absent_child_rename_records_title()
    check_unknown_controller_rename_is_noop()
    check_empty_name_uses_model_title()

    check_apply_writes_trims_and_retitles()
    check_apply_clears_the_name()
    check_apply_survives_a_missing_stack()
    check_load_shows_the_stored_name()

    check_locale_key_is_filled()
    check_the_row_holds_no_timeout()
    check_real_name_row_save_flow()

    print("PASS: scenario_deck_display_name")


if __name__ == "__main__":
    main()
