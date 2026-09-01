"""Import icon packs into the store layout from archives and folders.
Covers containment, staging, visibility, and reader-compatible assets."""
import fixtures  # noqa: F401  (must be first: see fixtures.py docstring)

import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import zipfile  # noqa: E402

import globals as gl  # noqa: E402

from PIL import Image  # noqa: E402

from locales.LocaleManager import LocaleManager  # noqa: E402

from src.backend import archive_safety  # noqa: E402
from src.backend.IconPackManagement.IconPackManager import IconPackManager  # noqa: E402
from src.backend.PackManagement import pack_import  # noqa: E402
from src.backend.PackManagement.pack_import import PackImportError  # noqa: E402


PACKS_ROOT = os.path.join(gl.DATA_PATH, IconPackManager.DATA_DIR)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(REPO_ROOT, "locales", "locales.csv")

UI_SOURCES = (
    os.path.join(REPO_ROOT, "src", "windows", "AssetManager", "IconPacks", "ImportDialog.py"),
    os.path.join(REPO_ROOT, "src", "windows", "AssetManager", "IconPacks", "PackChooser.py"),
)


def scratch(name: str) -> str:
    path = os.path.join(gl.DATA_PATH, "import-sources", name)
    os.makedirs(path, exist_ok=True)
    return path


def write_png(path: str, colour: tuple = (10, 120, 200, 255)) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGBA", (16, 16), colour).save(path, format="PNG")
    return path


def png_bytes(colour: tuple = (200, 40, 60, 255)) -> bytes:
    path = os.path.join(gl.DATA_PATH, "import-sources", "_scratch.png")
    write_png(path, colour)
    with open(path, "rb") as handle:
        return handle.read()


def make_zip(name: str, members: dict) -> str:
    """An archive whose members map a name to bytes."""
    path = os.path.join(scratch("zips"), name)
    with zipfile.ZipFile(path, "w") as archive:
        for member, payload in members.items():
            archive.writestr(member, payload)
    return path


def packs() -> dict:
    return IconPackManager().get_icon_packs()


def pack_folders() -> list:
    if not os.path.isdir(PACKS_ROOT):
        return []
    return sorted(os.listdir(PACKS_ROOT))


def icon_names(pack) -> set:
    return {os.path.basename(icon.path) for icon in pack.get_icons()}


def clear_packs() -> None:
    shutil.rmtree(PACKS_ROOT, ignore_errors=True)


def check_renderable_extensions() -> None:
    """Require import formats to come from the app's renderable format lists."""
    extensions = pack_import.importable_extensions()
    assert extensions == {"png", "jpg", "jpeg", "svg", "gif"}, (
        f"the importable set drifted: {sorted(extensions)}"
    )
    renders = ({e.lower() for e in gl.image_extensions}
               | {e.lower() for e in gl.svg_extensions}
               | {e.lower() for e in gl.video_extensions})
    unrenderable = extensions - renders
    assert not unrenderable, f"the import copies formats the app cannot show: {unrenderable}"
    assert "gif" in {e.lower() for e in gl.video_extensions}, (
        "the gif is in the set because the app plays it; that stopped being true"
    )
    assert "mp4" not in extensions, "an icon pack holds pictures, not films"
    assert "webp" not in extensions, (
        "webp is in none of the app's format lists, so a webp icon would show blank"
    )
    print("PASS: the import copies only formats the app renders")


def check_member_rule() -> None:
    """The member names an archive may carry, and the ones it may not."""
    for name in ("icons/a.png", "a.png", "deep/nested/a.png", "a..b/x.png"):
        assert archive_safety.unsafe_member_reason(name) is None, (
            f"{name!r} is an ordinary member and must be accepted"
        )
    for name in ("/etc/passwd", "../a.png", "../../a.png", "icons/../../a.png",
                 "\\windows\\a.png", "C:/a.png", ".."):
        assert archive_safety.unsafe_member_reason(name) is not None, (
            f"{name!r} names a path outside the unpack folder and must be refused"
        )
    print("PASS: the member rule accepts ordinary names and refuses the rest")


def check_resolved_within() -> None:
    base = scratch("within")
    assert archive_safety.resolved_within(base, os.path.join(base, "a", "b.png"))
    assert archive_safety.resolved_within(base, base)
    assert not archive_safety.resolved_within(base, os.path.dirname(base))
    assert not archive_safety.resolved_within(base, base + "-sibling"), (
        "a sibling whose name starts with the base name is outside it"
    )
    print("PASS: the write check compares whole path components")


