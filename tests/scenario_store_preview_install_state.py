"""Keep preview install state unchanged after failed descriptor-driven installs."""

# An Err is truthy, so the protocol is narrowing on the result type rather than
# a truthiness check.
import types

import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import gi

gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

import globals as gl  # noqa: E402

from src.backend.Store import asset_types  # noqa: E402
from src.backend.Store.store_result import Ok, Err, ErrReason  # noqa: E402
from src.windows.Store.StoreData import (  # noqa: E402
    IconData,
    PluginData,
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
    """Bind calls against the selected backend install method's real signature."""
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
    """Bind each descriptor record positionally against its real backend method."""
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
    """Pump the idle-marshalled state change after an Ok install result."""
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


# Supply readable manifests to exercise dependency-set branches of the shared install.

PINNED_SHA = "0" * 40


def _dependency_backend(install_results: dict):
    """A backend that names one dependency for the root, and answers each
    install from install_results by asset id."""
    root = PluginData(github="https://github.com/t/Root", plugin_id="com.test.Root",
                      plugin_name="Root", commit_sha=PINNED_SHA)
    dep = PluginData(github="https://github.com/t/Dep", plugin_id="com.test.Dep",
                     plugin_name="Dep", commit_sha=PINNED_SHA)
    installed: list[str] = []

    def install_plugin(plugin_data, auto_update=False, ask_install_script=None):
        installed.append(plugin_data.plugin_id)
        return install_results[plugin_data.plugin_id]

    def get_manifest(url, commit):
        if url.endswith("/Root"):
            return {"dependencies": ["com.test.Dep"]}
        return {}

    backend = types.SimpleNamespace(
        install_plugin=install_plugin,
        get_manifest=get_manifest,
        get_all_plugins=lambda include_images=True: Ok([root, dep]),
        get_all_icons=lambda include_images=True: Ok([]),
        get_all_wallpapers=lambda include_images=True: Ok([]),
        get_all_sd_plus_bar_wallpapers=lambda include_images=True: Ok([]),
    )
    return backend, root, installed


def _fake_over(backend, descriptor, data):
    state = {"install_state": 0, "set_calls": [], "notified": 0}

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
        _install_kwargs=lambda: {},
    )
    return fake, state


class _SetConsentPatch:
    """Answer the set prompt without building a dialog. The duck-typed self
    carries no real window, so the real prompt cannot be presented."""

    def __init__(self, agree: bool):
        self.agree = agree
        self.asked: list = []

    def __enter__(self):
        import src.windows.Store.install_consent as consent
        self._module = consent
        self._original = consent.make_dependency_consent
        consent.make_dependency_consent = lambda parent: self._ask
        return self

    def _ask(self, root_name, plan):
        self.asked.append((root_name, plan.names()))
        return self.agree

    def __exit__(self, *exc):
        self._module.make_dependency_consent = self._original
        return False


class _NotifyRecorder:
    def __init__(self):
        self.errors: list[tuple[str, str | None]] = []

    def error(self, text, title=None):
        self.errors.append((text, title))

    def info(self, text, title=None):
        pass


def check_declined_set_notifies_nothing() -> None:
    """Cancelling the set prompt downloads nothing, so it is not a failure
    and must not raise a failure notification or move the button."""
    from src.windows.Store.AssetPage import StoreAssetPreview

    backend, root, installed = _dependency_backend(
        {"com.test.Root": Ok(None), "com.test.Dep": Ok(None)})
    fake, state = _fake_over(backend, asset_types.PLUGIN, root)

    recorder = _NotifyRecorder()
    original_notify = getattr(gl, "notify", None)
    gl.notify = recorder
    try:
        with _SetConsentPatch(agree=False) as consent:
            result = StoreAssetPreview.install(fake)
            pump_main_context()
    finally:
        gl.notify = original_notify

    assert consent.asked, "the set prompt must have been reached"
    assert result is False, f"a declined install must report False, got {result!r}"
    assert installed == [], f"a declined set must install nothing, got {installed}"
    assert state["notified"] == 0, (
        "cancelling is not a failure, so no failure notification may fire, "
        f"got {state['notified']}")
    assert recorder.errors == [], (
        f"and no error notification either, got {recorder.errors}")
    assert state["set_calls"] == [], (
        f"the button must not move at all, got {state['set_calls']}")
    print("PASS: a declined dependency set installs nothing and reports nothing")


