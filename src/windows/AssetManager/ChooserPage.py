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
# Import gtk modules
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GLib

from typing import Any

import globals as gl

from src.windows.AssetManager import asset_search

# How long the entry waits for the typing to stop before it says the search
# changed. Gtk.SearchEntry owns this wait; its default is 150 ms. A pass here
# scores and sorts a whole icon pack, which is thousands of names, so the wait
# is longer than the default. It applies to typing only: GTK reports a cleared
# entry at once, so emptying the box brings the whole grid back with no wait.
SEARCH_DELAY_MS = 300


class ChooserPage(Gtk.Stack):
    # The search entry connects on_search_changed inside _build, which runs
    # from this constructor, so both of these must exist before __init__ of
    # any subclass reaches its own attributes.
    _search_generation = 0
    _search_showing = True
    # The query of the last pass that rendered. A page compares it against the
    # entry when it is shown again, so a grid that fell behind while it was
    # hidden catches up and one that did not keeps the page it was on.
    _searched_text = ""

    def __init__(self) -> None:
        super().__init__(margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        # The entry holds an emission back for its delay, so one can arrive
        # after this page stopped showing, and a pass then renders into a page
        # on its way out. A window that hides unmaps its pages, which covers
        # the hide, the close and the destroy paths alike. The destroy signal
        # does not: a widget that is not a window emits it from dispose, and
        # anything that still holds the page, such as a queued pass, keeps
        # dispose from running at all.
        self.connect("map", self._on_map)
        self.connect("unmap", self.invalidate_search)
        self._build()

        self.init_dnd()

    def _build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_titled(self.main_box, "main", "main")

        self.nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, vexpand=False, margin_bottom=15)
        self.main_box.append(self.nav_box)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Search", hexpand=True)
        self.search_entry.set_search_delay(SEARCH_DELAY_MS)
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.nav_box.append(self.search_entry)

        self.type_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["linked"], margin_start=15)
        self.nav_box.append(self.type_box)

        self.video_button = Gtk.ToggleButton(icon_name="camera-video-symbolic", css_classes=["blue-toggle-button"])
        self.video_button.connect("toggled", self.on_video_toggled)
        self.type_box.append(self.video_button)

        self.image_button = Gtk.ToggleButton(icon_name="camera-photo-symbolic", css_classes=["blue-toggle-button"])
        self.image_button.connect("toggled", self.on_image_toggled)
        self.type_box.append(self.image_button)

        self.scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.main_box.append(self.scrolled_window)

        self.scrolled_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=False,
                                margin_top=5, margin_bottom=5)
        self.scrolled_window.set_child(self.scrolled_box)

        self.inside_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=False)
        self.scrolled_box.append(self.inside_box)


        # Add vexpand box to the bottom to avoid unwanted stretching of the children
        self.fix_box = Gtk.Box(vexpand=True, hexpand=True)
        self.scrolled_box.append(self.fix_box)


        ## Loading box
        self.loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True,
                                   valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.add_titled(self.loading_box, "loading", "loading")

        self.spinner = Gtk.Spinner(spinning=False)
        self.loading_box.append(self.spinner)

        self.loading_label = Gtk.Label(label=gl.lm.get("store.page.loading-spinner.label"))
        self.loading_box.append(self.loading_label)

        self.set_loading(True)

    def set_loading(self, loading: bool) -> None:
        if loading:
            GLib.idle_add(self.set_visible_child_name, "loading")
            GLib.idle_add(self.spinner.start)
        else:
            GLib.idle_add(self.set_visible_child_name, "main")
            GLib.idle_add(self.spinner.stop)

    def init_dnd(self) -> None:
        self.dnd_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        self.dnd_target.connect("drop", self.on_dnd_drop)
        self.dnd_target.connect("accept", self.on_dnd_accept)

        self.main_box.add_controller(self.dnd_target)

    def on_dnd_accept(self, drop: Gtk.DropTarget, user_data: Gdk.Drop) -> bool | None:
        pass
    
    def on_dnd_drop(self, drop_target: Gtk.DropTarget, value: Any, x: float, y: float) -> bool | None:
        pass

    def show_for_path(self, path: str) -> None:
        pass

    def on_video_toggled(self, button: Gtk.ToggleButton) -> None:
        pass

    def on_image_toggled(self, button: Gtk.ToggleButton) -> None:
        pass

    def on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        """Queue a pass over the query the entry now holds.

        The entry owns the wait for the typing to stop, so this runs once per
        pause and not once per keystroke. A page overrides apply_search, never
        this method: the staleness guard belongs to every page that carries a
        search entry, and an override here loses it.
        """
        if not self._search_showing:
            # The entry held this emission for its delay, and the page stopped
            # showing in between. It renders again when it is shown.
            return
        self._search_generation += 1
        # One turn of the loop, not a wait of its own. A cleared entry reports
        # at once and a delayed emission can land in the same turn, and the
        # guard below then leaves one pass of the two.
        GLib.idle_add(self.run_search, self._search_generation)

    def run_search(self, generation: int) -> bool:
        """Render the query unless a later pass has overtaken this one."""
        if generation != self._search_generation:
            # A newer pass, or invalidate_search, moved the generation on. The
            # newer pass renders the text the user has now.
            return False
        query = self.search_entry.get_text()
        self.apply_search(query)
        return False  # one-shot idle

    def search_rendered(self, query: str) -> None:
        """Record that this page now shows what query asks for.

        A page calls this when a pass has put the query on screen, and never
        when it has only started the work. _on_map compares the entry against
        what this records, so a pass that gathers off the main thread and is
        dropped before it renders must leave it alone. Recorded too early, a
        dropped pass leaves the page believing it is current: the grid keeps
        the results of the query before, and only another keystroke recovers
        it, because showing the page again finds nothing to catch up with.
        """
        self._searched_text = query

    def search_is_current(self, generation: int) -> bool:
        """Whether a pass queued with generation is still the one to render.

        A pass that gathers off the main thread asks this before it renders,
        and the main-loop callback that renders asks it again, because the
        query can move on in between. A later pass, a page turn and a hidden
        window all answer False. It is the one staleness test a page outside
        this one may use, so a search that spans two pages guards on the page
        whose entry holds the query.
        """
        return generation == self._search_generation and self._search_showing

    def focus_search_entry(self) -> bool:
        """Take the typing to this page's entry. A one-shot idle callback.

        The cursor goes to the end of the text, because grabbing the focus of
        an entry selects everything it holds and the next keystroke would then
        replace the query rather than extend it.
        """
        self.search_entry.grab_focus()
        self.search_entry.set_position(-1)
        return False  # one-shot idle

    def invalidate_search(self, *args: Any) -> None:
        """Stop searching until this page shows again.

        Every pass in flight goes stale, and so does an emission the entry
        still holds. The scoring cache goes too: it memoizes the names of a
        whole pack, which a hidden window has no use for.
        """
        self._search_showing = False
        self._search_generation += 1
        asset_search.release_cache()

    def _on_map(self, *args: Any) -> None:
        """Catch the grid up with the entry, if it fell behind while hidden.

        The entry can be typed into or cleared while this page is hidden, and
        such a pass is dropped, so the grid would otherwise show the query of
        the last time the page was up. A query that has not moved renders
        nothing: a pass restarts the grid at its first page, and switching
        between the tabs of this window maps a page each time.
        """
        self._search_showing = True
        # Before the catch-up test, so a page that settles its entry as it
        # shows is compared against the entry it ends up with. Settled after,
        # the pass below would search for a query this page is about to throw
        # away.
        self.on_shown()
        if self.search_entry.get_text() == self._searched_text:
            return
        self._search_generation += 1
        self.run_search(self._search_generation)

    def on_shown(self) -> None:
        """Subclass hook: settle the entry as this page shows.

        It runs on the main thread, inside the map handler and before the
        catch-up pass. A page that keeps whatever the entry holds leaves it
        alone.
        """

    def apply_search(self, query: str) -> None:
        """Subclass hook: show what query asks for.

        It runs on the main thread, once the typing stops. A page with no
        grid to filter leaves it alone. A page that renders here says so with
        search_rendered; one that starts work which renders later says so
        when that work lands.
        """