def check_archive_with_nested_folders() -> None:
    clear_packs()
    archive = make_zip("nested.zip", {
        "top.png": png_bytes(),
        "arrows/left.png": png_bytes(),
        "arrows/right.png": png_bytes(),
        "arrows/small/tiny.png": png_bytes(),
        "notes.txt": b"not a picture",
    })
    folder = pack_import.import_icon_pack(archive, "Nested Pack", "A test pack")

    found = packs()
    assert list(found) == [folder], f"the manager sees {list(found)}, expected [{folder!r}]"
    pack = found[folder]
    assert pack.name == "Nested Pack", f"the pack calls itself {pack.name!r}"
    assert pack.is_valid, "the imported pack is not readable by the pack reader"

    assert icon_names(pack) == {"top.png", "left.png", "right.png", "tiny.png"}, (
        f"the pack holds {sorted(icon_names(pack))}"
    )
    assert "Base" in pack.assets_by_folder and "arrows" in pack.assets_by_folder, (
        f"the folder structure is {sorted(pack.assets_by_folder)}"
    )
    deep = [icon for icon in pack.get_icons() if os.path.basename(icon.path) == "tiny.png"][0]
    assert os.path.basename(os.path.dirname(deep.path)) == "arrows", (
        f"a file two folders down must land in the folder that holds it, it is at {deep.path}"
    )

    manifest = pack.get_manifest()
    assert manifest["description"] == "A test pack"
    assert manifest["icons"] == "icons", "the manifest must name the asset folder the store names"
    thumbnail = pack.get_thumbnail_path()
    assert thumbnail is not None and thumbnail.exists(), (
        "a pack with no thumbnail on disk reads as invalid and drops out of the chooser"
    )
    print("PASS: an archive with nested folders becomes a readable pack")


def check_wrapper_folder_is_stripped() -> None:
    clear_packs()
    archive = make_zip("wrapped.zip", {
        "my-icons-main/a.png": png_bytes(),
        "my-icons-main/b.png": png_bytes(),
        "my-icons-main/arrows/c.png": png_bytes(),
    })
    folder = pack_import.import_icon_pack(archive, "Wrapped")
    pack = packs()[folder]
    assert "Base" in pack.assets_by_folder, (
        f"the one top folder of the archive must go, the structure is "
        f"{sorted(pack.assets_by_folder)}"
    )
    assert "arrows" in pack.assets_by_folder
    assert "my-icons-main" not in pack.assets_by_folder
    print("PASS: an archive built from one folder does not gain a level")


def check_flat_archive_single_picture() -> None:
    """Keep a top-level picture because it is not a wrapper folder."""
    clear_packs()
    archive = make_zip("flat.zip", {"only.png": png_bytes()})
    folder = pack_import.import_icon_pack(archive, "Flat Pack")
    pack = packs()[folder]
    assert icon_names(pack) == {"only.png"}, f"the pack holds {sorted(icon_names(pack))}"
    print("PASS: an archive of one top-level picture keeps it")


def check_oversized_archive_is_refused() -> None:
    """An archive that claims more than the limit is refused before a write."""
    clear_packs()
    archive = make_zip("big.zip", {"a.png": png_bytes()})
    real_limit = pack_import.MAX_UNPACKED_BYTES
    pack_import.MAX_UNPACKED_BYTES = 1
    try:
        pack_import.import_icon_pack(archive, "Big Pack")
    except PackImportError as error:
        assert str(error).strip(), "the refusal must carry a sentence for the user"
    else:
        raise AssertionError("an archive over the unpacked-size limit was imported")
    finally:
        pack_import.MAX_UNPACKED_BYTES = real_limit
    assert pack_folders() == [], f"the refused archive left {pack_folders()}"
    print("PASS: an archive that claims too much is refused")


def check_write_target_is_checked() -> None:
    """Refuse a write destination outside the pack at the final guard."""
    assets_dir = scratch("target-check")
    inside = pack_import._checked_target(assets_dir, os.path.join("arrows", "a.png"))
    assert inside.startswith(assets_dir), inside
    for escaping in (os.path.join("..", "a.png"), os.path.join("..", "..", "a.png")):
        try:
            pack_import._checked_target(assets_dir, escaping)
        except PackImportError:
            continue
        raise AssertionError(f"a destination of {escaping!r} was accepted")
    print("PASS: a destination outside the pack is refused before the write")


def check_archive_rejects_escaping_member_path() -> None:
    clear_packs()
    archive = make_zip("escape.zip", {
        "good.png": png_bytes(),
        "../escaped.png": png_bytes(),
    })
    try:
        pack_import.import_icon_pack(archive, "Escaping Pack")
    except PackImportError as error:
        assert str(error).strip(), "the refusal must carry a sentence for the user"
    else:
        raise AssertionError("an archive naming a path outside the pack was imported")

    assert pack_folders() == [], (
        f"the refused archive left something behind: {pack_folders()}"
    )
    assert packs() == {}, "the refused archive registered a pack"
    assert not os.path.exists(os.path.join(os.path.dirname(PACKS_ROOT), "escaped.png")), (
        "a file landed outside the packs folder"
    )
    print("PASS: one escaping member refuses the whole archive and writes nothing")


