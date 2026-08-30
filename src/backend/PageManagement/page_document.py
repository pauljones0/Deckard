"""Keep one permanent canonical document and save lock per page after manager startup.
Before manager startup, a Page gets a private document because it has no registry or sibling."""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from typing import cast, Any, Iterator

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PageManagement import page_flush
from src.backend.atomic_json import atomic_copy_file


def snapshot_json_tree(value: Any) -> Any:
    """Copy JSON containers atomically under the GIL while sharing leaves.
    Shared leaves keep ActionCore unique; live content is unsafe for json.dump and deepcopy."""
    if isinstance(value, dict):
        return {key: snapshot_json_tree(item) for key, item in value.copy().items()}
    if isinstance(value, list):
        return [snapshot_json_tree(item) for item in list(value)]
    return value


def content_without_action_objects(data: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable snapshot without live action objects.
    Live mutation can break traversal; shallow deletion would alter source action dictionaries."""
    dictionary = snapshot_json_tree(data)
    for input_type in Input.KeyTypes:
        for key in dictionary.get(input_type, {}):
            for state in dictionary[input_type][key].get("states", {}):
                if "actions" not in dictionary[input_type][key]["states"][state]:
                    continue
                for action in dictionary[input_type][key]["states"][state]["actions"]:
                    if "object" in action:
                        del action["object"]

    return cast(dict[str, Any], dictionary)


def move_key_to_end(dictionary: dict[str, Any], key: str) -> None:
    """Move an existing key to the end of the caller's snapshot.
    Never use this on live content because pop and reinsert would mutate the page during save."""
    if key in dictionary:
        dictionary[key] = dictionary.pop(key)


def back_up_page_file(src_path: str) -> None:
    """Copy a valid page to pages/backups/ as the corrupt-primary heal source.
    Missing or invalid primaries leave backups unchanged but allow replacement writes."""
    os.makedirs(os.path.join(gl.DATA_PATH, "pages", "backups"), exist_ok=True)
    dst_path = os.path.join(gl.DATA_PATH, "pages", "backups", os.path.basename(src_path))

    try:
        with open(src_path) as f:
            json.load(f)
    except FileNotFoundError:
        # A quarantined primary can be live from its backup; let the write recreate
        # that primary instead of blocking recovery because there is nothing to copy.
        log.warning(f"No page file at {src_path} to back up; the write recreates it")
        return
    except ValueError as e:
        # ValueError includes JSONDecodeError and UnicodeDecodeError, so malformed
        # text cannot escape before parsing and block the replacement write.
        log.error(f"Invalid json in {src_path}: {e}")
        return

    # The copy is the heal source for a corrupt primary, so it must never be
    # a torn file itself: the atomic copy publishes whole or not at all.
    atomic_copy_file(src_path, dst_path)


def _apply(data: dict[str, Any], content: dict[str, Any]) -> None:
    """Apply content without replacing data; the caller holds the file lock.
    Bind new trees before removals so unlocked readers see whole values without transient gaps."""
    # Every Page aliases data. GIL-atomic update and deletion can leave a removed
    # section briefly visible, but never remove a section present in both versions.
    if content is data:
        return
    dropped = [key for key in data if key not in content]
    data.update(content)
    for key in dropped:
        del data[key]


class PageDocument:
    """The single in-memory content object for one page file.
    Page/flush share json_path and read-only data, preventing aliases to replaced dictionaries."""

    def __init__(self, path: str) -> None:
        self.json_path = path
        self._data: dict[str, Any] = {}
        # Serialize only the first fill. Take this above the save lock because
        # loading uses the page manager's read barrier and takes that lock.
        self._load_guard = threading.Lock()
        self._loaded = False

    @property
    def data(self) -> dict[str, Any]:
        """This page's content. The same object for every Page on the path."""
        return self._data

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Mutate this page under its file lock and mark it dirty even on error.
        Do not read page files or enter GTK in the block; their outer locks would deadlock."""
        # Load before locking so a new off-deck document cannot write only its edit.
        # Always mark partial mutations because every reader already sees them.
        self.ensure_loaded()
        with page_flush.save_lock(self.json_path):
            try:
                yield self._data
            finally:
                page_flush.get().mark_dirty(self)

    def replace(self, content: dict[str, Any]) -> None:
        """Replace all page content as one edit and take the input tree by reference.
        The caller must drop it; later mutation would change the live page without its lock."""
        # Replace memory and file together; a file-only replacement can lose to a
        # pending page edit that writes the old content back.
        with self.edit() as data:
            _apply(data, content)

    def ensure_loaded(self) -> None:
        """Load an unfilled document exactly once before non-Page access.
        The guard prevents a second read from discarding the first thread's edit."""
        with self._load_guard:
            if self._loaded:
                return
            self._load()

    def refresh_from_disk(self) -> None:
        """Refresh through the page manager's read barrier and corrupt-file recovery.
        A marked edit between load and swap can be lost because the read takes the file lock."""
        # External writers include full-page imports, pre-manager migrations, and
        # recreation of a deleted page under a document's retained name.
        with self._load_guard:
            self._load()

    def _load(self) -> None:
        page_manager = gl.page_manager
        if page_manager is None:
            # Only before create_global_objects(), with nothing to load from.
            return
        self.adopt(page_manager.get_page_data(self.json_path))
        self._loaded = True

    def adopt(self, content: dict[str, Any]) -> None:
        """Adopt content without replacing the shared dictionary.
        The lock prevents a flush between update and removal from restoring dropped sections."""
        with page_flush.save_lock(self.json_path):
            _apply(self._data, content)

    # What the flush seam needs from the holder of a page's content.

    def get_without_action_objects(self) -> dict[str, Any]:
        """This page's content as it goes into its file."""
        return content_without_action_objects(self._data)

    def move_key_to_end(self, dictionary: dict[str, Any], key: str) -> None:
        move_key_to_end(dictionary, key)

    def make_backup(self, json_path: str | None = None) -> None:
        """Back up the path whose save lock the flush holds.
        A move can repoint json_path during an old-path write; the current path can be wrong."""
        back_up_page_file(json_path if json_path is not None else self.json_path)


def document_for(path: str) -> PageDocument:
    """Return the manager's shared document, or a private pre-manager document.
    A Page created before global objects has no registry or sibling with which to share."""
    page_manager = gl.page_manager
    if page_manager is None:
        return PageDocument(path)
    return page_manager.get_document(path)
