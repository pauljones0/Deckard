"""Require onboarding and missing-action UI to recover from an unavailable store."""

# Recommendations must show the error state for Err results.
# Onboarding install failures must reach the surviving main window.
import types

import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import gi

gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

import globals as gl  # noqa: E402

WATCHDOG_SECONDS = 30


def pump_main_context(rounds: int = 50) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        while ctx.pending():
            ctx.iteration(False)


class RecorderGroup:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def make_recommendations_stub():
    calls = {"loading": [], "error": 0}
    fake = types.SimpleNamespace(
        defaults=[],
        group=RecorderGroup(),
        set_loading=lambda loading: calls["loading"].append(loading),
        show_connection_error=lambda: calls.__setitem__("error", calls["error"] + 1),
    )
    return fake, calls


def check_recommendations_offline() -> None:
    from src.backend.Store.store_result import Err, ErrReason, Ok
    from src.windows.Onboarding.PluginRecommendations import PluginRecommendations

    # Leg 1a. An Err from the read boundary shows the error state. Iterating
    # the Err instead raises TypeError and kills the loader thread.
    gl.store_backend = types.SimpleNamespace(get_all_plugins=lambda: Err(ErrReason.NO_CONNECTION))
    fake, calls = make_recommendations_stub()
    PluginRecommendations.load(fake)
    assert calls["error"] == 1, (
        "an Err from get_all_plugins must show the error state (pre-fix: "
        "iterating the sentinel killed the loader thread and the spinner span "
        "forever -- fresh-install mode)"
    )
    assert not fake.group.rows, "no rows may be built on a failed fetch"

    # Leg 1b. A raising fetch gets the same treatment.
    def boom():
        raise RuntimeError("store exploded")

    gl.store_backend = types.SimpleNamespace(get_all_plugins=boom)
    fake, calls = make_recommendations_stub()
    PluginRecommendations.load(fake)
    assert calls["error"] == 1, "a raising fetch must also show the error state"

    # Leg 1c. An Ok still completes the load. The list holds only falsy
    # entries, so no widgets get built, and build_rows stops the spinner.
    gl.store_backend = types.SimpleNamespace(get_all_plugins=lambda: Ok([None, None]))
    fake, calls = make_recommendations_stub()
    PluginRecommendations.load(fake)
    pump_main_context()
    assert calls["loading"] == [True, False], (
        f"a successful fetch must complete the load (set_loading calls: "
        f"{calls['loading']})"
    )
    assert calls["error"] == 0

    print("PASS: recommendations page survives an unreachable store")


def check_get_plugin_for_id_offline() -> None:
    """Require get_plugin_for_id to convert an unavailable-store Err to None."""
    from src.backend.Store.StoreBackend import StoreBackend
    from src.backend.Store.store_result import Err, ErrReason

    backend = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    backend.get_all_plugins = lambda include_images=True: Err(ErrReason.NO_CONNECTION, "offline")

    assert backend.get_plugin_for_id("com_any_Plugin") is None, (
        "an unreachable store must resolve to None, not raise TypeError out of "
        "an iterated sentinel"
    )
    print("PASS: get_plugin_for_id returns None when the store is unreachable")


class _LabelRecorder:
    def __init__(self, text):
        self.text = text

    def set_text(self, text):
        self.text = text


class _SpinnerRecorder:
    def __init__(self):
        self.visible = True
        self.spinning = True

    def set_visible(self, visible):
        self.visible = visible

    def start(self):
        self.spinning = True

    def stop(self):
        self.spinning = False