def check_archive_rejects_absolute_member_path() -> None:
    clear_packs()
    archive = make_zip("absolute.zip", {"/tmp/escaped.png": png_bytes(), "ok.png": png_bytes()})
    try:
        pack_import.import_icon_pack(archive, "Absolute Pack")
    except PackImportError:
        pass
    else:
        raise AssertionError("an archive naming an absolute path was imported")
    assert pack_folders() == [], f"the refused archive left {pack_folders()}"
    print("PASS: an absolute member refuses the whole archive")


def check_empty_archive_is_refused() -> None:
    clear_packs()
    empty = make_zip("empty.zip", {})
    text_only = make_zip("text.zip", {"readme.txt": b"nothing to see"})
    for archive, source_description in (
        (empty, "an empty archive"), (text_only, "an archive of no pictures")
    ):
        try:
            pack_import.import_icon_pack(archive, "Empty Pack")
        except PackImportError as error:
            assert str(error).strip(), (
                f"{source_description} must be refused with a sentence"
            )
        else:
            raise AssertionError(f"{source_description} became a pack")
    assert pack_folders() == [], f"a refused import left {pack_folders()}"
    print("PASS: an archive with no pictures is refused with a message")


def check_lying_member_size_is_refused() -> None:
    """A member that unpacks to more than its entry says fails the import."""
    clear_packs()
    archive = make_zip("liar.zip", {"a.png": png_bytes(), "b.png": png_bytes()})
    # An understated size, which is what an archive built to fill a disk
    # carries. The reader stays honest, so only the limit moves.
    real_declared = pack_import._declared_size
    pack_import._declared_size = lambda _archive, _member: 1
    try:
        pack_import.import_icon_pack(archive, "Liar Pack")
    except PackImportError:
        pass
    else:
        raise AssertionError("a member that unpacked past its declared size was accepted")
    finally:
        pack_import._declared_size = real_declared

    assert pack_folders() == [], f"the refused import left {pack_folders()}"
    print("PASS: a member that unpacks past its declared size fails the import")


def check_folder_import() -> None:
    clear_packs()
    source = scratch("plain-folder")
    write_png(os.path.join(source, "one.png"))
    write_png(os.path.join(source, "shapes", "two.png"))
    write_png(os.path.join(source, "shapes", "deeper", "three.png"))
    with open(os.path.join(source, "notes.txt"), "w") as handle:
        handle.write("skip me")

    folder = pack_import.import_icon_pack(source, "Folder Pack")
    pack = packs()[folder]
    assert icon_names(pack) == {"one.png", "two.png", "three.png"}, (
        f"the pack holds {sorted(icon_names(pack))}"
    )
    assert pack.is_valid
    print("PASS: a folder of pictures becomes a readable pack")


def check_internal_folder_name_collision() -> None:
    """Two source files of one name in different deep folders both survive."""
    clear_packs()
    source = scratch("collide-folder")
    write_png(os.path.join(source, "set", "a", "icon.png"))
    write_png(os.path.join(source, "set", "b", "icon.png"))
    folder = pack_import.import_icon_pack(source, "Collide Pack")
    pack = packs()[folder]
    assert len(pack.get_icons()) == 2, (
        f"one file overwrote the other, the pack holds "
        f"{sorted(icon_names(pack))}"
    )
    print("PASS: two source files of one name both reach the pack")


def check_banner_is_used_when_given() -> None:
    clear_packs()
    source = scratch("banner-folder")
    write_png(os.path.join(source, "icon.png"), colour=(1, 2, 3, 255))
    banner = write_png(os.path.join(scratch("banner-src"), "banner.png"), colour=(9, 9, 9, 255))

    folder = pack_import.import_icon_pack(source, "Banner Pack", banner_path=banner)
    pack = packs()[folder]
    thumbnail = pack.get_thumbnail_path()
    assert thumbnail is not None and thumbnail.exists()
    with open(thumbnail, "rb") as handle:
        stored = handle.read()
    with open(banner, "rb") as handle:
        assert stored == handle.read(), "the thumbnail is not the banner the user chose"
    print("PASS: the chosen banner becomes the pack thumbnail")


def check_folder_import_skips_symlink() -> None:
    """Do not follow linked files, which os.walk lists with followlinks disabled."""
    clear_packs()
    outside = write_png(os.path.join(scratch("outside"), "secret.png"), colour=(7, 7, 7, 255))
    with open(os.path.join(scratch("secret-text"), "secret.txt"), "w") as handle:
        handle.write("a private thing")
    secret_text = os.path.join(scratch("secret-text"), "secret.txt")

    source = scratch("link-folder")
    write_png(os.path.join(source, "real.png"))
    # A link named like a picture, pointing at a file outside the folder.
    link = os.path.join(source, "stolen.png")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(outside, link)
    # A link whose target is not even a picture, named like one.
    text_link = os.path.join(source, "note.png")
    if os.path.lexists(text_link):
        os.remove(text_link)
    os.symlink(secret_text, text_link)
    # A link that points at a file inside the folder. It resolves within the
    # folder, so only the skip of a link keeps it out of the pack.
    inside_link = os.path.join(source, "inside.png")
    if os.path.lexists(inside_link):
        os.remove(inside_link)
    os.symlink(os.path.join(source, "real.png"), inside_link)

    folder = pack_import.import_icon_pack(source, "Link Pack")
    pack = packs()[folder]
    assert icon_names(pack) == {"real.png"}, (
        f"the import followed a link, the pack holds "
        f"{sorted(icon_names(pack))}"
    )
    for icon in pack.get_icons():
        assert not os.path.islink(icon.path), "a link was copied into the pack as a link"
    print("PASS: a folder import copies no file a link points at")


