"""Build icon packs from ZIP archives or picture folders in hidden, unique staging trees, then register them with one rename.
Validate source and destination paths, reject linked or special files, enforce the size budget, flatten deep folders, and copy only renderable formats."""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import threading
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from loguru import logger as log
from PIL import Image

import globals as gl
from src.backend import archive_safety
from src.backend.IconPackManagement.IconPack import IconPack
from src.backend.IconPackManagement.IconPackManager import IconPackManager
from src.backend.atomic_json import atomic_write_json

#: Hidden staging-directory suffix that pack discovery skips.
STAGING_SUFFIX = ".deckard-import"

#: What a pack goes into when the name the user typed leaves nothing usable.
FALLBACK_FOLDER_NAME = "Imported Pack"

#: Maximum generated folder-name length; longer user input is truncated.
MAX_FOLDER_NAME_LENGTH = 64

#: Maximum collision suffix attempts before an import fails.
MAX_NAME_ATTEMPTS = 100

#: Maximum total bytes copied or extracted. Archive declarations are checked before writes,
#: and streamed copies enforce the same bound.
MAX_UNPACKED_BYTES = 512 * 1024 * 1024

_COPY_CHUNK = 256 * 1024

#: The staging directories this session holds open. A sweep spares these, so a
#: live import's tree survives a second import that starts while it runs.
_staging_lock = threading.Lock()
_live_staging: set[str] = set()

#: GTK-main-thread state that prevents a second dialog import while one is active.
_import_in_flight = False


class PackImportError(Exception):
    """An import that cannot go on. Its text is shown to the user."""


@dataclass(frozen=True)
class _PlannedFile:
    """A planned source and its destination relative to the asset folder.
    source is a disk path for folders or an archive member name."""

    dest_rel: str
    source: str


def importable_extensions() -> frozenset[str]:
    """Return lowercase image, SVG, and GIF extensions that the icon importer accepts.
    Other video formats belong to wallpaper packs."""
    extensions = {str(ext).lower() for ext in gl.image_extensions}
    extensions |= {str(ext).lower() for ext in gl.svg_extensions}
    extensions.add("gif")
    return frozenset(extensions)


