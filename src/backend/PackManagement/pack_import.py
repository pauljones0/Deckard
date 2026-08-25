"""Build an icon pack out of a zip archive or a folder of pictures.

The store installs a pack by downloading a repository. This module makes one
out of what the user already has, and it writes the same layout, so the pack
chooser reads an imported pack and a store one through one code path. See
pack_family for that layout: a folder under the data path holding a
manifest.json, the thumbnail the manifest names, and the asset folder the
manifest names.

Four rules shape the work.

The pack is registered last. Everything is built inside a staging directory
whose name carries a dot prefix, which is what the pack scanner skips, and a
rename puts it in place once every file is written. A crash, a power cut or a
kill therefore leaves a hidden half-built tree that no reader trusts, and
never a pack folder with some of its icons in it. Each import gets its own
staging directory with a random name, so two imports at once never share one,
and a later import sweeps a tree that a dead import left.

Untrusted input is validated before a byte is written. An archive member whose
name resolves outside the folder it unpacks into fails the whole archive, and a
folder import skips a symlink, refuses a special file, and refuses a file whose
inode carries a second name that could sit outside the folder, so neither
source copies a file the user did not choose. The destination of each file is
built here, from the file name and at most one folder name, and never from the
source string, and the built path is checked against the pack folder before the
write.

An import has a size budget. An archive that declares more than the budget is
refused before a byte is written, and every copy counts what it writes and
stops at the budget, so a folder of huge files or an archive with a forged
size header fills no disk.

What the browser can show is what gets copied. The pack reader lists the asset
folder and the folders one level inside it and stops there, so a file deeper
than that would be invisible; a deeper file lands in the folder that holds it.
A file whose format the app does not render is skipped, because a pack full of
unreadable entries is worse than a smaller pack.
"""
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

#: The suffix of a staging directory. The leading dot is what makes the pack
#: scanner skip it, and the rest tells a person who finds one where it came
#: from.
STAGING_SUFFIX = ".deckard-import"

#: What a pack goes into when the name the user typed leaves nothing usable.
FALLBACK_FOLDER_NAME = "Imported Pack"

#: The longest folder name an import makes. Every filesystem this app runs on
#: takes far more, and a name past this is a paste rather than a name.
MAX_FOLDER_NAME_LENGTH = 64

#: How many folder names an import tries before it gives up. A collision takes
#: a numbered suffix, and a user with this many packs of one name has a
#: different problem.
MAX_NAME_ATTEMPTS = 100

#: The largest an import unpacks, measured as the total size the source says
#: its files take. A source that claims more is refused before a byte is
#: written, and the copy counts what it writes, so a source that grows or lies
#: cannot fill a disk. A folder of huge real pictures and an archive with a
#: forged size header are the cases this covers.
MAX_UNPACKED_BYTES = 512 * 1024 * 1024

_COPY_CHUNK = 256 * 1024

#: The staging directories this session holds open. A sweep spares these, so a
#: live import's tree survives a second import that starts while it runs.
_staging_lock = threading.Lock()
_live_staging: set[str] = set()

#: True while an import runs. The dialog reads it to keep a second import from
#: starting on top of one already in flight. It is set and cleared on the GTK
#: main thread only.
_import_in_flight = False


class PackImportError(Exception):
    """An import that cannot go on. Its text is shown to the user."""


@dataclass(frozen=True)
class _PlannedFile:
    """One file the import will write, and where it comes from.

    dest_rel is relative to the pack's asset folder and is built here. source
    is a path on disk for a folder import and a member name for an archive.
    """

    dest_rel: str
    source: str


def importable_extensions() -> frozenset[str]:
    """The formats an import copies, lowercase and without the dot.

    Every one is a format the app already renders. The image and svg lists
    hold four of them, and the gif sits in the video list because the app
    plays it. The rest of the video list stays out: an icon pack holds
    pictures, and an mp4 in one is a wallpaper in the wrong family.
    """
    extensions = {str(ext).lower() for ext in gl.image_extensions}
    extensions |= {str(ext).lower() for ext in gl.svg_extensions}
    extensions.add("gif")
    return frozenset(extensions)