def check_folder_import_rejects_special_file() -> None:
    """A named pipe named like a picture is refused, not read forever."""
    if not hasattr(os, "mkfifo"):
        print("SKIP: no mkfifo on this platform")
        return
    clear_packs()
    source = scratch("fifo-folder")
    write_png(os.path.join(source, "ok.png"))
    fifo = os.path.join(source, "pipe.png")
    if os.path.lexists(fifo):
        os.remove(fifo)
    os.mkfifo(fifo)
    try:
        pack_import.import_icon_pack(source, "Fifo Pack")
    except PackImportError as error:
        assert str(error).strip(), "the refusal must carry a sentence"
    else:
        raise AssertionError("a folder holding a named pipe became a pack")
    finally:
        os.remove(fifo)
    assert pack_folders() == [], f"the refused import left {pack_folders()}"
    print("PASS: a special file in a folder is refused, not read")


def check_folder_import_rejects_external_hardlink() -> None:
    """Refuse hardlinks because a regular-file check cannot contain their data."""
    clear_packs()
    secret = os.path.join(scratch("hardlink-secret"), "private.png")
    write_png(secret, colour=(2, 2, 2, 255))
    with open(secret, "rb") as handle:
        secret_bytes = handle.read()

    source = scratch("hardlink-folder")
    write_png(os.path.join(source, "real.png"))
    linked = os.path.join(source, "shared.png")
    if os.path.lexists(linked):
        os.remove(linked)
    os.link(secret, linked)  # a hard link: one inode, two names
    assert not os.path.islink(linked), "the probe must be a hard link, not a symlink"
    assert os.stat(linked).st_nlink > 1, "the probe must share its inode"

    try:
        pack_import.import_icon_pack(source, "Hardlink Pack")
    except PackImportError as error:
        assert str(error).strip(), "the refusal must carry a sentence"
    else:
        raise AssertionError("a folder holding a hardlink to an outside file became a pack")
    finally:
        os.remove(linked)

    assert pack_folders() == [], f"the refused import left {pack_folders()}"
    # Verify that no pack file contains the outside inode's data.
    for dirpath, _dirs, files in os.walk(PACKS_ROOT):
        for name in files:
            with open(os.path.join(dirpath, name), "rb") as handle:
                assert handle.read() != secret_bytes, (
                    f"the outside file's bytes reached {os.path.join(dirpath, name)!r}"
                )
    print("PASS: a hardlink to a file outside the folder is refused")


def check_folder_import_refuses_over_budget() -> None:
    """A folder whose pictures exceed the budget is refused before a write."""
    clear_packs()
    source = scratch("big-folder")
    write_png(os.path.join(source, "a.png"))
    real_limit = pack_import.MAX_UNPACKED_BYTES
    pack_import.MAX_UNPACKED_BYTES = 1
    try:
        pack_import.import_icon_pack(source, "Big Folder Pack")
    except PackImportError as error:
        assert str(error).strip(), "the refusal must carry a sentence"
    else:
        raise AssertionError("a folder over the size budget became a pack")
    finally:
        pack_import.MAX_UNPACKED_BYTES = real_limit
    assert pack_folders() == [], f"the refused import left {pack_folders()}"
    print("PASS: a folder over the size budget is refused")


def check_damaged_archive_is_refused() -> None:
    """Wrap a corrupt member read in PackImportError while the index stays valid."""
    clear_packs()
    good = make_zip("whole.zip", {"a.png": png_bytes()})
    damaged = os.path.join(scratch("zips"), "damaged.zip")
    with open(good, "rb") as handle:
        archive_bytes = bytearray(handle.read())
    # Flip bytes in the member's data region, well before the central
    # directory at the tail, so is_zipfile still passes and the read fails.
    for offset in range(40, min(80, len(archive_bytes) - 60)):
        archive_bytes[offset] ^= 0xFF
    with open(damaged, "wb") as handle:
        handle.write(archive_bytes)
    assert zipfile.is_zipfile(damaged), "the corruption must leave a readable central directory"

    try:
        pack_import.import_icon_pack(damaged, "Damaged Pack")
    except PackImportError as error:
        assert str(error).strip(), "a damaged archive must be refused with a sentence"
    except Exception as error:
        raise AssertionError(
            f"a damaged archive raised {type(error).__name__}, not PackImportError"
        )
    else:
        raise AssertionError("a damaged archive was imported as a pack")
    assert pack_folders() == [], f"a damaged archive left {pack_folders()}"
    print("PASS: a damaged archive raises the import contract's error")