def _extension(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()


def _is_importable(path: str) -> bool:
    return _extension(path) in importable_extensions()


def folder_name_for(name: str) -> str:
    """Keep letters, digits, spaces, dots, dashes, and underscores in one non-hidden path component.
    Trim non-alphanumeric edges, cap the length, and use FALLBACK_FOLDER_NAME when empty."""
    kept = re.sub(r"[^A-Za-z0-9 ._-]", "-", name.strip())
    kept = re.sub(r"-{2,}", "-", kept)
    kept = kept.strip(" .-_")[:MAX_FOLDER_NAME_LENGTH].strip(" .-_")
    return kept or FALLBACK_FOLDER_NAME


def _free_folder_name(root: str, base: str) -> str:
    """Return base or its first free numbered variant under root.
    Folder names are internal, so collisions do not change the user-visible title."""
    for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
        candidate = base if attempt == 1 else f"{base} ({attempt})"
        if not os.path.lexists(os.path.join(root, candidate)):
            return candidate
    raise PackImportError(f"There are already too many packs called {base!r}.")


def _unique_dest(dest_dir: str, filename: str, taken: set[str]) -> str:
    """Return and reserve a destination relative to dest_dir.
    Add a number when flattened source paths would collide."""
    stem, extension = os.path.splitext(filename)
    candidate = os.path.join(dest_dir, filename) if dest_dir else filename
    attempt = 2
    while candidate in taken:
        numbered = f"{stem} ({attempt}){extension}"
        candidate = os.path.join(dest_dir, numbered) if dest_dir else numbered
        attempt += 1
    taken.add(candidate)
    return candidate


def _planned_destination(relative: str, taken: set[str]) -> str:
    """Keep only the source's first folder and file name because pack discovery reads one subfolder level.
    Clean both components and reserve a unique destination."""
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts:
        # Reject separator-only or dot-only paths before indexing the final component.
        raise PackImportError("A file in this import has no name, so nothing was imported.")
    filename = _clean_component(parts[-1])
    folder = _clean_component(parts[0]) if len(parts) > 1 else ""
    return _unique_dest(folder, filename, taken)


def _clean_component(name: str) -> str:
    """One path component, with everything that is not a plain name removed."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._()-]", "-", name).strip(" .")
    return cleaned or "file"


def _folder_plan(folder: str) -> list[_PlannedFile]:
    """Plan importable, non-symlink files under folder without following linked directories.
    Copying later validates ordinary files and enforces the byte budget."""
    taken: set[str] = set()
    planned: list[_PlannedFile] = []
    for path in sorted(_walk_files(folder)):
        if not _is_importable(path):
            continue
        relative = os.path.relpath(path, folder)
        planned.append(_PlannedFile(_planned_destination(relative, taken), path))
    return planned


def _walk_files(folder: str) -> Iterator[str]:
    # os.walk skips linked directories, but linked files still appear in filenames.
    # Skip those files so every yielded path remains under the chosen folder.
    for dirpath, _dirnames, filenames in os.walk(folder, followlinks=False):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                continue
            yield full


def _copy_planned_files(planned: list[_PlannedFile], assets_dir: str) -> None:
    """Copy only ordinary, single-link files; reject pipes, devices, symlinks, and hardlinks that can reference external data.
    Count streamed bytes against MAX_UNPACKED_BYTES."""
    written = 0
    for item in planned:
        try:
            info = os.lstat(item.source)
        except OSError as error:
            raise PackImportError(
                "A file in this folder could not be read, so nothing was imported."
            ) from error
        if not stat.S_ISREG(info.st_mode):
            raise PackImportError(
                "A file in this folder is not an ordinary picture, so nothing "
                "was imported."
            )
        if info.st_nlink > 1:
            raise PackImportError(
                "A file in this folder is shared with another outside it, so "
                "nothing was imported."
            )
        target = _checked_target(assets_dir, item.dest_rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(item.source, "rb") as source_file, open(target, "wb") as out:
            while True:
                chunk = source_file.read(_COPY_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UNPACKED_BYTES:
                    raise PackImportError(
                        "This folder holds more pictures than an icon pack can "
                        "take, so nothing was imported."
                    )
                out.write(chunk)


def _archive_plan(archive: zipfile.ZipFile) -> list[_PlannedFile]:
    """Validate every member before writing and reject the whole archive if any path escapes.
    Enforce the declared total-size budget before extraction."""
    for name in archive.namelist():
        reason = archive_safety.unsafe_member_reason(name)
        if reason is not None:
            log.error(f"Refusing the icon pack archive: {reason}: {name!r}")
            raise PackImportError(
                "This archive names a file outside the folder it unpacks into, so "
                "nothing from it was imported."
            )

    infos = [info for info in archive.infolist() if not info.is_dir()]
    declared = sum(info.file_size for info in infos)
    if declared > MAX_UNPACKED_BYTES:
        raise PackImportError("This archive is too large to import as an icon pack.")
    names = [info.filename for info in infos]

    prefix = _common_top_folder(names)
    taken: set[str] = set()
    planned: list[_PlannedFile] = []
    for name in sorted(names):
        if not _is_importable(name):
            continue
        relative = name[len(prefix):] if prefix and name.startswith(prefix) else name
        if not relative:
            continue
        planned.append(_PlannedFile(_planned_destination(relative, taken), name))
    return planned


def _common_top_folder(names: list[str]) -> str:
    """Return the sole shared top-level folder plus its separator, or an empty string.
    A member equal to that top name is a file and is not stripped."""
    tops = {name.replace("\\", "/").split("/")[0] for name in names}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    return f"{top}/" if top else ""


def _declared_size(archive: zipfile.ZipFile, member: str) -> int:
    """Return the member's declared unpacked size.
    ZipExtFile enforces it, but extraction also checks it for readers that may trust forged headers."""
    return archive.getinfo(member).file_size


def _extract_planned_files(archive: zipfile.ZipFile, planned: list[_PlannedFile],
                           assets_dir: str) -> None:
    for item in planned:
        try:
            limit = _declared_size(archive, item.source)
        except KeyError as error:
            # One open archive supplies the plan and extraction, so a missing member indicates a malformed handle.
            # Convert the lookup error to the import contract.
            raise PackImportError(
                "This archive is damaged, so nothing from it was imported."
            ) from error
        target = _checked_target(assets_dir, item.dest_rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # The member is opened by name and written to a path this module built,
        # so the archive chooses what comes out and never where it goes.
        written = 0
        with archive.open(item.source, "r") as member, open(target, "wb") as out:
            while True:
                chunk = member.read(_COPY_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise PackImportError(
                        "A file in this archive unpacks to more than it says, "
                        "so nothing from it was imported."
                    )
                out.write(chunk)


def _checked_target(base_dir: str, dest_rel: str) -> str:
    """Resolve dest_rel under base_dir and reject paths outside it.
    Keep this final write guard independent of earlier destination cleaning."""
    target = os.path.join(base_dir, dest_rel)
    if not archive_safety.resolved_within(base_dir, target):
        raise PackImportError("A file in this import would land outside the pack.")
    return target


def _is_decodable(path: str) -> bool:
    """Return whether path passes thumbnail validation.
    Verify raster images with PIL and require SVG or XML markup in an SVG header."""
    if _extension(path) == "svg":
        try:
            with open(path, "rb") as handle:
                head = handle.read(4096)
        except OSError:
            return False
        return b"<svg" in head or b"<?xml" in head
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        # PIL raises a wide family for a file it cannot read. Any of them means
        # the same thing here: this is not a picture.
        return False


def _write_thumbnail(staging: str, assets_dir: str, planned: list[_PlannedFile],
                     banner_path: str | None) -> str:
    """Write a valid banner or the first planned icon as the required thumbnail, preferring a raster fallback.
    Check the destination before copying."""
    if banner_path and os.path.isfile(banner_path) and _is_importable(banner_path) \
            and _is_decodable(banner_path):
        name = f"thumbnail{os.path.splitext(banner_path)[1].lower()}"
        shutil.copyfile(banner_path, _checked_target(staging, name))
        return name

    chosen = next((item for item in planned if _extension(item.dest_rel) != "svg"), planned[0])
    source = os.path.join(assets_dir, chosen.dest_rel)
    name = f"thumbnail{os.path.splitext(chosen.dest_rel)[1].lower()}"
    shutil.copyfile(source, _checked_target(staging, name))
    return name


def _new_staging(root: str) -> str:
    """Create a hidden, unique staging directory and track it while holding _staging_lock.
    The shared lock prevents stale-tree sweeps from seeing a new live directory as untracked."""
    with _staging_lock:
        staging = tempfile.mkdtemp(prefix=".", suffix=STAGING_SUFFIX, dir=root)
        _live_staging.add(staging)
    return staging


def _forget_staging(staging: str) -> None:
    with _staging_lock:
        _live_staging.discard(staging)


def _discard_staging(staging: str) -> None:
    """Drop a staging tree after a failure, and stop tracking it."""
    _remove_tree(staging)
    _forget_staging(staging)


def _sweep_stale_staging(root: str) -> None:
    """Remove stale hidden staging trees but spare paths tracked by this session.
    Hold _staging_lock across listing and removal so new live trees cannot appear untracked."""
    with _staging_lock:
        live = set(_live_staging)
        try:
            entries = os.listdir(root)
        except OSError:
            return
        for entry in entries:
            if not (entry.startswith(".") and entry.endswith(STAGING_SUFFIX)):
                continue
            path = os.path.join(root, entry)
            if path in live:
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                _remove_tree(path)


def _remove_tree(path: str) -> None:
    """Remove a staging tree, file, or symlink on a best-effort basis.
    A stuck leftover blocks no import because each staging directory has a unique name."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        try:
            os.remove(path)
        except OSError as error:
            log.warning(f"Could not remove {path}: {error}")


def import_is_running() -> bool:
    """Whether an import is in flight. Read on the GTK main thread only."""
    return _import_in_flight


def set_import_running(running: bool) -> None:
    """Mark the in-flight state. Set on the GTK main thread only."""
    global _import_in_flight
    _import_in_flight = running


def import_icon_pack(source: str, name: str, description: str = "",
                     banner_path: str | None = None) -> str:
    """Import a ZIP archive or picture folder off the main thread and return its new folder name.
    Raise PackImportError with user-facing text and leave no staging data on failure."""
    title = (name or "").strip()
    if not title:
        raise PackImportError("An icon pack needs a name.")

    try:
        if os.path.isdir(source):
            return _import_from_folder(source, title, description, banner_path)
        if zipfile.is_zipfile(source):
            return _import_from_archive(source, title, description, banner_path)
        if os.path.isfile(source):
            raise PackImportError("An icon pack comes from a zip archive or a folder.")
        raise PackImportError("That archive or folder is not there any more.")
    except PackImportError:
        raise
    except (zipfile.BadZipFile, zlib.error) as error:
        # A ZIP with an intact central directory can pass is_zipfile and fail during reading.
        # _build_pack has already removed its staging tree.
        raise PackImportError(
            "This archive is damaged, so nothing from it was imported."
        ) from error
    except OSError as error:
        log.opt(exception=True).error(f"Importing an icon pack from {source!r} failed: {error}")
        raise PackImportError(
            "The pack could not be written, so nothing was imported."
        ) from error


def _import_from_folder(source: str, title: str, description: str,
                        banner_path: str | None) -> str:
    plan = _folder_plan(source)
    return _build_pack(title, description, banner_path, plan, lambda planned, assets_dir:
                       _copy_planned_files(planned, assets_dir))


def _import_from_archive(source: str, title: str, description: str,
                         banner_path: str | None) -> str:
    # One open of the archive for the plan and the extraction both. A member
    # cannot vanish under a handle held open, and the file is read once.
    with zipfile.ZipFile(source, "r") as archive:
        plan = _archive_plan(archive)
        return _build_pack(title, description, banner_path, plan, lambda planned, assets_dir:
                           _extract_planned_files(archive, planned, assets_dir))


def _build_pack(title: str, description: str, banner_path: str | None,
                plan: list[_PlannedFile],
                populate: Callable[[list[_PlannedFile], str], None]) -> str:
    """Populate a staging asset folder, write shared pack metadata, and rename it into place.
    populate handles the source-specific copy or extraction."""
    if not plan:
        raise PackImportError(
            "No pictures the app can show were found, so there is no pack to make."
        )

    root = os.path.join(gl.DATA_PATH, IconPackManager.DATA_DIR)
    os.makedirs(root, exist_ok=True)
    _sweep_stale_staging(root)
    folder_name = _free_folder_name(root, folder_name_for(title))

    staging = _new_staging(root)
    try:
        assets_dir = os.path.join(staging, IconPack.ASSET_MANIFEST_KEY)
        os.makedirs(assets_dir, exist_ok=True)

        populate(plan, assets_dir)

        thumbnail = _write_thumbnail(staging, assets_dir, plan, banner_path)
        atomic_write_json(os.path.join(staging, "manifest.json"), {
            "name": title,
            "description": (description or "").strip(),
            "thumbnail": thumbnail,
            IconPack.ASSET_MANIFEST_KEY: IconPack.ASSET_MANIFEST_KEY,
        })

        destination = os.path.join(root, folder_name)
        try:
            os.rename(staging, destination)
        except OSError as error:
            raise PackImportError("The pack could not be put in place.") from error
    except BaseException:
        # Every exit that is not the rename leaves the staging tree behind, so
        # drop it. Nothing reads it, because of the dot, but it costs disk.
        _discard_staging(staging)
        raise
    _forget_staging(staging)

    log.info(f"Imported icon pack {title!r} into {folder_name!r} with {len(plan)} icons")
    return folder_name
