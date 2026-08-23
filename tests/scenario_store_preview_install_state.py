"""
A store preview button must not read "installed" after a failed download.

The one data-only preview install() checks the StoreResult, notifies on an Err,
and leaves the button in its previous state so the user can retry. Each asset
class reaches it through its own descriptor, so every check below drives the
same method over the descriptor of one class. The preview runs unbound over a
duck-typed self.
"""

# An Err is truthy, so the protocol is narrowing on the result type rather than
# a truthiness check.
import types

import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import gi

gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from src.backend.Store import asset_types  # noqa: E402
from src.backend.Store.store_result import Ok, Err, ErrReason  # noqa: E402
from src.windows.Store.StoreData import (  # noqa: E402
    IconData,
    WallpaperData,
    SDPlusBarWallpaperData,
)

WATCHDOG_SECONDS = 30


def pump_main_context(rounds: int = 50) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        while ctx.pending():
            ctx.iteration(False)


def _install_stub(descriptor, install_result):
    """A stand-in for one backend install method, with its real signature.

    A stub that swallowed every call shape would hide the trap this scenario
    now covers. install_icon takes icon_data, install_wallpaper takes
    wallpaper_data, and install_sd_plus_bar_wallpaper takes
    sd_plus_bar_wallpaper_data. A shared install written with any one of those
    keywords works for that class and raises TypeError for the rest. Binding
    the call against the real signature raises here exactly where the real
    backend would.
    """
    import inspect

    from src.backend.Store.StoreBackend import StoreBackend

    signature = inspect.signature(getattr(StoreBackend, descriptor.install_attr))

    def stub(*args, **kwargs):
        # None stands in for self, which a bound method would supply.
        signature.bind(None, *args, **kwargs)
        return install_result

    return stub


def _make_fake(descriptor, data, install_result):
    """A duck-typed preview self. It records set_install_state and notify
    calls, over a backend stub whose install_* answers install_result.
    """
    state = {"install_state": 0, "set_calls": [], "notified": 0}
    backend = types.SimpleNamespace(
        **{descriptor.install_attr: _install_stub(descriptor, install_result)})

    def set_install_state(s):
        state["set_calls"].append(s)
        state["install_state"] = s

    fake = types.SimpleNamespace(
        store=types.SimpleNamespace(backend=backend),
        descriptor=descriptor,
        asset_data=data,
        install_state=0,
        set_install_state=set_install_state,
        notify_install_failure=lambda: state.__setitem__("notified", state["notified"] + 1),
        # The base install passes no extra install kwargs; a plugin card
        # overrides this to add the install-script consent prompt.
        _install_kwargs=lambda: {},
    )
    return fake, state


def _check_failed_install_preserves_state(descriptor, data, err, label) -> None:
    from src.windows.Store.AssetPage import StoreAssetPreview

    fake, state = _make_fake(descriptor, data, err)

    StoreAssetPreview.install(fake)
    pump_main_context()

    assert state["install_state"] != 1, (
        f"{label}: a failed install ({err.reason}) must NOT flip the button to "
        f"installed -- got set_install_state calls {state['set_calls']}"
    )
    assert 1 not in state["set_calls"], (
        f"{label}: set_install_state(1) must never run on a failed install, "
        f"got {state['set_calls']}"
    )
    assert state["notified"] == 1, (
        f"{label}: a failed install must record exactly one failure "
        f"notification, got {state['notified']}"
    )
    print(f"PASS: {label} preview keeps its state and notifies on a failed install")


def check_icon_preview_404() -> None:
    data = IconData(github="https://github.com/a/Icons", icon_id="com_a_Icons",
                    icon_name="Test Icons")
    _check_failed_install_preserves_state(
        asset_types.ICON, data,
        Err(ErrReason.INSTALL_FAILED, "404-shaped"), "icon")


def check_wallpaper_preview_offline() -> None:
    data = WallpaperData(github="https://github.com/b/Wall", wallpaper_id="com_b_Wall",
                         wallpaper_name="Test Wall")
    _check_failed_install_preserves_state(
        asset_types.WALLPAPER, data,
        Err(ErrReason.NO_CONNECTION, "offline"), "wallpaper")


def check_sd_plus_preview_400() -> None:
    data = SDPlusBarWallpaperData(github="https://github.com/c/SDPlus", id="com_c_SDPlus",
                                  name="Test SDPlus")
    _check_failed_install_preserves_state(
        asset_types.SD_PLUS_BAR, data,
        Err(ErrReason.INVALID_ASSET, "400-shaped"), "SD+ bar wallpaper")


def check_install_rows_bind_against_the_real_backend() -> None:
    """Every row's install method must take the record the shared install
    passes it.

    The stub above answers any call shape, so it cannot see a call the real
    backend would refuse. A keyword call is the trap: install_icon takes
    icon_data, install_wallpaper takes wallpaper_data, and a shared install
    written with either name works for one class and raises TypeError for the
    other three. This binds the row against the real signature instead.
    """
    import inspect

    from src.backend.Store.StoreBackend import StoreBackend

    for descriptor in asset_types.ASSET_TYPES:
        method = getattr(StoreBackend, descriptor.install_attr)
        # None stands in for self. The bind proves the record goes in
        # positionally, which is how the shared install calls it.
        inspect.signature(method).bind(None, descriptor.data_cls())
    print(f"PASS: all {len(asset_types.ASSET_TYPES)} install rows take their "
          "record positionally on the real backend")


def check_icon_preview_success_flips_installed() -> None:
    """A successful install still flips the button. An Ok(None) reaches the
    idle-marshalled set_install_state, which this check pumps."""
    from src.windows.Store.AssetPage import StoreAssetPreview

    data = IconData(github="https://github.com/a/Icons", icon_id="com_a_Icons",
                    icon_name="Test Icons")
    fake, state = _make_fake(asset_types.ICON, data, Ok(None))

    StoreAssetPreview.install(fake)
    pump_main_context()

    assert state["install_state"] == 1, (
        f"a successful install must flip the button to installed, got "
        f"{state['set_calls']}"
    )
    assert state["notified"] == 0, "a successful install must not notify a failure"
    print("PASS: icon preview flips to installed on a successful install")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_store_preview_install_state")
    check_icon_preview_404()
    check_wallpaper_preview_offline()
    check_sd_plus_preview_400()
    check_install_rows_bind_against_the_real_backend()
    check_icon_preview_success_flips_installed()
    print("scenario_store_preview_install_state: PASS")


if __name__ == "__main__":
    main()