def check_undecodable_banner_falls_back() -> None:
    """A banner that is not a picture is not used; a real icon is."""
    clear_packs()
    source = scratch("banner-fallback")
    write_png(os.path.join(source, "icon.png"), colour=(3, 4, 5, 255))
    fake_banner = os.path.join(scratch("fake-banner"), "banner.png")
    with open(fake_banner, "w") as handle:
        handle.write("this is text, not a picture")

    folder = pack_import.import_icon_pack(source, "Fallback Banner Pack", banner_path=fake_banner)
    pack = packs()[folder]
    assert pack.is_valid, "the pack must stay valid when the banner does not decode"
    thumbnail = pack.get_thumbnail_path()
    assert thumbnail is not None and thumbnail.exists()
    with open(thumbnail, "rb") as handle:
        stored = handle.read()
    with open(fake_banner, "rb") as handle:
        assert stored != handle.read(), (
            "an undecodable banner was used as the thumbnail, leaving a blank tile"
        )
    print("PASS: an undecodable banner is dropped for a real icon")


def check_staging_is_unique_per_run() -> None:
    """Give each import a staging path that another import cannot delete."""
    os.makedirs(PACKS_ROOT, exist_ok=True)
    first = pack_import._new_staging(PACKS_ROOT)
    second = pack_import._new_staging(PACKS_ROOT)
    try:
        assert first != second, "two staging directories shared one path"
        for path in (first, second):
            name = os.path.basename(path)
            assert name.startswith("."), f"{name!r} is not hidden from the pack scanner"
            assert name.endswith(pack_import.STAGING_SUFFIX), f"{name!r} is not a staging name"
            assert os.path.isdir(path), f"{path!r} is not a real directory"
    finally:
        pack_import._discard_staging(first)
        pack_import._discard_staging(second)
    print("PASS: each import gets its own staging directory")


def check_sweep_preserves_live_tree() -> None:
    """A sweep removes a dead import's tree and spares a live one."""
    os.makedirs(PACKS_ROOT, exist_ok=True)
    live = pack_import._new_staging(PACKS_ROOT)
    stale = os.path.join(PACKS_ROOT, f".stale{pack_import.STAGING_SUFFIX}")
    write_png(os.path.join(stale, "icons", "left.png"))
    try:
        pack_import._sweep_stale_staging(PACKS_ROOT)
        assert os.path.isdir(live), (
            "the sweep deleted a live import's staging tree"
        )
        assert not os.path.exists(stale), (
            "the sweep left a dead import's staging tree behind"
        )
    finally:
        pack_import._discard_staging(live)
        pack_import._remove_tree(stale)
    print("PASS: a sweep spares a live tree and removes a dead one")


def check_wedged_staging_recovery() -> None:
    """Use a unique staging path when a read-only leftover survives the sweep."""
    clear_packs()
    os.makedirs(PACKS_ROOT, exist_ok=True)
    folder_name = pack_import.folder_name_for("Wedged Pack")
    wedged = os.path.join(PACKS_ROOT, f".{folder_name}{pack_import.STAGING_SUFFIX}")
    write_png(os.path.join(wedged, "icons", "half.png"))
    os.chmod(wedged, 0o500)  # read-only: the sweep cannot remove its contents
    try:
        source = scratch("after-wedge")
        write_png(os.path.join(source, "whole.png"))
        folder = pack_import.import_icon_pack(source, "Wedged Pack")
        pack = packs()[folder]
        assert pack.is_valid, "a wedged leftover blocked a new import of the same name"
        assert icon_names(pack) == {"whole.png"}, (
            f"the new pack picked up a file from the leftover: {sorted(icon_names(pack))}"
        )
    finally:
        os.chmod(wedged, 0o700)
        pack_import._remove_tree(wedged)
    print("PASS: a leftover that cannot be removed does not wedge a name")


def check_reload_replaces_grid() -> None:
    """Remove the old pack grid before reload builds its replacement."""
    from src.windows.AssetManager.GenericAssetChooser import GenericPackChooserPage

    removed: list = []
    started: list = []
    loading: list = []

    class _Page:
        _build_running = False
        build_finished = True
        build_failed = False

        def __init__(self):
            self.pack_flow = object()
            self.scrolled_box = _Bag(remove=removed.append)

        def set_loading(self, value):
            loading.append(value)

        def start_build(self):
            started.append(True)
            return True

    page = _Page()
    old_grid = page.pack_flow
    GenericPackChooserPage.reload(page)
    assert removed == [old_grid], (
        f"reload must remove the old grid, it removed {removed}"
    )
    assert page.pack_flow is None, "reload left the old grid attached"
    assert started == [True], "reload did not start a rebuild"
    assert loading == [True], "reload did not show the spinner"

    # A reload while a build runs must not start a second one over it.
    page.pack_flow = object()
    page._build_running = True
    removed.clear()
    started.clear()
    GenericPackChooserPage.reload(page)
    assert removed == [] and started == [], (
        "reload started a second build while one was already running"
    )
    print("PASS: reload removes the old grid and does not stack builds")


