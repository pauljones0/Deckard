"""Verify action-index rebuilds publish one complete replacement.
Hooks expose concurrent windows without sleeps; missing placeholders persist."""

import threading
import types

import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import globals as gl  # noqa: F401,E402  (import order; PluginBase reads it)

from src.backend.PluginManager.PluginBase import PluginBase  # noqa: E402
from src.backend.PluginManager.PluginManager import PluginManager  # noqa: E402

PLUGIN_ID = "com_test_index"
HOLDER_A = types.SimpleNamespace(action_id=f"{PLUGIN_ID}::A")
HOLDER_B = types.SimpleNamespace(action_id=f"{PLUGIN_ID}::B")
HOLDER_C = types.SimpleNamespace(action_id=f"{PLUGIN_ID}::C")


def seed_registry() -> dict:
    """Seed one enabled plugin with two action holders."""
    PluginBase.plugins.clear()
    PluginBase.disabled_plugins.clear()
    holders = {f"{PLUGIN_ID}::A": HOLDER_A, f"{PLUGIN_ID}::B": HOLDER_B}
    PluginBase.plugins[PLUGIN_ID] = {"object": types.SimpleNamespace(action_holders=holders)}
    PluginManager.action_index = {}
    return holders


def check_publication_slot() -> None:
    """Verify the rebuild publishes through the shared class attribute."""
    seed_registry()
    pm = PluginManager()
    pm.generate_action_index()

    assert PluginManager.action_index.get(f"{PLUGIN_ID}::A") is HOLDER_A, (
        "generate_action_index did not publish into the shared class "
        f"attribute: {sorted(PluginManager.action_index)}"
    )
    assert pm.get_action_holder_from_id(f"{PLUGIN_ID}::B") is HOLDER_B
    print("PASS: the rebuild publishes into the shared action index slot")


def check_rebuild_replaces_the_index() -> None:
    """Verify each rebuild adds installed holders and drops uninstalled ones."""
    holders = seed_registry()
    pm = PluginManager()
    pm.generate_action_index()

    assert pm.get_action_holder_from_id(f"{PLUGIN_ID}::C") is None

    holders[f"{PLUGIN_ID}::C"] = HOLDER_C
    pm.generate_action_index()
    assert pm.get_action_holder_from_id(f"{PLUGIN_ID}::C") is HOLDER_C, (
        "a rebuild after an install did not pick up the new action holder"
    )

    del holders[f"{PLUGIN_ID}::C"]
    pm.generate_action_index()
    assert pm.get_action_holder_from_id(f"{PLUGIN_ID}::C") is None, (
        "a rebuild after an uninstall merged into the previous index: "
        f"{sorted(PluginManager.action_index)}"
    )
    print("PASS: a rebuild replaces the index instead of merging into it")


def check_same_thread_reader_inside_the_rebuild() -> None:
    """Probe a same-thread read from inside the rebuild window."""
    seed_registry()
    pm = PluginManager()
    pm.generate_action_index()
    assert pm.get_action_holder_from_id(f"{PLUGIN_ID}::A") is HOLDER_A

    seen: list = []
    real_get_plugins = pm.get_plugins

    def probing_get_plugins(*args, **kwargs):
        seen.append(pm.get_action_holder_from_id(f"{PLUGIN_ID}::A"))
        seen.append(len(PluginManager.action_index))
        return real_get_plugins(*args, **kwargs)

    pm.get_plugins = probing_get_plugins
    try:
        pm.generate_action_index()
    finally:
        del pm.get_plugins

    assert seen[0] is HOLDER_A, (
        "a read inside the rebuild found no holder for an installed action -- "
        "the page it serves keeps a NoActionHolderFound placeholder"
    )
    assert seen[1] == 2, f"the published index was partial inside the rebuild: {seen[1]} entries"
    print("PASS: a read inside the rebuild resolves the installed holder")


def check_concurrent_reader_inside_the_rebuild() -> None:
    """Park a bounded reader inside the rebuild before publication."""
    seed_registry()
    pm = PluginManager()
    pm.generate_action_index()

    mid_rebuild = threading.Event()
    read_done = threading.Event()
    observed: list = []

    def reader() -> None:
        try:
            mid_rebuild.wait(30)
            observed.append(pm.get_action_holder_from_id(f"{PLUGIN_ID}::B"))
        finally:
            read_done.set()

    thread = threading.Thread(target=reader, name="action_index_reader", daemon=True)
    thread.start()

    real_get_plugins = pm.get_plugins

    def releasing_get_plugins(*args, **kwargs):
        mid_rebuild.set()
        read_done.wait(30)
        return real_get_plugins(*args, **kwargs)

    pm.get_plugins = releasing_get_plugins
    try:
        pm.generate_action_index()
    finally:
        del pm.get_plugins

    thread.join(30)
    assert not thread.is_alive(), "the reader thread did not finish"
    assert observed, "the reader thread never ran its read"
    assert observed[0] is HOLDER_B, (
        "a concurrent read during the rebuild found no holder for an "
        "installed action"
    )
    print("PASS: a concurrent read during the rebuild resolves the installed holder")


def main() -> None:
    # Below the per-scenario timeout of run_all.py, so a stall reports here
    # with a message instead of an opaque runner timeout.
    fixtures.start_watchdog(60, label="scenario_action_index_publish")

    check_publication_slot()
    check_rebuild_replaces_the_index()
    check_same_thread_reader_inside_the_rebuild()
    check_concurrent_reader_inside_the_rebuild()

    print("PASS: scenario_action_index_publish")


if __name__ == "__main__":
    main()
