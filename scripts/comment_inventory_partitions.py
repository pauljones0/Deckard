from __future__ import annotations

import collections
from pathlib import Path
from typing import Iterable

from comment_inventory_core import InventoryError, Unit


PARTITION_IDS = tuple(
    [*(f"P{number:02d}" for number in range(16)), *(f"T{number:02d}" for number in range(14))]
)
PARTITION_NAMES = {
    "P00": "Support modules, GtkHelper, locales, scripts, Flatpak",
    "P01": "Deck lifecycle and input contract",
    "P02": "Media writer and paint protocol contract",
    "P03": "Render composition and cache contract",
    "P04": "Public plugin API contract",
    "P05": "Signals and event dispatch contract",
    "P06": "Plugin manager internals",
    "P07": "Atomic settings and page persistence contract",
    "P08": "Page management and migration",
    "P09": "Store backend",
    "P10": "Asset and pack backends",
    "P11": "Desktop and platform integration",
    "P12": "Asset and store UI",
    "P13": "Main-window UI",
    "P14": "Other windows and UI adapters",
    "P15": "Application shell and core backend services",
    "T00": "Test harness, hardware, and soak tests",
    "T01": "Scenarios A",
    "T02": "Scenarios B",
    "T03": "Scenarios C",
    "T04": "Scenarios D through E",
    "T05": "Scenarios F through H",
    "T06": "Scenarios I through K",
    "T07": "Scenarios L through O",
    "T08": "Page scenarios",
    "T09": "Other P scenarios",
    "T10": "Scenarios Q through R and W through X",
    "T11": "Store scenarios",
    "T12": "Other S scenarios",
    "T13": "Scenarios T through V",
}
PLUGIN_API = {
    "src/backend/PluginManager/PluginBase.py",
    "src/backend/PluginManager/ActionHolder.py",
    "src/backend/PluginManager/ActionCore.py",
    "src/backend/PluginManager/InputBases.py",
    "src/backend/PluginManager/EventAssigner.py",
}
EVENT_FILES = {
    "src/backend/PluginManager/event_dispatch.py",
    "src/backend/PluginManager/backend_event_hold.py",
    "src/backend/PluginManager/EventHolder.py",
    "src/backend/PluginManager/EventManager.py",
}
LIFECYCLE_FILES = {
    "BetterDeck.py",
    "DeckController.py",
    "DeckManager.py",
    "InputIdentifier.py",
    "FakeDeck.py",
    "RemoteDeck.py",
    "RemoteDeckManager.py",
    "RemoteDecksLocalServerHandler.py",
    "ScreenSaver.py",
    "controller.py",
    "inputs.py",
    "input_state.py",
    "input_state_classes.py",
    "deck_events.py",
    "fair_lock.py",
    "reader_supervisor.py",
    "usb_reset.py",
}
WRITER_FILES = {
    "input_latency.py",
    "loop_metrics.py",
    "media_loop.py",
    "media_tasks.py",
    "media_writer.py",
    "page_completion.py",
    "paint_protocol.py",
    "paint_queue.py",
}
PERSISTENCE_FILES = {
    "src/backend/atomic_json.py",
    "src/backend/settings_store.py",
    "src/backend/settings_views.py",
    "src/backend/SettingsManager.py",
    "src/backend/PageManagement/page_document.py",
    "src/backend/PageManagement/page_flush.py",
    "src/backend/PageManagement/page_pins.py",
}
ASSET_PREFIXES = (
    "src/backend/AssetManagerBackend.py",
    "src/backend/archive_safety.py",
    "src/backend/IconPackManagement/",
    "src/backend/PackManagement/",
    "src/backend/SDPlusBarWallpaperPackManagement/",
    "src/backend/WallpaperPackManagement/",
)
PLATFORM_PREFIXES = (
    "src/backend/GnomeExtensions.py",
    "src/backend/LockScreenManager/",
    "src/backend/PermissionManagement/",
    "src/backend/PresenceMonitor/",
    "src/backend/Wayland/",
    "src/backend/WindowGrabber/",
    "src/backend/trayicon.py",
)


def _test_partition(path: str) -> str:
    name = Path(path).name
    if not name.startswith("scenario_"):
        return "T00"
    stem = name.removeprefix("scenario_")
    first = stem[0]
    if first == "a":
        return "T01"
    if first == "b":
        return "T02"
    if first == "c":
        return "T03"
    if first in "de":
        return "T04"
    if first in "fgh":
        return "T05"
    if first in "ijk":
        return "T06"
    if first in "lmno":
        return "T07"
    if first == "p":
        return "T08" if stem.startswith("page") else "T09"
    if first in "qrwx":
        return "T10"
    if first == "s":
        return "T11" if stem.startswith("store") else "T12"
    if first in "tuv":
        return "T13"
    raise InventoryError(f"{path}: no test campaign partition")


def partition_for(path: str) -> str:
    if path.startswith("tests/"):
        return _test_partition(path)
    if not path.startswith("src/"):
        return "P00"
    if path.startswith("src/backend/DeckManagement/"):
        name = Path(path).name
        if name in LIFECYCLE_FILES:
            return "P01"
        if name in WRITER_FILES:
            return "P02"
        return "P03"
    if path in PLUGIN_API:
        return "P04"
    if path.startswith("src/Signals/") or path in EVENT_FILES:
        return "P05"
    if path.startswith("src/backend/PluginManager/"):
        return "P06"
    if path in PERSISTENCE_FILES:
        return "P07"
    if path.startswith(("src/backend/PageManagement/", "src/backend/Migration/")):
        return "P08"
    if path.startswith("src/backend/Store/"):
        return "P09"
    if path.startswith(ASSET_PREFIXES):
        return "P10"
    if path.startswith(PLATFORM_PREFIXES):
        return "P11"
    if path.startswith(("src/windows/AssetManager/", "src/windows/Store/")):
        return "P12"
    if path.startswith("src/windows/mainWindow/"):
        return "P13"
    if path.startswith("src/windows/"):
        return "P14"
    return "P15"


def validate_coverage(unit_count: int, assignments: Iterable[tuple[int, str]]) -> None:
    occurrences: collections.Counter[int] = collections.Counter()
    for index, partition in assignments:
        if partition not in PARTITION_IDS:
            raise InventoryError(f"unit {index}: unknown partition {partition}")
        if index < 0 or index >= unit_count:
            raise InventoryError(f"partition assignment refers to unknown unit {index}")
        occurrences[index] += 1
    missing = [index for index in range(unit_count) if occurrences[index] == 0]
    duplicate = [index for index, count in occurrences.items() if count != 1]
    if missing or duplicate:
        raise InventoryError(
            f"partition coverage is not exact; missing={missing[:5]} duplicate={duplicate[:5]}"
        )


def assign_partitions(units: tuple[Unit, ...]) -> dict[str, list[Unit]]:
    assignments = [(index, partition_for(unit.path)) for index, unit in enumerate(units)]
    validate_coverage(len(units), assignments)
    result = {partition: [] for partition in PARTITION_IDS}
    for (index, partition), unit in zip(assignments, units, strict=True):
        if index >= len(units):
            raise InventoryError("partition assignment escaped the corpus")
        result[partition].append(unit)
    return result