def check_name_collision_suffix() -> None:
    clear_packs()
    first_source = scratch("dup-a")
    write_png(os.path.join(first_source, "a.png"))
    second_source = scratch("dup-b")
    write_png(os.path.join(second_source, "b.png"))

    first = pack_import.import_icon_pack(first_source, "Studio Icons")
    second = pack_import.import_icon_pack(second_source, "Studio Icons")

    assert first != second, "the second import took the folder of the first"
    found = packs()
    assert set(found) == {first, second}, f"the manager sees {sorted(found)}"
    assert icon_names(found[first]) == {"a.png"}, "the first pack lost its icons"
    assert icon_names(found[second]) == {"b.png"}, "the second pack lost its icons"
    assert found[first].name == found[second].name == "Studio Icons", (
        "the folder suffix must not change the name the user typed"
    )
    print("PASS: a second pack of one name takes its own folder and keeps the name")


def check_pack_folder_name_safety() -> None:
    """Whatever the user types, the folder name holds the same four rules."""
    names = (
        "../../escape",
        "/etc/passwd",
        ".hidden",
        "-dashed",
        "normal name",
        "...",
        "",
        "   ",
        "x" * 400,
        "\x00null",
        "a\nb",
        "Stüdio Icons",
    )
    for name in names:
        folder = pack_import.folder_name_for(name)
        assert folder, f"{name!r} produced an empty folder name"
        assert os.sep not in folder and "/" not in folder and "\\" not in folder, (
            f"{name!r} produced {folder!r}, which is more than one path component"
        )
        assert folder[0].isalnum(), (
            f"{name!r} produced {folder!r}: a name that starts with a dot is hidden "
            f"from the pack scanner, and one that starts with a dash reads as an option"
        )
        assert len(folder) <= pack_import.MAX_FOLDER_NAME_LENGTH, (
            f"{name!r} produced a folder name of {len(folder)} characters"
        )
        assert ".." not in folder, f"{name!r} produced {folder!r}"

    assert pack_import.folder_name_for("...") == pack_import.FALLBACK_FOLDER_NAME, (
        "a name that keeps nothing usable must fall back"
    )
    assert pack_import.folder_name_for("Studio Icons") == "Studio Icons", (
        "an ordinary name must reach the folder unchanged"
    )
    print("PASS: a pack folder name is always one safe visible component")


def check_pack_name_required() -> None:
    source = scratch("named")
    write_png(os.path.join(source, "a.png"))
    for name in ("", "   "):
        try:
            pack_import.import_icon_pack(source, name)
        except PackImportError:
            pass
        else:
            raise AssertionError(f"a pack was imported under the name {name!r}")
    print("PASS: an import without a name is refused")


def check_missing_source_is_refused() -> None:
    for source, source_description in (
        (os.path.join(gl.DATA_PATH, "not-there.zip"), "a path that is not there"),
        (write_png(os.path.join(scratch("lonely"), "single.png")), "a single picture"),
    ):
        try:
            pack_import.import_icon_pack(source, "Missing Pack")
        except PackImportError as error:
            assert str(error).strip(), (
                f"{source_description} must be refused with a sentence"
            )
        else:
            raise AssertionError(f"{source_description} became a pack")
    print("PASS: a missing source and a lone file are refused")


def check_failed_import_leaves_no_registration() -> None:
    """A failure part way through leaves no pack and no staging tree."""
    clear_packs()
    source = scratch("fail-folder")
    write_png(os.path.join(source, "a.png"))

    real_write = pack_import.atomic_write_json

    def exploding(*args, **kwargs):
        raise OSError("the disk went away")

    pack_import.atomic_write_json = exploding
    try:
        pack_import.import_icon_pack(source, "Doomed Pack")
    except PackImportError as error:
        # The write error is wrapped in the import contract's error, never
        # raised raw past the boundary.
        assert str(error).strip(), "the wrapped write error must carry a sentence"
    except Exception as error:
        raise AssertionError(
            f"a failed write raised {type(error).__name__}, not PackImportError"
        )
    else:
        raise AssertionError("an import whose manifest write failed reported success")
    finally:
        pack_import.atomic_write_json = real_write

    assert packs() == {}, "a failed import registered a pack"
    assert pack_folders() == [], (
        f"a failed import left files behind: {pack_folders()}"
    )
    print("PASS: a failed import registers no pack and leaves nothing behind")