def check_mid_set_failure_names_what_landed() -> None:
    """When a dependency installed and the root then failed, the plain
    failure notification would not say the dependency is now installed."""
    from src.windows.Store.AssetPage import StoreAssetPreview

    backend, root, installed = _dependency_backend(
        {"com.test.Dep": Ok(None),
         "com.test.Root": Err(ErrReason.NO_CONNECTION, "offline")})
    fake, state = _fake_over(backend, asset_types.PLUGIN, root)

    recorder = _NotifyRecorder()
    original_notify = getattr(gl, "notify", None)
    gl.notify = recorder
    try:
        with _SetConsentPatch(agree=True):
            result = StoreAssetPreview.install(fake)
            pump_main_context()
    finally:
        gl.notify = original_notify

    assert result is False, f"a failed set must report False, got {result!r}"
    assert installed == ["com.test.Dep", "com.test.Root"], (
        f"the dependency installs before the root, got {installed}")
    assert 1 not in state["set_calls"], (
        f"a failed set must not flip the button to installed, got {state['set_calls']}")
    assert len(recorder.errors) == 1, (
        "a part-installed set must report through the notification that can "
        f"carry the detail, got {recorder.errors}")
    text, _title = recorder.errors[0]
    assert "Dep" in text, (
        f"the report must name the item that stays installed, got {text!r}")
    assert state["notified"] == 0, (
        "the plain failure notification says nothing about what landed, so it "
        f"must not be the one used, got {state['notified']}")
    print("PASS: a part-installed set names what stays installed")


def check_a_failed_pack_under_a_plugin_is_titled_as_a_pack() -> None:
    """A plugin can need an icon pack. If that pack is what failed, calling
    it a plugin install failure names the wrong thing."""
    from src.windows.Store.AssetPage import StoreAssetPreview

    root = PluginData(github="https://github.com/t/Root", plugin_id="com.test.Root",
                      plugin_name="Root", commit_sha=PINNED_SHA)
    pack = IconData(github="https://github.com/t/Icons", icon_id="com.test.Icons",
                    icon_name="Icons", commit_sha=PINNED_SHA)
    installed: list[str] = []

    def install_plugin(plugin_data, auto_update=False, ask_install_script=None):
        installed.append(plugin_data.plugin_id)
        return Ok(None)

    def install_icon(icon_data):
        installed.append(icon_data.icon_id)
        return Err(ErrReason.NO_CONNECTION, "offline")

    backend = types.SimpleNamespace(
        install_plugin=install_plugin,
        install_icon=install_icon,
        get_manifest=lambda url, commit: (
            {"dependencies": ["com.test.Icons"]} if url.endswith("/Root") else {}),
        get_all_plugins=lambda include_images=True: Ok([root]),
        get_all_icons=lambda include_images=True: Ok([pack]),
        get_all_wallpapers=lambda include_images=True: Ok([]),
        get_all_sd_plus_bar_wallpapers=lambda include_images=True: Ok([]),
    )
    fake, state = _fake_over(backend, asset_types.PLUGIN, root)

    recorder = _NotifyRecorder()
    original_notify = getattr(gl, "notify", None)
    gl.notify = recorder
    try:
        with _SetConsentPatch(agree=True):
            StoreAssetPreview.install(fake)
            pump_main_context()
    finally:
        gl.notify = original_notify

    assert installed == ["com.test.Icons"], (
        f"the pack installs first and fails, so the root never runs, got {installed}")
    assert len(recorder.errors) == 1, f"one report must fire, got {recorder.errors}"
    _text, title = recorder.errors[0]
    assert title == "Icon pack install failed", (
        "the title must name the class of the item that actually failed, and "
        f"not the class of the root, got {title!r}")
    print("PASS: a pack that failed under a plugin root is titled as a pack")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_store_preview_install_state")
    check_icon_preview_404()
    check_wallpaper_preview_offline()
    check_sd_plus_preview_400()
    check_install_rows_bind_against_the_real_backend()
    check_icon_preview_success_flips_installed()
    check_declined_set_notifies_nothing()
    check_mid_set_failure_names_what_landed()
    check_a_failed_pack_under_a_plugin_is_titled_as_a_pack()
    print("scenario_store_preview_install_state: PASS")


if __name__ == "__main__":
    main()