def _extension(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()


def _is_importable(path: str) -> bool:
    return _extension(path) in importable_extensions()


def folder_name_for(name: str) -> str:
    """A folder name for the pack the user called name.

    The user's text is a title and not a path. This is the one rule that turns
    it into a folder name, and what it returns always holds:

    - one path component, because every character that is not a letter, a
      digit, a space, a dot, a dash or an underscore becomes a dash
    - a first character that is a letter or a digit, because a name that
      starts with a dot is one the pack scanner skips as hidden, and one that
      starts with a dash reads as an option to a command line
    - at most MAX_FOLDER_NAME_LENGTH characters
    - never empty, because a pack still needs somewhere to live
    """
    kept = re.sub(r"[^A-Za-z0-9 ._-]", "-", name.strip())
    kept = re.sub(r"-{2,}", "-", kept)
    kept = kept.strip(" .-_")[:MAX_FOLDER_NAME_LENGTH].strip(" .-_")
    return kept or FALLBACK_FOLDER_NAME


def _free_folder_name(root: str, base: str) -> str:
    """base, or base with a number after it, whichever is free under root.

    A name a pack already holds takes a suffix rather than a refusal. The
    folder name is bookkeeping that no part of the UI shows, the pack keeps
    the title the user typed either way, and a refusal at this point throws
    away everything the user filled in.
    """
    for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
        candidate = base if attempt == 1 else f"{base} ({attempt})"
        if not os.path.lexists(os.path.join(root, candidate)):
            return candidate
    raise PackImportError(f"There are already too many packs called {base!r}.")


def _unique_dest(dest_dir: str, filename: str, taken: set[str]) -> str:
    """A destination relative path inside dest_dir that nothing else took.

    Two source files of one name in different deep folders both land in one
    destination folder, and the second must not overwrite the first.
    """
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
    """Where a source file at relative lands inside the pack's asset folder.

    It keeps the first folder of the path and drops the rest, because the pack
    reader looks one level down and no further. The file name and that one
    folder name are the only parts of the input that reach the destination,
    and both are cleaned, so nothing the input says can move the write.
    """
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts:
        # A relative path of only separators or dots names no file. The
        # extension test upstream already drops such a name, and this keeps
        # the parts[-1] below from an index error if that ever changes.
        raise PackImportError("A file in this import has no name, so nothing was imported.")
    filename = _clean_component(parts[-1])
    folder = _clean_component(parts[0]) if len(parts) > 1 else ""
    return _unique_dest(folder, filename, taken)


def _clean_component(name: str) -> str:
    """One path component, with everything that is not a plain name removed."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._()-]", "-", name).strip(" .")
    return cleaned or "file"


# Folder source


def _folder_plan(folder: str) -> list[_PlannedFile]:
    """What to copy out of a folder, after each file passed its checks.

    _walk_files yields no symlink and descends into no linked directory, so
    every path here is a real file that sits under the chosen folder. The size
    budget is counted at the copy, so it is not summed here.
    """
    taken: set[str] = set()
    planned: list[_PlannedFile] = []
    for path in sorted(_walk_files(folder)):
        if not _is_importable(path):
            continue
        relative = os.path.relpath(path, folder)
        planned.append(_PlannedFile(_planned_destination(relative, taken), path))
    return planned


def _walk_files(folder: str) -> Iterator[str]:
    # followlinks stays off, so os.walk descends into no linked directory, and
    # a file under a linked directory is never yielded. A linked file is still
    # in filenames, so each one is dropped here: a link is not a picture the
    # user put here, and following it would copy a file from wherever it
    # points, which may sit outside the chosen folder. What is left is a real
    # file under the folder, so nothing this yields can escape it.
    for dirpath, _dirnames, filenames in os.walk(folder, followlinks=False):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                continue
            yield full


def _copy_planned_files(planned: list[_PlannedFile], assets_dir: str) -> None:
    """Copy each planned file into the pack's asset folder.

    It refuses a source that is not an ordinary file, so a named pipe or a
    device cannot be read without end, and one whose inode carries more than
    one name, because a second name can sit outside the chosen folder and
    would put that file's bytes in the pack. It counts the bytes it writes
    against the budget, so a source that grew since the plan fills no disk.
    """
    written = 0
    for item in planned:
        try:
            info = os.lstat(item.source)
        except OSError as error:
            raise PackImportError(
                "A file in this folder could not be read, so nothing was imported."
            ) from error
        if not stat.S_ISREG(info.st_mode):
            # A symlink, a pipe or a device: not a picture the user put here.
            raise PackImportError(
                "A file in this folder is not an ordinary picture, so nothing "
                "was imported."
            )
        if info.st_nlink > 1:
            # A hardlink shares its bytes with another name, which can live
            # outside the chosen folder. Refuse it, so the copy reads only
            # files that live under the folder and nowhere else. A picture with
            # a second name is rare, and keeping the folder the one source of
            # what enters the pack is worth refusing it.
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


# Archive source


def _archive_plan(archive: zipfile.ZipFile) -> list[_PlannedFile]:
    """What to unpack, after the whole archive passed its member check.

    The member names are validated first, and one member that resolves outside
    the folder it unpacks into refuses the whole archive: the members before it
    are no more trustworthy than the one that gave itself away. The declared
    total is checked before any write.
    """
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
    """The one folder every member sits in, with its separator, or "".

    An archive made from a directory holds that directory as its only top
    entry. Keeping it would put every icon one level deeper than the author
    laid them out, which moves them all into one subfolder of the pack.

    A member that is the top entry itself is a file rather than a folder, and
    it keeps its name, because the caller strips this prefix only from a name
    that starts with it, separator and all.
    """
    tops = {name.replace("\\", "/").split("/")[0] for name in names}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    return f"{top}/" if top else ""


def _declared_size(archive: zipfile.ZipFile, member: str) -> int:
    """What the archive says this member takes unpacked.

    CPython's ZipExtFile stops reading a member at this size, so the guard in
    _extract_planned_files that compares against it cannot fire on a real
    archive. The guard stays as defence for a future reader that trusts a size
    header, and the test reaches it by understating this number. Remove neither
    believing the other covers a forged header.
    """
    return archive.getinfo(member).file_size


def _extract_planned_files(archive: zipfile.ZipFile, planned: list[_PlannedFile],
                           assets_dir: str) -> None:
    for item in planned:
        try:
            limit = _declared_size(archive, item.source)
        except KeyError as error:
            # The plan and this extraction read the same open archive's member
            # table, so a member the plan named cannot vanish here in normal
            # flow. This stays as defence against a malformed handle that
            # answers namelist and getinfo differently, and refuses through the
            # import contract rather than a bare lookup error.
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
    """The absolute path of dest_rel under base_dir, after one last check.

    Every write an import makes passes through here. The destination was built
    from cleaned components, so this holds nothing the earlier checks let
    through. It is the guard that stays right whatever later changes the way a
    destination is chosen.
    """
    target = os.path.join(base_dir, dest_rel)
    if not archive_safety.resolved_within(base_dir, target):
        raise PackImportError("A file in this import would land outside the pack.")
    return target


# Thumbnail


def _is_decodable(path: str) -> bool:
    """Whether the app can turn path into a picture.

    A file the manifest names as the thumbnail but that does not decode leaves
    the pack showing a blank tile, so a chosen banner is read here before it is
    trusted. An svg is markup rather than a raster, so it is checked for the
    tag every svg carries rather than decoded.
    """
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
    """Put the pack's thumbnail in place and return the name the manifest holds.

    A pack whose thumbnail is missing reads as invalid and drops out of the
    chooser, so an import always writes one. The user's banner is used when it
    is a picture the app can show, and the first icon otherwise. A raster icon
    is preferred over an svg, because the preview decoder handles one without a
    library the host may not have.

    The write goes through _checked_target like every other, so the thumbnail
    is no exception to the rule that a write lands inside the pack.
    """
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


# Staging


def _new_staging(root: str) -> str:
    """A fresh, empty staging directory under root, tracked as live.

    The name is random, so two imports never share one and the second cannot
    delete the first's tree. The dot prefix keeps the pack scanner out of it.

    The create and the track happen under the one lock the sweep takes, so a
    sweep cannot run between them and read the new directory as an untracked
    leftover.
    """
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
    """Remove staging trees a dead import left under root.

    A tree this session still holds open is spared, so a second import that
    starts while the first runs cannot delete the first's work. A leftover from
    a crashed run matches the staging name and no live tree, so it goes.

    The whole sweep, from the snapshot of the live set through the listing to
    the removals, runs under the one lock _new_staging takes. A new staging
    directory is therefore either fully created and tracked before the sweep
    reads the disk, or created after the sweep finished, and never seen by the
    sweep as an untracked leftover in between.
    """
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
    """Remove a staging tree, or whatever else took its name. Best effort.

    A tree this cannot remove, such as one left read-only by a crash, stays on
    disk and costs space until it can go. It blocks nothing: an import builds
    into a directory of its own with a fresh random name, so a stuck leftover
    never sits where a new import needs to write.
    """
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        try:
            os.remove(path)
        except OSError as error:
            log.warning(f"Could not remove {path}: {error}")


# The import


def import_is_running() -> bool:
    """Whether an import is in flight. Read on the GTK main thread only."""
    return _import_in_flight


def set_import_running(running: bool) -> None:
    """Mark the in-flight state. Set on the GTK main thread only."""
    global _import_in_flight
    _import_in_flight = running


def import_icon_pack(source: str, name: str, description: str = "",
                     banner_path: str | None = None) -> str:
    """Make an icon pack out of source and return the folder it went into.

    source is a .zip archive or a folder. name is what the pack is called in
    the chooser. Raises PackImportError with a sentence for the user when the
    import cannot go on, and leaves nothing behind when it does.

    This reads and writes files, so it runs off the main thread.
    """
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
        # is_zipfile passes a truncated download whose central directory is
        # intact; the read then fails here. The staging tree is already gone,
        # because _build_pack cleans it before this catch sees the error.
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
    """Stage a pack out of a plan and rename it into place.

    populate writes the plan's files into the pack's asset folder. Everything
    else here is common to a folder import and an archive one.
    """
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