def check_killed_import_cleanup() -> None:
    """Keep an interrupted staging tree hidden until the next import sweeps it."""
    clear_packs()
    os.makedirs(PACKS_ROOT, exist_ok=True)
    folder_name = pack_import.folder_name_for("Killed Pack")
    staging = os.path.join(PACKS_ROOT, f".{folder_name}{pack_import.STAGING_SUFFIX}")
    write_png(os.path.join(staging, "icons", "half.png"))

    assert packs() == {}, (
        "a tree left by a killed import must not read as a pack"
    )
    assert pack_import.folder_name_for("Killed Pack") == folder_name

    source = scratch("after-kill")
    write_png(os.path.join(source, "whole.png"))
    folder = pack_import.import_icon_pack(source, "Killed Pack")
    assert folder == folder_name, (
        f"the leftover took the name: the new pack went into {folder!r}"
    )
    assert not os.path.exists(staging), "the next import must sweep the leftover"
    assert icon_names(packs()[folder]) == {"whole.png"}, (
        "the new pack picked up a file from the leftover"
    )
    print("PASS: a killed import leaves nothing a reader trusts, and is swept")


def check_import_store_layout() -> None:
    """Write the store layout so the chooser needs only one reader."""
    clear_packs()
    source = scratch("layout-folder")
    write_png(os.path.join(source, "a.png"))
    folder = pack_import.import_icon_pack(source, "Layout Pack")
    pack_dir = os.path.join(PACKS_ROOT, folder)

    entries = sorted(os.listdir(pack_dir))
    assert "manifest.json" in entries, f"no manifest in {entries}"
    assert "icons" in entries and os.path.isdir(os.path.join(pack_dir, "icons")), (
        f"no icons folder in {entries}"
    )
    manifest = packs()[folder].get_manifest()
    assert set(manifest) == {"name", "description", "thumbnail", "icons"}, (
        f"the manifest carries {sorted(manifest)}"
    )
    assert os.path.isfile(os.path.join(pack_dir, manifest["thumbnail"])), (
        "the manifest names a thumbnail that is not there"
    )
    print("PASS: an imported pack is laid out the way an installed one is")


class _Bag:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def check_every_label_key_is_filled() -> None:
    """Require every import UI key in each shipped locale.
    LocaleManager otherwise displays the untranslated key."""
    keys = set()
    for source_path in UI_SOURCES:
        with open(source_path) as source_file:
            keys |= set(re.findall(r'gl\.lm\.get\("([^"]+)"\)', source_file.read()))
    assert len(keys) >= 12, f"the import UI asks for only {sorted(keys)}"

    locale_manager = LocaleManager(CSV_PATH)
    assert len(locale_manager.available_locales) >= 5, (
        f"expected at least the five shipped locales, found {locale_manager.available_locales}"
    )
    for key in sorted(keys):
        row = locale_manager.locale_data.get(key)
        assert row is not None, f"locales.csv carries no row for {key}"
        for language in locale_manager.available_locales:
            assert row.get(language, "").strip(), f"{language} has no label for {key}"
    print(f"PASS: all {len(keys)} import labels are filled for every shipped locale")


def check_import_dialog_construction() -> None:
    """Build the real dialog and require a name and source before import."""
    if not fixtures.has_usable_display():
        print("SKIP: no usable display; the import dialog is not built")
        return

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    Adw.init()
    # The real locale manager, so the dialog shows the labels it ships with.
    gl.lm = LocaleManager(CSV_PATH)
    from src.windows.AssetManager.IconPacks.ImportDialog import ImportPackDialog

    window = Gtk.Window()
    dialog = ImportPackDialog(_Bag(asset_manager=window))
    try:
        assert dialog.get_response_enabled("import") is False, (
            "a fresh dialog has neither a name nor a source, so the import must be off"
        )
        dialog.name_row.set_text("Studio")
        assert dialog.get_response_enabled("import") is False, (
            "a name with nothing to read is not enough to import"
        )

        source = scratch("dialog-source")
        write_png(os.path.join(source, "a.png"))
        dialog.set_source(source)
        assert dialog.get_response_enabled("import") is True, (
            "a name and a source together must enable the import"
        )
        assert dialog.source_row.get_subtitle() == source, (
            f"the row shows {dialog.source_row.get_subtitle()!r}"
        )
        assert dialog.name_row.get_text() == "Studio", (
            f"choosing a source overwrote the name the user typed with "
            f"{dialog.name_row.get_text()!r}"
        )

        dialog.name_row.set_text("   ")
        assert dialog.get_response_enabled("import") is False, (
            "a name of spaces is no name"
        )

        # The refusal surface. A dialog dismissed by the Escape key or the
        # window close must not start an import, so both resolve to cancel.
        assert dialog.get_default_response() == "cancel", (
            f"the default response must be cancel, it is {dialog.get_default_response()!r}"
        )
        assert dialog.get_close_response() == "cancel", (
            f"the close response must be cancel, it is {dialog.get_close_response()!r}"
        )

        # A source with no name of its own names the pack after itself.
        fresh = ImportPackDialog(_Bag(asset_manager=window))
        fresh.set_source(source)
        assert fresh.name_row.get_text() == os.path.basename(source), (
            f"the dialog named the pack {fresh.name_row.get_text()!r}"
        )
        fresh.destroy()

        _check_import_response_starts_worker(dialog, window, source)
        _check_pathless_file_choice_ignored(dialog)
        _check_finish_after_window_close(window)
    finally:
        dialog.destroy()
        window.destroy()
    print("PASS: the import dialog builds and waits for a name and a source")


