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
import datetime
import os
import threading
import zipfile
from contextlib import contextmanager, suppress
from typing import cast, Any, Iterator, TypedDict, TYPE_CHECKING

if TYPE_CHECKING:
    from src.backend.SettingsManager import SettingsManager

from loguru import logger as log

from src.Signals import Signals
from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS
from src.backend.DeckManagement.deck_controller.controller import DeckController

from src.backend.PageManagement.Page import Page
from src.backend.PageManagement import page_flush
from src.backend.PageManagement.page_flush import canonical_path
from src.backend.PageManagement.page_document import PageDocument
from src.backend.PageManagement.page_pins import PagePins
from src.backend.DeckManagement.HelperMethods import natural_sort_by_filenames
from src.backend.atomic_json import atomic_copy_file, atomic_write_json, require_containment
from src.backend import settings_store

import globals as gl


# "Argument not given", for a setter whose None already means "clear the key".
_UNSET: Any = object()


class PageEntry(TypedDict):
    """One cached page slot, with the Page object and its LRU stamp."""
    page: "Page"
    page_number: int


class PageManagerBackend:
    def __init__(self, settings_manager: "SettingsManager") -> None:
        self.settings_manager = settings_manager

        # Guard page-cache reads and mutations across close and media threads.
        # Use an RLock because cache methods call each other while holding it.
        self._pages_lock = threading.RLock()
        self.pages: dict["DeckController", dict[str, PageEntry]] = {}
        # Track cache-miss builds by (controller, path) under _pages_lock.
        # Concurrent callers wait instead of registering duplicate action handlers.
        self._loads_in_flight: dict[tuple["DeckController", str], tuple[threading.Thread, threading.Event]] = {}
        # Keep one unpruned document per canonical path, with one save lock and pending record.
        # Deleted and recreated paths reuse the document, which the next Page load refreshes.
        self._documents: dict[str, PageDocument] = {}
        self._documents_guard = threading.Lock()
        # Holders of a cached page that eviction cannot see for itself.
        # Public, so the deck controller can bracket its tick and key work.
        self.pins = PagePins()
        self.custom_pages: list[str] = []

        self.page_order: list[str] = []

        # The setting excludes the active page, while max_pages includes it.
        # Apply the same +1 used when the Settings UI changes the cache budget.
        n_cached_pages = self.settings_manager.app().n_cached_pages
        self.max_pages = int(n_cached_pages) + 1
        self.page_number = 0

        self.MAX_BACKUPS = 5
        self.PAGE_PATH = os.path.join(gl.DATA_PATH, "pages")

    def load_page(self, path: str, deck_controller: "DeckController") -> Page | None:
        """Load and cache a page for one deck controller."""
        if not path or not os.path.isfile(path):
            return None

        page = Page(json_path=path, deck_controller=deck_controller)
        with self._pages_lock:
            self.pages.setdefault(deck_controller, {})
            self.pages[deck_controller][path] = {"page": page, "page_number": self.page_number}
            self.page_number += 1

        return page

    def get_page(self, path: str, deck_controller: "DeckController") -> Page | None:
        in_flight_key = (deck_controller, path)

        while True:
            with self._pages_lock:
                entry = self.pages.get(deck_controller, {}).get(path)
                if entry is not None:
                    entry["page_number"] = self.page_number
                    page: Page | None = entry["page"]
                    self.page_number += 1
                    self.pins.reserve_fetch(page, deck_controller)
                    return page

                in_flight = self._loads_in_flight.get(in_flight_key)
                if in_flight is None:
                    done = threading.Event()
                    self._loads_in_flight[in_flight_key] = (threading.current_thread(), done)
                    break

            builder_thread, done = in_flight
            if builder_thread is threading.current_thread():
                # A plugin can re-enter this page load during action initialization.
                # Build its private twin directly because waiting here self-deadlocks.
                page = self.load_page(path, deck_controller)
                self.pins.reserve_fetch(page, deck_controller)
                self.clear_old_cached_pages()
                return page

            done.wait()
            # Re-check after the builder ends; after failure, this caller becomes the builder.

        # Construct outside _pages_lock; load_page acquires it only for insertion.
        # Slow file I/O must not stall another controller's lookup.
        try:
            page = self.load_page(path, deck_controller)
        finally:
            # Release the waiters even when the construction raises. They
            # re-check the cache and take over while it stays empty.
            with self._pages_lock:
                self._loads_in_flight.pop(in_flight_key, None)
            done.set()

        # Reserve before this fetch's own eviction pass. That pass sorts a
        # fresh page last, so it reaches one only when the excess covers all.
        self.pins.reserve_fetch(page, deck_controller)
        self.clear_old_cached_pages()
        return page

    def discard_controller(self, deck_controller: "DeckController") -> None:
        """Drop all cached pages of a torn-down controller.
        Dead-controller entries distort eviction because their active page cannot be evicted."""
        # Flush each page at deck close; eviction skips writes because its document survives.
        # Continue after failures so an unserializable page cannot keep the controller cached.
        flush = page_flush.get()
        with self._pages_lock:
            paths = list(self.pages.get(deck_controller, {}))
        for path in paths:
            try:
                flush.flush_path(path)
            except Exception:
                log.opt(exception=True).warning(
                    f"Could not write pending edits of page {path} while closing a deck")

        with self._pages_lock:
            self.pages.pop(deck_controller, None)
            # The deck is gone, so its outstanding fetch has no later holder.
            self.pins.release_fetch(deck_controller)

    def pages_for_controller(self, deck_controller: "DeckController") -> list["Page"]:
        """Return one controller's cached Pages as a snapshot.
        Deck close can then run plugin teardown without holding _pages_lock."""
        with self._pages_lock:
            cached = self.pages.get(deck_controller, {})
            return [entry["page"] for entry in cached.values() if entry.get("page") is not None]

    def all_cached_pages(self) -> list["Page"]:
        """Return every cached Page as a snapshot.
        Callers can then run plugin teardown without holding _pages_lock."""
        with self._pages_lock:
            return [
                entry["page"]
                for controller_pages in self.pages.values()
                for entry in controller_pages.values()
                if entry.get("page") is not None
            ]

    def get_pages(self, add_custom_pages: bool = True, sort: bool = True) -> list[str]:
        pages = []

        os.makedirs(self.PAGE_PATH, exist_ok=True)

        for page in os.listdir(self.PAGE_PATH):
            if not page.endswith(".json"):
                continue

            pages.append(os.path.join(self.PAGE_PATH, page))

        if add_custom_pages:
            pages.extend(self.custom_pages)

        if sort:
            pages = natural_sort_by_filenames(pages)

        return pages

    def get_page_names(self, add_custom_pages: bool = True) -> list[str]:
        page_names = []

        for page in self.get_pages(add_custom_pages=add_custom_pages):
            name = os.path.basename(page)
            name = name.split(".")[0]
            page_names.append(name)

        return page_names

    def clear_old_cached_pages(self) -> None:
        # Eviction never writes JSON and excludes live or pinned Pages; a bad eviction leaves dead actions.
        # Select and remove under lock, then run potentially blocking plugin teardown after release.
        with self._pages_lock:
            total = sum(len(controller_pages) for controller_pages in self.pages.values())
            excess = total - self.max_pages
            if excess <= 0:
                return

            # Evict oldest first, excluding active and pinned pages.
            # Pins cover fetches, ticks, and gestures.
            evictable = []
            for controller, controller_pages in self.pages.items():
                if controller.active_page is None:
                    continue
                for path, page_data in controller_pages.items():
                    page = page_data["page"]
                    if page is controller.active_page:
                        continue
                    if self.pins.is_pinned(page):
                        continue
                    evictable.append((page_data["page_number"], controller_pages, path, page))

            evictable.sort(key=lambda entry: entry[0])
            to_evict = evictable[:excess]

        # discard_controller can orphan a captured controller_pages dict.
        # Its later pop remains a safe no-op.
        for _, controller_pages, path, page in to_evict:
            # Revalidate active or screensaver-pending state and fetch, tick, or gesture pins under lock.
            # Pop before teardown so a concurrent fetch cannot receive the gutted Page.
            with self._pages_lock:
                current_entry = controller_pages.get(path)
                if current_entry is None or current_entry.get("page") is not page:
                    continue
                if self.pins.is_pinned(page):
                    continue
                if self._page_is_live(page):
                    continue
                controller_pages.pop(path, None)
            log.info(f"Evicting cached page {path}")
            # Run plugin teardown outside the lock so a wedged hook cannot block cache users.
            page.clear_action_objects()

    def _page_is_live(self, page_obj: "Page") -> bool:
        """Return whether a controller shows or has stashed this Page."""
        # A screensaver stash stays live until hide and must not be evicted.
        # Controller fields cover exact ownership; pins cover unnamed transition windows.
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            if controller.active_page is page_obj:
                return True
            if getattr(controller, "_screensaver_pending_page", None) is page_obj:
                return True
        return False

    def get_default_page(self, deck_serial_number: str) -> str | None:
        page_settings = settings_store.get().read(settings_store.PAGES)
        page_path = page_settings.get("default-pages", {}).get(deck_serial_number, None)

        if page_path and os.path.isfile(page_path):
            return cast(str, page_path)

        return None

    # path=None is the documented value that clears this deck's default page.
    # get_all_default_page_serial_numbers skips a falsy entry for that reason.
    def set_default_page(self, deck_serial_number: str, path: str | None) -> None:
        # Serialize this pages.json read-modify-write with the store's per-file lock.
        # Callers must not hold _pages_lock while acquiring it.
        with settings_store.get().edit(settings_store.PAGES) as page_settings:
            page_settings.setdefault("default-pages", {})
            page_settings["default-pages"][deck_serial_number] = path

    def get_all_default_page_serial_numbers(self) -> list[str]:
        serial_numbers: list[str] = []

        page_settings = settings_store.get().read(settings_store.PAGES)
        for serial_number, page_path in page_settings.get("default-pages", {}).items():
            if not page_path:
                continue
            serial_numbers.append(serial_number)

        return serial_numbers

    def get_serial_numbers_from_page(self, path: str | None) -> list[str]:
        serial_numbers: list[str] = []

        page_settings = settings_store.get().read(settings_store.PAGES)
        for serial_number, page_path in page_settings.get("default-pages", {}).items():
            if path != page_path:
                continue
            serial_numbers.append(serial_number)

        return serial_numbers

    def set_pages_to_cache(self, amount: int) -> None:
        old_max_pages = self.max_pages

        self.max_pages = amount + 1

        if old_max_pages > self.max_pages:
            self.clear_old_cached_pages()

    def move_page(self, old_path: str, new_path: str) -> None:
        # Enforce containment at this mutation seam for both source and destination.
        # Crafted names must not copy or delete files outside the pages directory.
        require_containment(self.PAGE_PATH, old_path)
        require_containment(self.PAGE_PATH, new_path)

        # Refuse an existing destination before discarding its pending edits.
        # Atomic publication below remains the race-safe guarantee.
        if os.path.exists(new_path):
            raise ValueError(f"{new_path!r} already exists")

        # Read barrier. The copy below reads the old file, so its pending
        # edits go to disk first, or the renamed page arrives without them.
        page_flush.get().flush_path(old_path)

        # Discard destination writes that could land after the copy and undo the rename.
        # This is a no-op unless the destination previously held a page.
        page_flush.get().discard_path(new_path)

        # Publish with os.link and refuse an existing name, including one from a winning writer.
        # Failure leaves no destination, only a complete source and a reapable temp file.
        try:
            atomic_copy_file(old_path, new_path, overwrite=False)
        except FileExistsError as error:
            raise ValueError(f"{new_path!r} already exists") from error

        # Move the document before old-name fetches can mint throwaway documents.
        # The real document retains its content and pending edits.
        document = self.rename_document(old_path, new_path)

        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            if controller.active_page is None:
                continue

            page = self.get_page(old_path, controller)

            if not page:
                continue

            page.json_path = new_path
            page.rebind_document(document)
            # Nothing here activates the page, so this fetch retires at once.
            self.pins.release_fetch(controller)

        # Update pages.json after releasing _pages_lock.
        # The required lock order acquires the store edit lock second, never concurrently.
        with settings_store.get().edit(settings_store.PAGES) as page_settings:
            default_pages = page_settings.get("default-pages", {})
            for serial_number, path in default_pages.items():
                if path != old_path:
                    continue
                default_pages[serial_number] = new_path
            page_settings["default-pages"] = default_pages

        # Discard old-path writes only after every Page points at the destination.
        # Edits marked during the move stay in shared memory for the next destination save.
        page_flush.get().discard_path(old_path)

        # Treat a concurrently removed source as an already completed move.
        with suppress(FileNotFoundError):
            os.remove(old_path)
        self.refresh_window_watch_state()

    def remove_page(self, page_path: str) -> None:
        # External API names are untrusted; reject paths outside the pages directory.
        # Validate before teardown or deletion.
        require_containment(self.PAGE_PATH, page_path)
        # Iterate over all deck controllers to handle any that are using the page to be removed
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # Remove a matching screensaver-pending request and cache entry.
            # Otherwise hide recreates the deleted page and leaves the deck on its current page.
            pending = getattr(controller, "_screensaver_pending_page", None)
            if pending is not None and pending.json_path == page_path:
                controller._screensaver_pending_page = None
                with self._pages_lock:
                    controller_pages = self.pages.get(controller, {})
                    entry = controller_pages.pop(page_path, None)
                    if not controller_pages:
                        self.pages.pop(controller, None)
                if entry is not None:
                    entry["page"].clear_action_objects()
                # Release the stashed fetch because no later hide can install or retire it.
                self.pins.release_fetch(controller)

            active_page = controller.active_page

            # Skip controllers without an active page or not using the page to be deleted
            if not active_page or active_page.json_path != page_path:
                continue

            # Determine the default page for this controller's deck
            serial = controller.deck.get_serial_number()
            deck_default = self.get_default_page(serial)

            if deck_default and deck_default != page_path:
                # Load and switch to the default page if it's not the one being deleted
                new_page = self.get_page(deck_default, controller)
            else:
                page_list = self.get_pages()
                if page_path in page_list:
                    page_list.remove(page_path)
                new_page = self.get_page(page_list[0], controller) if page_list else None

            if new_page:
                controller.load_page(new_page)
            else:
                # No replacement can retire this deck's fetch, which can name the deleted page.
                # Release it now so it does not stay pinned until another page request.
                self.pins.release_fetch(controller)

            # Remove the page from the created pages cache for this controller
            with self._pages_lock:
                controller_pages = self.pages.get(controller, {})
                entry = controller_pages.pop(page_path, None)
            if entry is not None:
                # No live guard: caches are per controller, and this Page is replaced or has no page left.
                # Screensavers fail the path guard; clear outside the lock because plugin hooks can block.
                entry["page"].clear_action_objects()
                if not controller_pages:
                    with self._pages_lock:
                        self.pages.pop(controller, None)

        # Discard pending writes after cache teardown, when no in-tree Page can mark it again.
        # A later timer must not recreate the deleted file.
        page_flush.get().discard_path(page_path)

        # Delete the JSON file representing the page
        if os.path.exists(page_path):
            os.remove(page_path)

        # Remove default-page references under the store lock after releasing _pages_lock.
        # This preserves the required order between those locks.
        with settings_store.get().edit(settings_store.PAGES) as settings:
            default_pages = settings.get("default-pages", {})
            settings["default-pages"] = {
                serial: path for serial, path in default_pages.items() if path != page_path
            }

        # A delete of the page that carried the only rule takes the rule with
        # it, so the watcher needs a new gate here too.
        self.refresh_window_watch_state()

    def add_page(self, page_name: str, page_dict: dict[str, Any] | None = None) -> str:
        page_dict = page_dict or {}

        # The app creates the pages dir at startup. A caller before that init,
        # such as a test, must not crash here.
        os.makedirs(self.PAGE_PATH, exist_ok=True)

        path = os.path.join(self.PAGE_PATH, f"{page_name}.json")
        # Import, API, and dialog names are untrusted; reject paths outside the pages directory.
        require_containment(self.PAGE_PATH, path)
        if os.path.exists(path):
            raise FileExistsError(f"A page with the name '{page_name}' already exists.")

        # Imported or duplicated content can add rules; discard stale writes before reusing its path.
        # This prevents old Page holders from resurrecting deleted content over the new page.
        page_flush.get().discard_path(path)
        atomic_write_json(path, page_dict)
        self.refresh_document(path)

        self.refresh_window_watch_state()
        return path

    def register_page(self, path: str) -> None:
        if not os.path.isfile(path):
            log.error(f"Page {path} does not exist")
            return

        log.trace(f"Registering page {path}")
        self.custom_pages.append(path)

        gl.signal_manager.trigger_signal(Signals.PageAdd, path)

        # A registered custom page matches like any other, so its rule counts
        # towards the watcher gate from here on.
        self.refresh_window_watch_state()

    def unregister_page(self, path: str) -> None:
        if not self.custom_pages.__contains__(path):
            return

        self.custom_pages.remove(path)
        gl.signal_manager.trigger_signal(Signals.PageDelete, path)
        self.refresh_window_watch_state()

    def get_pages_with_path(self, path: str) -> "list[Page]":
        pages_set = set()

        # Hold _pages_lock because discard_controller can remove whole entries concurrently.
        # This block performs lookups only and invokes no plugin hook.
        with self._pages_lock:
            for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
                page = controller.active_page
                if page is not None and page.json_path == path:
                    pages_set.add(page)

                entry = self.pages.get(controller, {}).get(path)
                if entry is not None:
                    pages_set.add(entry["page"])

        return list(pages_set)

    def reload_pages_with_path(self, path: str, brightness: bool = True, screensaver: bool = True, background: bool = True, inputs: bool = True) -> None:
        pages = self.get_pages_with_path(path)

        for page in pages:
            page.load()

            if page.deck_controller.active_page != page:
                continue

            page.deck_controller.load_page(page, allow_reload=True,
                                           load_brightness=brightness,
                                           load_screensaver=screensaver,
                                           load_background=background,
                                           load_inputs=inputs)

    @staticmethod
    def reload_all_pages() -> None:
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            active_page = controller.active_page
            if active_page is None:
                # The deck is present with no page loaded, at boot or after a
                # failed page load, so there is nothing to reload.
                log.warning(f"Deck {controller.serial_number()} has no active page; skipping reload")
                continue
            controller.load_page(active_page, allow_reload=True)

    def get_document(self, path: str) -> PageDocument:
        """Return the shared document for path, creating it when absent."""
        key = canonical_path(path)
        with self._documents_guard:
            document = self._documents.get(key)
            if document is None:
                document = PageDocument(path)
                self._documents[key] = document
            return document

    def existing_document(self, path: str) -> PageDocument | None:
        """Return an existing document for path without creating one.
        This avoids retaining every page that a file sweep visits."""
        with self._documents_guard:
            return self._documents.get(canonical_path(path))

    def refresh_document(self, path: str) -> None:
        """Refresh an existing document after an external file write.
        All Pages on the path receive the change; unheld paths stay untouched."""
        document = self.existing_document(path)
        if document is None:
            return
        document.refresh_from_disk()

    def rename_document(self, old_path: str, new_path: str) -> PageDocument:
        """Move old_path's shared document to new_path and return it.
        Existing Pages keep the same document object and any unsaved edits."""
        # Move the source; displace but do not mutate held destination docs; otherwise reuse or create one.
        # Moved docs keep unsaved edits, while reused destination docs refresh after the registry lock.
        old_key = canonical_path(old_path)
        new_key = canonical_path(new_path)
        with self._documents_guard:
            moved = self._documents.pop(old_key, None)
            if moved is not None:
                moved.json_path = new_path
                self._documents[new_key] = moved
                return moved
            document = self._documents.get(new_key)
            if document is None:
                document = PageDocument(new_path)
                self._documents[new_key] = document
        # Refresh reused destination content so it cannot restore overwritten data on its next save.
        # Run outside the registry guard because refresh takes the page save lock.
        document.refresh_from_disk()
        return document

    def get_page_data(self, path: str | None, use_backup: bool = True) -> dict[str, Any]:
        """Read one page file, using a backup for a missing primary when requested.
        A corrupt primary always uses a valid backup; other I/O errors propagate."""
        if path is None:
            return {}

        # Read barrier. The outstanding edits of this page go to disk before
        # anything reads the file. It is one dict lookup when there are none.
        page_flush.get().flush_path(path)

        backup_path = os.path.join(self.PAGE_PATH, "backups", os.path.basename(path))

        # Substitute the backup for a missing primary, when use_backup is set.
        if not os.path.exists(path) and os.path.exists(backup_path) and use_backup:
            path = backup_path

        data, corrupt = self.settings_manager.load_settings_reporting_corruption(path)

        # Use a valid backup for every corrupt primary, independent of use_backup or quarantine success.
        # Returning an empty dict would let a settings write erase the page before a later heal.
        if corrupt and path != backup_path and os.path.exists(backup_path):
            healed, backup_corrupt = self.settings_manager.load_settings_reporting_corruption(backup_path)
            if not backup_corrupt:
                data = healed
                # Report the backup content served; the next save rewrites the primary.
                log.warning(f"Corrupt page {path}: serving its content from backup {backup_path}")
        return data

    def set_page_data(self, path: str, data: dict[str, Any], reload_brightness: bool = True, reload_screensaver: bool = True, reload_background: bool = True, reload_inputs: bool = True) -> None:
        """Replace a page through its shared document.
        Direct file replacement would let a pending Page write restore old content."""
        self.get_document(path).replace(data)
        if any([reload_brightness, reload_screensaver, reload_background, reload_inputs]):
            self.reload_pages_with_path(path,
                                        brightness=reload_brightness,
                                        screensaver=reload_screensaver,
                                        background=reload_background,
                                        inputs=reload_inputs)

    @staticmethod
    def _strip_asset(page_dict: dict[str, Any], abs_target_path: str) -> bool:
        """Remove one asset path from page content and return whether it was present."""
        page_had_asset = False

        # Read every section defensively, because a page json can carry no
        # keys, no states and no media.
        for key, key_data in page_dict.get("keys", {}).items():
            for state, state_data in key_data.get("states", {}).items():
                dict_path = state_data.get("media", {}).get("path")
                if dict_path is None:
                    continue

                if os.path.abspath(dict_path) == abs_target_path:
                    page_had_asset = True
                    state_data["media"]["path"] = None

        return page_had_asset

    def remove_asset_from_all_pages(self, path: str) -> None:
        if not path:
            raise ValueError("Invalid path")

        abs_target_path = os.path.abspath(path)

        for page_path in self.get_pages():
            # Edit held pages through their documents to serialize against live deck edits.
            # Do not create documents for unheld pages because the registry retains them.
            document = self.existing_document(page_path)
            if document is not None:
                # Scan a snapshot because concurrent live-content mutation can invalidate iteration.
                # Edit only after a match; both passes are idempotent, and the second finds what remains.
                page_had_asset = self._strip_asset(
                    document.get_without_action_objects(), abs_target_path)
                if page_had_asset:
                    with document.edit() as page_dict:
                        self._strip_asset(page_dict, abs_target_path)
            else:
                # Unheld pages have only a file copy; flush before bypassing get_page_data.
                # Corrupt reads preserve sidecar and backup, keep page_had_asset false, and skip the write.
                page_flush.get().flush_path(page_path)
                page_dict = self.settings_manager.load_settings_from_file(page_path)
                page_had_asset = self._strip_asset(page_dict, abs_target_path)
                if page_had_asset:
                    atomic_write_json(page_path, page_dict)

                    # A deck can load this page during the write, so refresh any new document holder.
                    self.refresh_document(page_path)

            # Reload active holders after edits release the document lock.
            # Reload takes the page-load lock and then reads the page file.
            if page_had_asset:
                pages = self.get_pages_with_path(page_path)
                for page in pages:
                    if page.deck_controller.active_page == page:
                        page.deck_controller.load_page(page, allow_reload=True)

    def find_matching_page_path(self, name: str) -> str | None:
        if not name:
            return None

        if os.path.isfile(name):
            return name

        target_name = name.lower()

        for page_path in self.get_pages():
            base = os.path.basename(page_path).lower()
            base_no_ext = os.path.splitext(base)[0]

            if base == target_name or base_no_ext == target_name:
                return page_path

        return None

    def backup_pages(self) -> None:
        time_stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")

        backup_zip_path = os.path.join(self.PAGE_PATH, "backups", f"backup_{time_stamp}.zip")

        os.makedirs(os.path.dirname(backup_zip_path), exist_ok=True)

        # Flush all pages before the archive reads them, including any pending edits.
        # This is normally a no-op because the caller runs at boot.
        page_flush.get().flush_all()

        with zipfile.ZipFile(backup_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as backup_zip:
            for page_path in self.get_pages():
                backup_zip.write(page_path, arcname=os.path.basename(page_path))

    def remove_old_backups(self) -> None:
        backup_dir = os.path.join(self.PAGE_PATH, "backups")

        # Return early while the backup directory is absent, or os.listdir
        # raises FileNotFoundError.
        if not os.path.exists(backup_dir):
            return

        backup_files = [file for file in os.listdir(backup_dir) if file.endswith(".zip")]

        # Sort the backups by the timestamp in the filename, newest first.
        # The filename format is backup_YYYYMMDDTHHMMSS.zip.
        def extract_timestamp(filename: str) -> str:
            return filename.removeprefix("backup_").removesuffix(".zip")

        sorted_backups = sorted(backup_files, key=extract_timestamp, reverse=True)

        # Keep exactly the newest MAX_BACKUPS files when the count exceeds the limit.
        if len(sorted_backups) <= self.MAX_BACKUPS:
            return

        for old_backup in sorted_backups[self.MAX_BACKUPS:]:
            backup_path = os.path.join(backup_dir, old_backup)
            try:
                os.remove(backup_path)
                log.info(f"Removed old page backup file: {old_backup}")
            except Exception as e:
                log.error(f"Failed to remove backup file {old_backup}: {e}")

    def get_page_settings(self, path: str | None) -> dict[str, Any]:
        # get_page_data answers {} for a None path, and the page editor reads
        # through here before it holds a page.
        data = self.get_page_data(path, False)
        return cast(dict[str, Any], data.get("settings", {}))

    @contextmanager
    def edit_page_settings(self, path: str) -> Iterator[dict[str, Any]]:
        """Edit a page's shared settings section under its document lock."""
        # Keep the partial read-modify-write in one block so concurrent page edits survive.
        with self.get_document(path).edit() as data:
            settings = data.get("settings")
            if not isinstance(settings, dict):
                settings = {}
                data["settings"] = settings
            yield settings

    def set_page_settings(self, path: str | None, settings: dict[str, Any]) -> None:
        """Replace the complete settings section of one page."""
        if path is None:
            return

        with self.get_document(path).edit() as data:
            data["settings"] = settings

    def any_auto_change_rule_enabled(self) -> bool:
        """Return whether any page enables an active-window change rule.
        This gates the watcher and uses the same accessor as rule matching."""
        # No index lists rules, so scan until the first hit; a full-deck page is about 16 KB.
        # The scan stays below 1 ms for a few pages and near 2 ms for 50; boot already reads all.
        for page_path in self.get_pages():
            try:
                if self.get_auto_change_settings(page_path).get("enable", False):
                    return True
            except Exception:
                # One unreadable page must not decide the gate for the rest.
                log.opt(exception=True).warning(f"Could not read the auto-change settings of {page_path}")
        return False

    def refresh_window_watch_state(self) -> None:
        """Recalculate the active-window watcher gate after rule changes.
        Importers call this after whole-page writes that bypass local setters."""
        # Never abort the page operation; failure only leaves a wasted poll or inactive auto-switch.
        # The grabber applies the decision on its own worker.
        window_grabber = gl.window_grabber
        if window_grabber is None:
            return
        try:
            window_grabber.refresh_watch_state()
        except Exception:
            log.opt(exception=True).warning("Could not update the active window watcher state")

    def get_auto_change_settings(self, path: str) -> dict[str, Any]:
        """
        Returns the auto change settings section of the page settings
        :param path: Path to the file
        :return: dict
        """
        page_settings = self.get_page_settings(path)
        return cast(dict[str, Any], page_settings.get("auto-change", {}))

    def set_auto_change_settings(self, path: str, enable: bool = False, wm_class: str = "", regex_title: str = "", stay_on_page: bool = False, decks: list[str] | None = None) -> None:
        decks = decks or []

        with self.edit_page_settings(path) as settings:
            settings["auto-change"] = {
                "enable": enable,
                "wm-class": wm_class,
                "title": regex_title,
                "stay-on-page": stay_on_page,
                "decks": decks
            }

        # Outside the block, because the watcher gate re-reads every page, and
        # every read of a page file takes the lock the block above holds.
        self.refresh_window_watch_state()

    def overwrite_auto_change_settings(self, path: str, enable: bool | None = None, wm_class: str | None = None, regex_title: str | None = None, stay_on_page: bool | None = None, decks: list[str] | None = None) -> None:
        with self.edit_page_settings(path) as settings:
            auto_change_settings = settings.setdefault("auto-change", {})

            if enable is not None:
                auto_change_settings["enable"] = enable
            if wm_class is not None:
                auto_change_settings["wm-class"] = wm_class
            if regex_title is not None:
                auto_change_settings["title"] = regex_title
            if stay_on_page is not None:
                auto_change_settings["stay-on-page"] = stay_on_page
            if decks is not None:
                auto_change_settings["decks"] = decks

        self.refresh_window_watch_state()

    def get_screensaver_settings(self, path: str | None) -> dict[str, Any]:
        page_settings = self.get_page_settings(path)
        return cast(dict[str, Any], page_settings.get("screensaver", {}))

    def set_screensaver_settings(self, path: str, overwrite: bool = False, enable: bool = False, time_delay: int = 5, loop: bool = True, fps: int = MEDIA_LOOP_FPS, brightness: float = 30, media_path: str = "") -> None:
        with self.edit_page_settings(path) as settings:
            settings["screensaver"] = {
                "overwrite": overwrite,
                "enable": enable,
                "time-delay": time_delay,
                "loop": loop,
                "fps": fps,
                "brightness": brightness,
                "media-path": media_path
            }

    def overwrite_screensaver_settings(self, path: str, overwrite: bool | None = None, enable: bool | None = None, time_delay: int | None = None, loop: bool | None = None, fps: int | None = None, brightness: float | None = None, media_path: str | None = None) -> None:
        with self.edit_page_settings(path) as settings:
            screensaver_settings = settings.setdefault("screensaver", {})

            if overwrite is not None:
                screensaver_settings["overwrite"] = overwrite
            if enable is not None:
                screensaver_settings["enable"] = enable
            if time_delay is not None:
                screensaver_settings["time-delay"] = time_delay
            if loop is not None:
                screensaver_settings["loop"] = loop
            if fps is not None:
                screensaver_settings["fps"] = fps
            if brightness is not None:
                screensaver_settings["brightness"] = brightness
            if media_path is not None:
                screensaver_settings["media-path"] = media_path

    def get_brightness_settings(self, path: str) -> dict[str, Any]:
        page_settings = self.get_page_settings(path)
        return cast(dict[str, Any], page_settings.get("brightness", {}))

    def set_brightness_settings(self, path: str, overwrite: bool = False, brightness: float = 75) -> None:
        with self.edit_page_settings(path) as settings:
            settings["brightness"] = {
                "overwrite": overwrite,
                "value": brightness
            }

    def overwrite_brightness_settings(self, path: str, overwrite: bool | None = None, brightness: float | None = None) -> None:
        with self.edit_page_settings(path) as settings:
            brightness_settings = settings.setdefault("brightness", {})

            if overwrite is not None:
                brightness_settings["overwrite"] = overwrite
            if brightness is not None:
                brightness_settings["value"] = brightness

    def get_background_settings(self, path: str | None) -> dict[str, Any]:
        page_settings = self.get_page_settings(path)
        return cast(dict[str, Any], page_settings.get("background", {}))

    def set_background_settings(self, path: str, overwrite: bool = False, show: bool = False, fps: int = MEDIA_LOOP_FPS, loop: bool = False, media_path: str = "", extend_to_touchscreen: bool = False) -> None:
        with self.edit_page_settings(path) as settings:
            settings["background"] = {
                "overwrite": overwrite,
                "show": show,
                "fps": fps,
                "loop": loop,
                "media-path": media_path,
                "extend-to-touchscreen": extend_to_touchscreen
            }

    def overwrite_background_settings(self, path: str, overwrite: bool | None = None, show: bool | None = None, fps: int | None = None, loop: bool | None = None, media_path: str | None = None, extend_to_touchscreen: bool | None = None, view: "dict[str, float] | None | object" = _UNSET) -> None:
        """Write the given keys; a default argument changes nothing. view=None
        clears the stored viewport, so _UNSET is its leave-alone default."""
        with self.edit_page_settings(path) as settings:
            background_settings = settings.setdefault("background", {})

            if overwrite is not None:
                background_settings["overwrite"] = overwrite
            if view is not _UNSET:
                background_settings.pop("view", None)
                if view is not None:
                    background_settings["view"] = view
            if show is not None:
                background_settings["show"] = show
            if fps is not None:
                background_settings["fps"] = fps
            if loop is not None:
                background_settings["loop"] = loop
            if media_path is not None:
                background_settings["media-path"] = media_path
            if extend_to_touchscreen is not None:
                background_settings["extend-to-touchscreen"] = extend_to_touchscreen
