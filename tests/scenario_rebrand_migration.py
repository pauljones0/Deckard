"""Move the pre-rename application tree and leave a compatibility symlink."""

# Refuse conflicting roots, foreign links, and live old instances.
# A pending marker repairs interruption between rename and symlink creation.
import os
import shutil
import sys
import tempfile

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Redirect HOME before importing the module. The autostart cleanup resolves
# ~/.config/autostart at call time through expanduser.
HOME = tempfile.mkdtemp(prefix="rebrand_home_")
os.environ["HOME"] = HOME

import rebrand_migration as rm  # noqa: E402

assert "globals" not in sys.modules, "rebrand_migration must not pull in globals"

rm._old_instance_running = lambda: False  # no session bus in the harness


def fresh_roots():
    base = tempfile.mkdtemp(prefix="rebrand_roots_", dir=HOME)
    return os.path.join(base, rm.OLD_ID), os.path.join(base, rm.NEW_ID)


def make_old_tree(legacy_root):
    os.makedirs(os.path.join(legacy_root, "data", "settings"))
    os.makedirs(os.path.join(legacy_root, "data", "pages"))
    os.makedirs(os.path.join(legacy_root, "static"))
    with open(os.path.join(legacy_root, "data", "settings", "settings.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(legacy_root, "data", "pages", "Main.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(legacy_root, "static", "settings.json"), "w") as f:
        f.write("{}")


def make_skeleton(deckard_root):
    # exactly what globals.py + mp4_tile_cache.py leave behind at import time
    os.makedirs(os.path.join(deckard_root, "data", "plugins"))
    os.makedirs(os.path.join(deckard_root, "data", "cache", "videos"))


def marker_state(deckard_root):
    try:
        with open(os.path.join(deckard_root, rm.MARKER_NAME)) as f:
            return f.read().strip()
    except OSError:
        return None


def assert_migrated(legacy_root, deckard_root):
    assert os.path.isfile(os.path.join(deckard_root, "data", "settings", "settings.json"))
    assert os.path.isfile(os.path.join(deckard_root, "static", "settings.json"))
    assert os.path.islink(legacy_root), "compat symlink missing at old root"
    assert os.path.realpath(legacy_root) == os.path.realpath(deckard_root)
    # embedded absolute old paths must resolve through the link
    assert os.path.isfile(os.path.join(legacy_root, "data", "pages", "Main.json"))
    assert marker_state(deckard_root) == rm._STATE_COMPLETE


def expect_exit(fn):
    try:
        fn()
    except SystemExit as e:
        assert e.code == 1
        return
    raise AssertionError("expected SystemExit(1)")


# 1. A fresh install has neither root, so nothing happens.
old_root, new_root = fresh_roots()
rm.migrate(old_root, new_root, argv=["main.py"])
assert not os.path.lexists(old_root) and not os.path.lexists(new_root)
print("1. fresh install no-op: OK")

# 2. The normal move; autostart cleanup has separate coverage.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
rm.migrate(old_root, new_root, argv=["main.py"])
assert_migrated(old_root, new_root)
print("2. normal move: OK")

# 3. An idempotent re-run.
rm.migrate(old_root, new_root, argv=["main.py"])
assert_migrated(old_root, new_root)
print("3. idempotent re-run: OK")

# 4. A new root poisoned by the import-time makedirs skeleton.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
make_skeleton(new_root)
rm.migrate(old_root, new_root, argv=["main.py"])
assert_migrated(old_root, new_root)
assert not os.path.exists(os.path.join(new_root, "data", "cache", "videos")), "skeleton merged instead of replaced"
print("4. skeleton-poisoned new root: OK")

# 5. Both roots hold real files, so the migration aborts and touches nothing.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
os.makedirs(os.path.join(new_root, "data", "logs"))
with open(os.path.join(new_root, "data", "logs", "logs.log"), "w") as f:
    f.write("x")
expect_exit(lambda: rm.migrate(old_root, new_root, argv=["main.py"]))
assert os.path.isdir(old_root) and not os.path.islink(old_root), "old root mutated on abort"
assert os.path.isfile(os.path.join(old_root, "data", "settings", "settings.json"))
assert os.path.isfile(os.path.join(new_root, "data", "logs", "logs.log"))
print("5. both-have-files abort: OK")

# 6. A foreign symlink at the old root aborts the migration.
old_root, new_root = fresh_roots()
elsewhere = tempfile.mkdtemp(prefix="elsewhere_", dir=HOME)
os.makedirs(os.path.dirname(old_root), exist_ok=True)
os.symlink(elsewhere, old_root)
expect_exit(lambda: rm.migrate(old_root, new_root, argv=["main.py"]))
assert os.path.realpath(old_root) == os.path.realpath(elsewhere), "foreign symlink replaced"
print("6. foreign symlink abort: OK")

# 7. A broken symlink at the old root aborts the migration.
old_root, new_root = fresh_roots()
os.makedirs(os.path.dirname(old_root), exist_ok=True)
os.symlink(os.path.join(HOME, "does-not-exist"), old_root)
expect_exit(lambda: rm.migrate(old_root, new_root, argv=["main.py"]))
print("7. broken symlink abort: OK")

# 8. Repair mode after a crash between the rename and the symlink.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
os.makedirs(os.path.dirname(new_root), exist_ok=True)
with open(os.path.join(old_root, rm.MARKER_NAME), "w") as f:
    f.write(rm._STATE_PENDING + "\n")
os.rename(old_root, new_root)  # the crash point, renamed with no symlink and marker pending
rm.migrate(old_root, new_root, argv=["main.py"])
assert_migrated(old_root, new_root)
print("8. repair after rename/symlink crash: OK")

# 9. A pending marker with the old root reappeared as a real dir.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
with open(os.path.join(old_root, rm.MARKER_NAME), "w") as f:
    f.write(rm._STATE_PENDING + "\n")
os.rename(old_root, new_root)
os.makedirs(os.path.join(old_root, "data"))  # an old build recreated the tree
rm.migrate(old_root, new_root, argv=["main.py"])  # must not raise, must not delete
assert marker_state(new_root) == rm._STATE_PENDING, "completed despite blocked symlink"
assert os.path.isdir(os.path.join(old_root, "data")), "reappeared old tree deleted"
print("9. pending + reappeared old root stays pending: OK")

# 10. A --data override skips everything.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
rm.migrate(old_root, new_root, argv=["main.py", "--data", "/tmp/custom"])
assert os.path.isdir(old_root) and not os.path.lexists(new_root), "--data run touched the roots"
rm.migrate(old_root, new_root, argv=["main.py", "--data=/tmp/custom"])
assert os.path.isdir(old_root) and not os.path.lexists(new_root)
print("10. --data override skip: OK")

# 11. A live pre-rename instance aborts before any mutation.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
rm._old_instance_running = lambda: True
expect_exit(lambda: rm.migrate(old_root, new_root, argv=["main.py"]))
assert os.path.isdir(old_root) and not os.path.islink(old_root)
assert not os.path.lexists(new_root)
rm._old_instance_running = lambda: False
print("11. live old-instance abort: OK")

# 12. The compat symlink is already in place but the marker is missing.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
os.rename(old_root, new_root)
os.symlink(new_root, old_root)  # migration done by hand, or the marker write failed
rm.migrate(old_root, new_root, argv=["main.py"])
assert marker_state(new_root) == rm._STATE_COMPLETE
assert_migrated(old_root, new_root)
print("12. marker backfill on existing symlink: OK")

# 14. argparse accepts any unambiguous prefix of --data, and only --data and
# --devel start with --d, so --dat and --da are overrides too.
for abbrev in ("--dat", "--da"):
    old_root, new_root = fresh_roots()
    make_old_tree(old_root)
    rm.migrate(old_root, new_root, argv=["main.py", abbrev, "/tmp/custom"])
    assert os.path.isdir(old_root) and not os.path.lexists(new_root), f"{abbrev} did not skip"
print("14. --data abbreviations (--dat, --da) skip: OK")

# 15. A dir-symlink in the new root is not skeleton, so the migration aborts.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
target = tempfile.mkdtemp(prefix="relocation_", dir=HOME)
os.makedirs(os.path.join(new_root, "data"))
os.symlink(target, os.path.join(new_root, "data", "plugins"))  # user relocation, no plain files
expect_exit(lambda: rm.migrate(old_root, new_root, argv=["main.py"]))
assert os.path.islink(os.path.join(new_root, "data", "plugins")), "relocation symlink deleted as skeleton"
assert os.path.isdir(old_root) and not os.path.islink(old_root), "old tree mutated on abort"
print("15. dir-symlink not treated as skeleton: OK")

# 16. A new root that is itself a symlink aborts instead of crashing rmtree.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
elsewhere = tempfile.mkdtemp(prefix="newlink_", dir=HOME)
os.makedirs(os.path.dirname(new_root), exist_ok=True)
os.symlink(elsewhere, new_root)
expect_exit(lambda: rm.migrate(old_root, new_root, argv=["main.py"]))
assert os.path.islink(new_root) and os.path.realpath(new_root) == os.path.realpath(elsewhere)
print("16. symlinked new root abort: OK")

# 17. An undurable pending-marker write aborts before the rename.
old_root, new_root = fresh_roots()
make_old_tree(old_root)
os.chmod(old_root, 0o500)  # deny file creation in the legacy root, so the marker write fails
try:
    expect_exit(lambda: rm.migrate(old_root, new_root, argv=["main.py"]))
finally:
    os.chmod(old_root, 0o700)
assert os.path.isdir(old_root) and not os.path.lexists(new_root), "renamed despite undurable marker"
assert os.path.isfile(os.path.join(old_root, "data", "settings", "settings.json"))
print("17. undurable marker aborts before rename: OK")

# 13. The pre-globals contract.
class _FakeGlobals:  # simulate globals already imported
    pass

sys.modules["globals"] = _FakeGlobals()
try:
    rm.migrate(old_root, new_root, argv=["main.py"], require_pre_globals=True)
except AssertionError:
    print("13. pre-globals contract enforced: OK")
else:
    raise AssertionError("migrate() ran after `import globals`")
finally:
    del sys.modules["globals"]

shutil.rmtree(HOME, ignore_errors=True)
print("scenario_rebrand_migration: all cases passed")