def _check_finish_after_window_close(window) -> None:
    """Do not touch disposed widgets when worker completion follows window close."""
    from src.windows.AssetManager.IconPacks.ImportDialog import ImportPackDialog

    reloaded: list = []
    cursor: list = []
    chooser = _Bag(asset_manager=window, reload=lambda: reloaded.append(True))
    # A real dialog, so _finish_import runs its own body; its pack_chooser is
    # the stand-in above.
    dialog = ImportPackDialog(_Bag(asset_manager=window))
    dialog.pack_chooser = chooser
    window.set_cursor_from_name = lambda *_a: cursor.append(True)

    saved = getattr(gl, "asset_manager", None)
    pack_import.set_import_running(True)
    gl.asset_manager = None  # the window closed while the import ran
    try:
        dialog._finish_import(None)
        assert reloaded == [], "the finish reloaded a grid on a closed window"
        assert cursor == [], "the finish set a cursor on a closed window"
        assert pack_import.import_is_running() is False, (
            "the finish left the in-flight flag set"
        )

        gl.asset_manager = window
        dialog._finish_import(None)
        assert reloaded == [True], "the finish did not reload on the live window"
    finally:
        gl.asset_manager = saved
        pack_import.set_import_running(False)
        dialog.destroy()


def _check_import_response_starts_worker(dialog, window, source) -> None:
    """Only the import response starts a worker. Cancel starts none."""
    import src.windows.AssetManager.IconPacks.ImportDialog as import_dialog_mod

    real_thread = import_dialog_mod.threading.Thread
    started: list = []

    class _NoThread:
        def __init__(self, *args, **kwargs):
            started.append((args, kwargs))

        def start(self):
            pass

    dialog.name_row.set_text("Studio")
    dialog.set_source(source)
    import_dialog_mod.threading.Thread = _NoThread
    try:
        dialog.on_response(dialog, "cancel")
        assert started == [], "a cancel response started an import worker"
        dialog.on_response(dialog, "import")
        assert len(started) == 1, "the import response did not start a worker"
    finally:
        import_dialog_mod.threading.Thread = real_thread
        pack_import.set_import_running(False)


def _check_pathless_file_choice_ignored(dialog) -> None:
    """A chosen location with no local path hands nothing to the callback."""
    from src.windows.AssetManager.IconPacks.ImportDialog import _FileChoice

    delivered: list = []
    choice = _FileChoice(dialog, delivered.append)

    choice._deliver(_Bag(get_path=lambda: None))
    assert delivered == [], "a location with no local path was delivered"
    choice._deliver(_Bag(get_path=lambda: "/tmp/here.png"))
    assert delivered == ["/tmp/here.png"], (
        f"a location with a path was not delivered: {delivered}"
    )
    choice._deliver(None)
    assert delivered == ["/tmp/here.png"], "a missing selection was delivered"


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_icon_pack_import")

    check_renderable_extensions()
    check_member_rule()
    check_resolved_within()

    check_write_target_is_checked()

    check_archive_with_nested_folders()
    check_wrapper_folder_is_stripped()
    check_flat_archive_single_picture()
    check_oversized_archive_is_refused()
    check_archive_rejects_escaping_member_path()
    check_archive_rejects_absolute_member_path()
    check_empty_archive_is_refused()
    check_lying_member_size_is_refused()

    check_folder_import()
    check_internal_folder_name_collision()
    check_banner_is_used_when_given()
    check_folder_import_skips_symlink()
    check_folder_import_rejects_special_file()
    check_folder_import_rejects_external_hardlink()
    check_folder_import_refuses_over_budget()
    check_damaged_archive_is_refused()
    check_undecodable_banner_falls_back()

    check_staging_is_unique_per_run()
    check_sweep_preserves_live_tree()
    check_wedged_staging_recovery()
    check_reload_replaces_grid()

    check_name_collision_suffix()
    check_pack_folder_name_safety()
    check_pack_name_required()
    check_missing_source_is_refused()

    check_failed_import_leaves_no_registration()
    check_killed_import_cleanup()
    check_import_store_layout()

    check_every_label_key_is_filled()
    check_import_dialog_construction()

    print("PASS: scenario_icon_pack_import")


if __name__ == "__main__":
    main()
