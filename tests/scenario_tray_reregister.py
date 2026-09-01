"""Verify tray registration, icon discovery, and watcher recovery."""

import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import gc
import os
import sys
import traceback

from gi.repository import Gio

import globals as gl
from tray_icon_asset_checks import (
    REPO_ROOT,
    check_flatpak_manifest_installs_the_app_icon,
    check_shipped_icon_theme_resolves,
)
from tray_icon_behavior_checks import (
    check_icon_theme_path_only_when_the_host_lacks_the_icon,
    check_item_and_menu_take_their_own_paths,
)
from tray_registration_checks import (
    check_base_double_register_no_orphan,
    check_sni_double_register_keeps_menu_live,
)
from tray_watcher_checks import run_watcher_checks


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_tray_reregister")
    check_base_double_register_no_orphan()
    check_sni_double_register_keeps_menu_live()
    check_flatpak_manifest_installs_the_app_icon()
    gl.MAIN_PATH = REPO_ROOT

    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()
    try:
        check_shipped_icon_theme_resolves()
        check_item_and_menu_take_their_own_paths()
        check_icon_theme_path_only_when_the_host_lacks_the_icon()
        run_watcher_checks(test_bus.get_bus_address())
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        test_bus.stop()
        os._exit(1)
    finally:
        gc.collect()
        test_bus.down()
    print("PASS: scenario_tray_reregister")


if __name__ == "__main__":
    main()