def check_missing_row_spinner_recovers() -> None:
    """Require MissingRow failure styling and stop its spinner when offline."""
    import types as _types

    from src.backend import timer_wheel
    from src.backend.Store.StoreBackend import StoreBackend
    from src.backend.Store.store_result import Err, ErrReason
    from src.windows.mainWindow.elements.Sidebar.elements.ActionMissing.MissingRow import MissingRow

    backend = StoreBackend.__new__(StoreBackend)
    backend.get_all_plugins = lambda include_images=True: Err(ErrReason.NO_CONNECTION, "offline")
    gl.store_backend = backend

    # The 3s auto-hide would leave a live timer past this check, and it hides
    # the recovery state the assertions read.
    real_schedule = timer_wheel.schedule
    timer_wheel.schedule = lambda *a, **k: None

    css: list = []
    label = _LabelRecorder("Installing...")
    spinner = _SpinnerRecorder()
    fake = _types.SimpleNamespace(
        action_id="com_test_Missing::action0",
        spinner=spinner,
        label=label,
        install_label="Install",
        installing_label="Installing...",
        install_failed_label="Install failed",
        add_css_class=lambda name: css.append(name),
        set_sensitive=lambda sensitive: None,
        main_button=_types.SimpleNamespace(set_sensitive=lambda sensitive: None),
    )
    # show_install_error and hide_install_error are the real methods, bound
    # to the stub self. Their GLib marshalling is real and pumped below.
    fake.show_install_error = _types.MethodType(MissingRow.show_install_error, fake)
    fake.hide_install_error = _types.MethodType(MissingRow.hide_install_error, fake)

    try:
        MissingRow.install(fake)  # the @log.catch wrapper must not swallow into a stuck spinner
        pump_main_context()
    finally:
        timer_wheel.schedule = real_schedule

    assert label.text == fake.install_failed_label, (
        f"an unreachable store must move the row to the failed label, not leave "
        f"it stuck on {fake.installing_label!r}; got {label.text!r}"
    )
    assert spinner.visible is False and spinner.spinning is False, (
        "the spinner must stop on install failure, not spin forever"
    )
    assert "error" in css, "the error styling must be applied on install failure"
    print("PASS: MissingRow install surfaces failure when the store is unreachable")


def check_missing_row_reports_dependency_type() -> None:
    """Name the actual failed dependency class in MissingRow notifications.
    A pack failure must not be reported as failure of the selected plugin."""
    import types as _types

    from src.backend import timer_wheel
    from src.backend.Store.store_result import Err, ErrReason, Ok
    from src.windows.mainWindow.elements.Sidebar.elements.ActionMissing.MissingRow import MissingRow
    from src.windows.Store.StoreData import IconData, PluginData
    import src.windows.Store.install_consent as install_consent

    pinned = "0" * 40
    root = PluginData(github="https://github.com/t/Root", plugin_id="com_root_Plugin",
                      plugin_name="Root", commit_sha=pinned)
    pack = IconData(github="https://github.com/t/Icons", icon_id="com_root_Icons",
                    icon_name="Icons", commit_sha=pinned)
    installed: list = []

    def install_plugin(plugin_data, auto_update=False, ask_install_script=None):
        installed.append(plugin_data.plugin_id)
        return Ok(None)

    def install_icon(icon_data):
        installed.append(icon_data.icon_id)
        return Err(ErrReason.NO_CONNECTION, "offline")

    backend = _types.SimpleNamespace(
        get_plugin_for_id=lambda plugin_id: root,
        install_plugin=install_plugin,
        install_icon=install_icon,
        get_manifest=lambda url, commit: (
            {"dependencies": ["com_root_Icons"]} if url.endswith("/Root") else {}),
        get_all_plugins=lambda include_images=True: Ok([root]),
        get_all_icons=lambda include_images=True: Ok([pack]),
        get_all_wallpapers=lambda include_images=True: Ok([]),
        get_all_sd_plus_bar_wallpapers=lambda include_images=True: Ok([]),
    )
    gl.store_backend = backend
    gl.app = None  # so the install parents its prompts on no window

    # Answer the set prompt without a real dialog. MissingRow imports
    # make_dependency_consent at call time, so patching the module attribute lands.
    asked: list = []
    original_set_consent = install_consent.make_dependency_consent
    install_consent.make_dependency_consent = lambda parent: (
        lambda root_name, plan: (asked.append(root_name), True)[1])

    class _Notify:
        def __init__(self):
            self.errors = []

        def error(self, text, title=None):
            self.errors.append((text, title))

        def info(self, text, title=None):
            pass

    recorder = _Notify()
    gl.notify = recorder

    real_schedule = timer_wheel.schedule
    timer_wheel.schedule = lambda *a, **k: None

    label = _LabelRecorder("Installing...")
    spinner = _SpinnerRecorder()
    fake = _types.SimpleNamespace(
        action_id="com_root_Plugin::action0",
        spinner=spinner,
        label=label,
        install_label="Install",
        installing_label="Installing...",
        install_failed_label="Install failed",
        add_css_class=lambda name: None,
        set_sensitive=lambda sensitive: None,
        main_button=_types.SimpleNamespace(set_sensitive=lambda sensitive: None),
    )
    fake.show_install_error = _types.MethodType(MissingRow.show_install_error, fake)
    fake.hide_install_error = _types.MethodType(MissingRow.hide_install_error, fake)

    try:
        MissingRow.install(fake)
        pump_main_context()
    finally:
        timer_wheel.schedule = real_schedule
        install_consent.make_dependency_consent = original_set_consent

    assert asked, "the set prompt must have been reached"
    assert installed == ["com_root_Icons"], (
        f"the pack installs first and fails, so the root never runs, got {installed}")
    assert len(recorder.errors) == 1, (
        "a pack pulled in by the plugin that failed must be reported through "
        f"the notification that can carry the detail, got {recorder.errors}")
    _text, title = recorder.errors[0]
    assert title == "Icon pack install failed", (
        "the title must name the class of the item that actually failed, and "
        f"not the plugin the user clicked, got {title!r}")
    print("PASS: MissingRow names the failed dependency's class, not the plugin's")


