"""Build an icon pack out of a zip archive or a folder of pictures.

The store installs a pack by downloading a repository. This module makes one
out of what the user already has, and it writes the same layout, so the pack
chooser reads an imported pack and a store one through one code path. See
pack_family for that layout: a folder under the data path holding a
manifest.json, the thumbnail the manifest names, and the asset folder the
manifest names.

Three rules shape the work.

The pack is registered last. Everything is built inside a staging directory
whose name carries a dot prefix, which is what the pack scanner skips, and a
rename puts it in place once every file is written. A crash, a power cut or a
kill therefore leaves a hidden half-built tree that no reader trusts, and
never a pack folder with some of its icons in it. The next import of the same
name sweeps the leftover.

An archive is untrusted input. Every member name goes through archive_safety
before anything is written, and one refused member refuses the whole archive.
The destination of each file is built here, from the file name and at most one
folder name, and never from the member string, and the built path is checked
against the pack folder before the write.

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
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass

from loguru import logger as log

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

#: The largest archive an import unpacks, measured as the total size the
#: archive says its members take unpacked. An archive that claims more is
#: refused before a byte is written, and a member that writes more than its
#: own entry claims fails the import. A small archive that unpacks into a full
#: disk is the case both of these cover.
MAX_UNPACKED_BYTES = 512 * 1024 * 1024

_COPY_CHUNK = 256 * 1024


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
    filename = _clean_component(parts[-1])
    folder = _clean_component(parts[0]) if len(parts) > 1 else ""
    return _unique_dest(folder, filename, taken)


def _clean_component(name: str) -> str:
    """One path component, with everything that is not a plain name removed."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._()-]", "-", name).strip(" .")
    return cleaned or "file"


def _folder_plan(folder: str) -> list[_PlannedFile]:
    taken: set[str] = set()
    planned: list[_PlannedFile] = []
    for path in sorted(_walk_files(folder)):
        if not _is_importable(path):
            continue
        relative = os.path.relpath(path, folder)
        planned.append(_PlannedFile(_planned_destination(relative, taken), path))
    return planned


def _walk_files(folder: str) -> Iterator[str]:
    # followlinks stays off. A link that points at a directory above the
    # source would copy a tree the user never chose.
    for dirpath, _dirnames, filenames in os.walk(folder, followlinks=False):
        for filename in filenames:
            yield os.path.join(dirpath, filename)


def _archive_plan(zip_path: str) -> list[_PlannedFile]:
    """What to unpack, after the whole archive passed its member check."""
    unsafe = archive_safety.first_unsafe_member(zip_path)
    if unsafe is not None:
        name, reason = unsafe
        log.error(f"Refusing the icon pack archive: {reason}: {name!r}")
        raise PackImportError(
            "This archive names a file outside the folder it unpacks into, so "
            "nothing from it was imported."
        )

    with zipfile.ZipFile(zip_path, "r") as archive:
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

    if os.path.isdir(source):
        planned = _folder_plan(source)
    elif zipfile.is_zipfile(source):
        planned = _archive_plan(source)
    elif os.path.isfile(source):
        raise PackImportError("An icon pack comes from a zip archive or a folder.")
    else:
        raise PackImportError("That archive or folder is not there any more.")

    if not planned:
        raise PackImportError(
            "No pictures the app can show were found, so there is no pack to make."
        )

    root = os.path.join(gl.DATA_PATH, IconPackManager.DATA_DIR)
    os.makedirs(root, exist_ok=True)
    folder_name = _free_folder_name(root, folder_name_for(title))

    staging = os.path.join(root, f".{folder_name}{STAGING_SUFFIX}")
    _remove_tree(staging)
    try:
        assets_dir = os.path.join(staging, IconPack.ASSET_MANIFEST_KEY)
        os.makedirs(assets_dir, exist_ok=True)

        if os.path.isdir(source):
            _copy_planned_files(planned, assets_dir)
        else:
            _extract_planned_files(source, planned, assets_dir)

        thumbnail = _write_thumbnail(staging, assets_dir, planned, banner_path)
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
        # Every exit that is not the rename leaves the staging tree behind,
        # and a leftover holds the whole import. Nothing reads it, because of
        # the dot, but it costs disk until the next import of this name.
        _remove_tree(staging)
        raise

    log.info(f"Imported icon pack {title!r} into {folder_name!r} with {len(planned)} icons")
    return folder_name


def _copy_planned_files(planned: list[_PlannedFile], assets_dir: str) -> None:
    for item in planned:
        target = _checked_target(assets_dir, item.dest_rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(item.source, target)


def _declared_size(archive: zipfile.ZipFile, member: str) -> int:
    """What the archive says this member takes unpacked.

    The whole-archive limit is the sum of these, so each one is also the limit
    for its own member. A number an archive can state is a number it can
    misstate, which is why the copy below counts what it writes.
    """
    return archive.getinfo(member).file_size


def _extract_planned_files(zip_path: str, planned: list[_PlannedFile], assets_dir: str) -> None:
    with zipfile.ZipFile(zip_path, "r") as archive:
        for item in planned:
            limit = _declared_size(archive, item.source)
            target = _checked_target(assets_dir, item.dest_rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # The member is opened by name and written to a path this module
            # built, so the archive chooses what comes out and never where it
            # goes.
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


def _checked_target(assets_dir: str, dest_rel: str) -> str:
    """The absolute path of dest_rel, after one last check on where it lands.

    The destination was built from cleaned components, so this holds nothing
    the earlier checks let through. It is the guard that stays right whatever
    later changes the way a destination is chosen.
    """
    target = os.path.join(assets_dir, dest_rel)
    if not archive_safety.resolved_within(assets_dir, target):
        raise PackImportError("A file in this import would land outside the pack.")
    return target


def _write_thumbnail(staging: str, assets_dir: str, planned: list[_PlannedFile],
                     banner_path: str | None) -> str:
    """Put the pack's thumbnail in place and return the name the manifest holds.

    A pack whose thumbnail is missing reads as invalid and drops out of the
    chooser, so an import always writes one. The user's banner is used when
    there is one, and the first icon otherwise. A raster icon is preferred
    over an svg, because the preview decoder handles one without a library the
    host may not have.
    """
    if banner_path and os.path.isfile(banner_path) and _is_importable(banner_path):
        name = f"thumbnail{os.path.splitext(banner_path)[1].lower()}"
        shutil.copyfile(banner_path, os.path.join(staging, name))
        return name

    chosen = next((item for item in planned if _extension(item.dest_rel) != "svg"), planned[0])
    source = os.path.join(assets_dir, chosen.dest_rel)
    name = f"thumbnail{os.path.splitext(chosen.dest_rel)[1].lower()}"
    shutil.copyfile(source, os.path.join(staging, name))
    return name


def _remove_tree(path: str) -> None:
    """Remove a staging tree, or whatever else took its name."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        try:
            os.remove(path)
        except OSError as error:
            log.warning(f"Could not remove {path}: {error}")
