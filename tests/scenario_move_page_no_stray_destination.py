"""An aborted page rename leaves no destination file behind.

move_page claims the destination with O_EXCL, which creates a 0-byte file,
then fills it from the source. A failure between the claim and the fill used
to strand that empty file under a real page name, and the loader then
quarantines it as a corrupt page at the next load. The fill also streamed
straight into the destination, so a kill mid-copy left a truncated page.
This injects a failing and a partially-writing copy into the rename and
asserts the destination is gone afterward, and that a successful rename
lands the whole content with no temp strays.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import glob
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
    """Every empty page file and every leftover temp in the pages folder."""
    found = []
    for path in glob.glob(os.path.join(_pages_dir(), "*")):
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
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

    real_copy2 = shutil.copy2

    # --- Part A: the copy raises before writing anything -------------------
    src_a = fixtures.seed_page("StrayMoveSrcA")
    dst_a = os.path.join(_pages_dir(), "StrayMoveDstA.json")

    def failing_copy2(src, dst, **kwargs):
        raise InjectedCopyFailure(dst)

    shutil.copy2 = failing_copy2
    try:
        gl.page_manager.move_page(src_a, dst_a)
    except InjectedCopyFailure:
        pass
    else:
        failures.append("a failed copy did not surface out of move_page")
    finally:
        shutil.copy2 = real_copy2

    if os.path.exists(dst_a):
        failures.append("a failed copy left a destination file behind "
                        f"({os.path.getsize(dst_a)} bytes)")
    if not os.path.exists(src_a):
        failures.append("a failed copy lost the source page")

    # --- Part B: the copy writes half the page, then dies -------------------
    src_b = fixtures.seed_page("StrayMoveSrcB")
    dst_b = os.path.join(_pages_dir(), "StrayMoveDstB.json")

    def partial_copy2(src, dst, **kwargs):
        with open(dst, "w") as f:
            f.write('{"keys": {')
            f.flush()
            os.fsync(f.fileno())
        raise InjectedCopyFailure(dst)

    shutil.copy2 = partial_copy2
    try:
        gl.page_manager.move_page(src_b, dst_b)
    except InjectedCopyFailure:
        pass
    else:
        failures.append("a dying copy did not surface out of move_page")
    finally:
        shutil.copy2 = real_copy2

    if os.path.exists(dst_b):
        failures.append("a dying copy left a destination file behind")

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

    print("PASS: an aborted rename leaves no destination and no strays, "
          "and a completed rename lands the whole page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
