import contextlib
import functools
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Concatenate, ParamSpec, TypeVar, cast

from gi.repository import GObject, Gtk

from typing import cast, TYPE_CHECKING

from loguru import logger as log


if TYPE_CHECKING:
    from src.backend.PluginManager.ActionCore import ActionCore

# The element's value type, e.g. bool for a toggle row or float for a scale.
T = TypeVar("T")
# Preserve a decorated method's parameters and return type through
# signal_manager, so the wrapped method keeps its own signature.
_Params = ParamSpec("_Params")
_Return = TypeVar("_Return")

class GenerativeUI[T](ABC):
    """
       Abstract base for dynamic UI elements linked to an ActionCore.

       Attributes:
           _action_core (ActionCore): The action this UI element is associated with.
           _var_name (str): The key used to store the value in the action's settings.
           _default_value (T): The default value for this UI element.
           on_change (Callable[[Gtk.Widget, T, T], None]): Function called when the value changes.
           _widget (Gtk.Widget): The GTK widget representing the UI element.
           _can_reset (bool): Whether the UI element can be reset to its default value.
           _auto_add (bool): Whether the UI element is automatically added to the action.
       """
    _action_core: "ActionCore"
    _var_name: str # name of the key in the actions settings
    _default_value: T # default value of the key
    # The first callback argument is the concrete row, or None while unbuilt.
    # It is Any because subclasses build specific Adw row types, not Gtk.Widget.
    on_change: Callable[[Any, T, T], None] | None
    _widget: Gtk.Widget | None # The actual widget of the UI Element; None until built and after destroy()
    _can_reset: bool
    _auto_add: bool
    _complex_var_name: bool

    # Classes that already logged the off-main forced-build note in
    # _ensure_built. The log holds one line per class, not one per instance.
    _logged_forced_build_classes: set[str] = set()

    def __init__(self, action_core: "ActionCore", var_name: str, default_value: T, can_reset: bool = True,
                 auto_add: bool = True, complex_var_name: bool = False,
                 on_change: Callable[[Any, T, T], None] | None = None,
                 build: Callable[[], None] | None = None):
        """
        Initialize without building; first .widget access builds it.
        Failed builds can retry, and unopened config sidebars allocate no Adw row tree.

        Args:
            action_core (ActionCore): The action this UI element is associated with.
            var_name (str): The key used to store the value in the action's settings.
            default_value (T): The default value for this UI element.
            can_reset (bool, optional): Whether the UI element can be reset. Defaults to True.
            auto_add (bool, optional): Whether the UI element is automatically added to the action. Defaults to True.
            on_change (Callable[[Gtk.Widget, T, T], None], optional): Function called when the value changes. Defaults to None.
            build (Callable[[], None], optional): Builds self._widget and subclass
                widget-only state on first access.
        """
        self._action_core = action_core
        self._var_name = var_name
        self._default_value = default_value
        self.on_change = on_change
        self._can_reset = can_reset
        self._auto_add = auto_add
        self._complex_var_name = complex_var_name
        self._widget = None
        # Store one handler id per connected key to make disconnect idempotent.
        # A reconnect cannot stack a second handler.
        self._signal_handlers: dict[str, int] = {}
        self._built = False
        self._build_flag_lock = threading.Lock()
        self._build_fn = build

        # Register before building; initialization and teardown both accept an unbuilt object.
        self._action_core.add_generative_ui_object(self)

    def _ensure_built(self) -> None:
        """Build on first access; off-main access uses the 30-second main-loop bound.
        Access before the config opens forces an eager build and logs once per class."""
        # The early flag stops recursion; build_fn stays outside the lock to avoid deadlock.
        # Before marshal, a main-thread reader can see no widget; callers run later or guard None.
        with self._build_flag_lock:
            if self._built:
                return
            self._built = True
        build_fn = self._build_fn
        if build_fn is None:
            return
        if threading.current_thread() is not threading.main_thread():
            cls_name = type(self).__name__
            if cls_name not in GenerativeUI._logged_forced_build_classes:
                GenerativeUI._logged_forced_build_classes.add(cls_name)
                log.debug(
                    f"{cls_name}: gen-ui widget forced at construction; will become config-open-only"
                )
        from GtkHelper.GtkHelper import run_on_main
        try:
            run_on_main(build_fn)
        except BaseException:
            # A failed build must not latch the object into a built state
            # with no widget. Let a later access retry.
            with self._build_flag_lock:
                self._built = False
            raise

    @abstractmethod
    def connect_signals(self) -> None:
        """Connects signals for the UI element."""
        pass

    @abstractmethod
    def disconnect_signals(self) -> None:
        """Disconnects signals for the UI element."""
        pass

    def _track_connect(self, key: str, widget: GObject.Object, signal: str,
                       callback: Callable[..., Any]) -> None:
        """Connect the callback once under key; repeated calls do not stack handlers."""
        if self._signal_handlers.get(key) is None:
            self._signal_handlers[key] = widget.connect(signal, callback)

    def _track_disconnect(self, key: str, widget: GObject.Object) -> None:
        """Disconnect the handler under key; repeated calls do nothing."""
        handler = self._signal_handlers.pop(key, None)
        if handler is not None:
            widget.disconnect(handler)

    @property
    def action_core(self) -> "ActionCore":
        """Returns the associated ActionCore instance."""
        return self._action_core

    @property
    def var_name(self) -> str:
        """Returns the variable name used in settings."""
        return self._var_name

    @property
    def default_value(self) -> T:
        """Returns the default value of the UI element."""
        return self._default_value

    @property
    def widget(self) -> Any:
        """Return the GTK widget, building it on first access."""
        self._ensure_built()
        # Back-reference so a container can recover the owning GenerativeUI object.
        if self._widget is not None:
            with contextlib.suppress(Exception):
                setattr(self._widget, "_generative_ui_owner", self)
        return self._widget

    @property
    def is_built(self) -> bool:
        """True after widget construction.
        Value-layer operations do not need this; widget sync uses it to avoid forcing a build."""
        return self._widget is not None

    @property
    def can_reset(self) -> bool:
        """Returns whether the UI element can be reset."""
        return self._can_reset

    @property
    def auto_add(self) -> bool:
        """Returns whether the UI element is automatically added to the action."""
        return self._auto_add

    @property
    def complex_var_name(self) -> bool:
        """Returns the complex variable name used in settings."""
        return self._complex_var_name

    @staticmethod
    def signal_manager(func: Callable[Concatenate[Any, _Params], _Return]) -> Callable[Concatenate[Any, _Params], _Return]:
        """
        Decorator to manage signal connections by disconnecting and reconnecting signals around the function call.

        Args:
            func (Callable): The function to wrap.

        Returns:
            Callable: The wrapped function.
        """

        @functools.wraps(func)
        def wrapper(self: Any, *args: _Params.args, **kwargs: _Params.kwargs) -> _Return:
            from GtkHelper.GtkHelper import run_on_main

            def _run() -> _Return:
                self.disconnect_signals()
                try:
                    return func(self, *args, **kwargs)
                finally:
                    self.connect_signals()

            return run_on_main(_run)

        # functools.wraps types its result as _Wrapped, which does not unify
        # with the Concatenate return annotation; the cast restores it.
        return cast(Callable[Concatenate[Any, _Params], _Return], wrapper)

    @abstractmethod
    @signal_manager
    def set_ui_value(self, value: T) -> None:
        """
        Sets the UI element to the specified value.

        Args:
            value (T): The value to set in the UI.
        """
        pass

    def _handle_value_changed(self, new_value: T, update_settings: bool = True, trigger_callback: bool = True) -> None:
        """
        Handles changes in the UI element's value.

        Args:
            new_value (T): The new value of the UI element.
        """
        old_value = self.get_value()

        if update_settings:
            self.set_value(new_value)

        if trigger_callback and self.on_change:
            # Pass the raw widget, which is None while unbuilt.
            # A value-layer callback must not force a widget build.
            self.on_change(self._widget, new_value, old_value)

    def update_value_in_ui(self) -> None:
        """Updates the UI element with the current value from settings."""
        value = self.get_value()
        self.set_ui_value(value)

    def reset_value(self) -> None:
        """Reset the value to its default.
        Sync only an existing widget so reset does not force a build.
        """
        self._handle_value_changed(self._default_value)
        if self._widget is not None:
            self.update_value_in_ui()

    def resolve_var_name(self) -> list[str]:
        keys = [self.var_name]

        if self.complex_var_name:
            keys = self.var_name.split('.')

        return keys

    def set_value(self, value: T) -> None:
        """
        Sets the value in the action's settings.

        Args:
            value (T): The value to set.
        """
        # A local annotation, not a cast. ActionCore.get_settings declares a
        # return type of dir, a typo for dict, so this file cannot use it.
        settings: dict[str, Any] = self._action_core.get_settings()

        keys = self.resolve_var_name()

        d = settings

        for key in keys[:-1]:
            if key not in d:
                d[key] = {}
            d = d[key]

        d[keys[-1]] = value

        self._action_core.set_settings(settings)

    def get_value(self, fallback: T | None = None) -> T:
        """
        Retrieves the value from the action's settings.

        Args:
            fallback (T, optional): The fallback value if the key is not found. Defaults to None.

        Returns:
            T: The retrieved value.
        """
        settings: dict[str, Any] = self._action_core.get_settings()

        keys = self.resolve_var_name()

        d: Any = settings
        for key in keys:
            if not isinstance(d, dict) or key not in d:
                return fallback if fallback is not None else self._default_value
            d = d[key]

        # The dict walk validates only intermediate nodes; plugin JSON can leave a wrong-typed leaf.
        # T is erased and rows have no validator, so set_ui_value receives the leaf unchecked.
        return cast("T", d)

    def load_initial_ui(self) -> None:
        """Loads the initial UI state based on the stored value."""
        value = self.get_value()
        self.set_ui_value(value)
        self._handle_value_changed(value, False)

    def load_ui_value(self) -> None:
        """Loads the UI element with the stored value."""
        value = self.get_value()
        self.set_ui_value(value)

    def get_translation(self, key: str | None, fallback: str | None = None) -> str:
        """
        Retrieves a translated string for the given key.

        Args:
            key (str | None): The translation key. A falsy key answers "".
            fallback (str, optional): The fallback text if translation is not found.

        Returns:
            str: The translated string.
        """
        return self._action_core.get_translation(key, fallback) if key else ""

    def unparent(self) -> None:
        """Remove an existing widget from its parent.
        An unbuilt widget makes this a no-op without forcing a build."""
        from GtkHelper.GtkHelper import run_on_main

        def _do() -> None:
            widget = self._widget
            if widget is not None and widget.get_parent():
                widget.unparent()
        run_on_main(_do)

    def destroy(self) -> None:
        """Disconnect signals, unparent the widget, and unregister; repeated calls do nothing.
        Do not dispose live Adw composites because GTK logs critical errors."""
        from GtkHelper.GtkHelper import run_on_main

        def _do() -> None:
            # An unbuilt widget has nothing to disconnect or unparent.
            # Use the raw field so teardown does not force a build.
            if self._widget is not None:
                with contextlib.suppress(Exception):
                    self.disconnect_signals()
            self._action_core.remove_generative_ui_object(self)
            widget = self._widget
            if widget is not None and widget.get_parent() is not None:
                widget.unparent()
            self._widget = None
        run_on_main(_do)

    def _create_reset_button(self) -> Gtk.Button:
        """Creates a reset button for the UI element."""
        button = Gtk.Button(icon_name="edit-undo-symbolic", vexpand=True, css_classes=["no-rounded-corners"],
                            overflow=Gtk.Overflow.HIDDEN)
        button.connect("clicked", lambda _: self.reset_value())
        return button

    def _get_suffix_box(self) -> "Gtk.Widget | None":
        """
        Retrieves the suffix box widget from the UI element.

        Returns:
            Gtk.Widget: The suffix box widget.
        """
        return cast("Gtk.Widget | None", self.widget.get_first_child().get_last_child())

    def _handle_reset_button_creation(self) -> None:
        """
        Handles the creation and addition of the reset button to the UI element.
        """

        if not self.can_reset:
            return

        self.widget.add_css_class("gen-ui-row")
        self.widget.set_overflow(Gtk.Overflow.HIDDEN)
        self.widget.get_child().add_css_class("gen-ui-box")
        self.widget.get_child().set_overflow(Gtk.Overflow.HIDDEN)

        suffix_box = self._get_suffix_box()
        if suffix_box:
            suffix_box.add_css_class("no-margin")

        if self._can_reset:
            self.widget.add_suffix(self._create_reset_button())