def check_install_failures_toast() -> None:
    from src.windows.Onboarding.OnboardingWindow import OnboardingScreen5
    from src.backend.notify import Notifier

    toasts = []
    gl.app = types.SimpleNamespace(
        main_win=types.SimpleNamespace(
            show=lambda: None,
            is_visible=lambda: True,
            show_error_toast=lambda body: toasts.append(body),
        )
    )
    # The real facade runs here. The onboarding path reports through
    # gl.notify, and its main-thread routing is under test.
    gl.notify = Notifier()

    def get_plugin_for_id(plugin_id):
        return None  # unresolvable, so the install fails

    gl.store_backend = types.SimpleNamespace(get_plugin_for_id=get_plugin_for_id)

    progress_bar = types.SimpleNamespace(
        set_text=lambda *_: None,
        set_fraction=lambda *_: None,
        set_visible=lambda *_: None,
    )
    loading_box = types.SimpleNamespace(
        loading_label=types.SimpleNamespace(set_label=lambda *_: None),
        set_spinning=lambda *_: None,
        progress_bar=progress_bar,
    )
    plugin_data = types.SimpleNamespace(plugin_id="com_test_x", plugin_name="TestX")
    onboarding_window = types.SimpleNamespace(
        stack=types.SimpleNamespace(set_visible_child_name=lambda *_: None),
        loading_box=loading_box,
        recommendations=types.SimpleNamespace(get_selected_plugins=lambda: [plugin_data]),
        close=lambda: None,
        # Match an unpresented onboarding dialog, whose root is None.
        get_root=lambda: None,
    )
    fake_self = types.SimpleNamespace(onboarding_window=onboarding_window)

    # Called on the main thread, where run_on_main runs inline. The idle_adds
    # queue on the default context and are pumped below.
    OnboardingScreen5._on_start_button_click(fake_self)
    pump_main_context()

    assert len(toasts) == 1, (
        f"install failures must surface as ONE error toast on the surviving "
        f"main window (got {toasts}) -- pre-fix they only flashed on the "
        f"progress bar of the closing onboarding window"
    )
    assert "TestX" in toasts[0] and "Store" in toasts[0], (
        f"the toast must name the failed plugin and point at the store: {toasts[0]}"
    )

    print("PASS: onboarding install failures surface as a main-window toast")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_onboarding_store_offline")
    check_recommendations_offline()
    check_get_plugin_for_id_offline()
    check_missing_row_spinner_recovers()
    check_missing_row_reports_dependency_type()
    check_install_failures_toast()
    print("PASS: scenario_onboarding_store_offline")


if __name__ == "__main__":
    main()
