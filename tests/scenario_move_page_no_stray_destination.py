"""An aborted page rename leaves no destination file behind.

move_page once claimed the destination with O_EXCL, a 0-byte file, and then
filled it in place. A failure between the claim and the fill stranded that
empty file under a real page name, which the loader quarantines as a corrupt
page at the next load, and a kill mid-copy left a truncated page. The fill
now goes through a temp file that os.link publishes as the destination in
one atomic step, so no destination name exists until the whole content
does. This injects a failing and a partially-writing copy into the rename
and asserts no destination and no strays remain and the source survives,
and that a successful rename lands the whole content.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os
import shutil

import globals as gl
from fixtures import start_watchdog


class InjectedCopyFailure(Exception):
    pass


def _pages_dir() -> str:
    return os.path.join(gl.DATA_PATH, "pages")


def _strays() -> list[str]:
    """Every empty page file and every leftover temp in the pages folder.

    os.listdir, not glob: the temps are dot-prefixed and glob patterns skip
    dotfiles, which would make this scan blind to the exact files it is for.
    """
    found = []
    for name in os.listdir(_pages_dir()):
        path = os.path.join(_pages_dir(), name)
        if not os.path.isfile(path):
            continue
        if name.endswith(".tmp") or name.startswith(".save-"):
            found.append(name)
        elif name.endswith(".json") and os.path.getsize(path) == 0:
            found.append(name)
    return found


def main() -> int:
    start_watchdog(60, "move_page_no_stray_destination")
    fixtures._install_integration_globals()
    os.makedirs(_pages_dir(), exist_ok=True)
    failures: list[str] = []

    # The injection sits inside atomic_copy_file, so it exercises that
    # helper's own failure cleanup, not just move_page's handling. The pages
    # seeded here carry no pending edits, so the backup seam declines before
    # its copy and the fill is the only copyfileobj call in the move.
    real_copyfileobj = shutil.copyfileobj

    # --- Part A: the copy raises before writing anything -------------------
    src_a = fixtures.seed_page("StrayMoveSrcA")
    dst_a = os.path.join(_pages_dir(), "StrayMoveDstA.json")

    def failing_copyfileobj(fsrc, fdst, *args, **kwargs):
        raise InjectedCopyFailure("no bytes written")

    shutil.copyfileobj = failing_copyfileobj
    try:
        gl.page_manager.move_page(src_a, dst_a)
    except InjectedCopyFailure:
        pass
    else:
        failures.append("a failed copy did not surface out of move_page")
    finally:
        shutil.copyfileobj = real_copyfileobj

    if os.path.exists(dst_a):
        failures.append("a failed copy left a destination file behind "
                        f"({os.path.getsize(dst_a)} bytes)")
    if not os.path.exists(src_a):
        failures.append("a failed copy lost the source page")

    # --- Part B: the copy writes half the page, then dies -------------------
    src_b = fixtures.seed_page("StrayMoveSrcB")
    dst_b = os.path.join(_pages_dir(), "StrayMoveDstB.json")

    def partial_copyfileobj(fsrc, fdst, *args, **kwargs):
        fdst.write(b'{"keys": {')
        fdst.flush()
        raise InjectedCopyFailure("died mid-copy")

    shutil.copyfileobj = partial_copyfileobj
    try:
        gl.page_manager.move_page(src_b, dst_b)
    except InjectedCopyFailure:
        pass
    else:
        failures.append("a dying copy did not surface out of move_page")
    finally:
        shutil.copyfileobj = real_copyfileobj

    if os.path.exists(dst_b):
        failures.append("a dying copy left a destination file behind")
    if not os.path.exists(src_b):
        failures.append("a dying copy lost the source page")

    for name in _strays():
        failures.append(f"stray file after the aborted renames: {name}")

    # --- Part C: a normal rename lands whole, with nothing left over --------
    src_c = fixtures.seed_page("StrayMoveSrcC")
    with open(src_c) as f:
        seeded = json.load(f)
    dst_c = os.path.join(_pages_dir(), "StrayMoveDstC.json")
    gl.page_manager.move_page(src_c, dst_c)
    if os.path.exists(src_c):
        failures.append("a completed rename kept the source file")
    try:
        with open(dst_c) as f:
            landed = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        failures.append(f"the renamed page does not read back: {e}")
    else:
        if landed != seeded:
            failures.append("the renamed page lost content in transit")

    for name in _strays():
        failures.append(f"stray file in the pages folder: {name}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print("PASS: an aborted rename leaves no destination, no strays, and a "
          "live source; a completed rename lands the whole page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
