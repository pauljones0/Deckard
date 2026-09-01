"""Tray object-registration checks that do not need a D-Bus daemon."""

import src.backend.trayicon as trayicon_mod
from src.backend.trayicon import DBusService, StatusNotifierItemService


class _StubBus:
    def __init__(self):
        self.registered: list[int] = []
        self.unregistered: list[int] = []
        self._next_id = 1

    def register_object(self, object_path, interface_info,
                        method_call_closure, get_property_closure):
        reg_id = self._next_id
        self._next_id += 1
        self.registered.append(reg_id)
        return reg_id

    def unregister_object(self, reg_id):
        self.unregistered.append(reg_id)

    def emit_signal(self, **kwargs):
        pass

    @property
    def live(self) -> set:
        return set(self.registered) - set(self.unregistered)


class _StubInterfaceInfo:
    def cache_build(self):
        pass

    def cache_release(self):
        pass


def check_base_double_register_no_orphan() -> None:
    bus = _StubBus()
    service = DBusService(_StubInterfaceInfo(), "/StubPath", bus)
    service.register()
    service.register()

    assert len(bus.live) == 1, (
        f"double register() leaked object registrations: registered "
        f"{bus.registered}, unregistered {bus.unregistered}; "
        f"{len(bus.live)} left live (expected 1)"
    )
    service.unregister()
    assert not bus.live, f"unregister() left registrations live: {bus.live}"
    print("PASS: base DBusService double register() keeps exactly one live "
          "registration")


def check_sni_double_register_keeps_menu_live() -> None:
    original_watch = trayicon_mod.Gio.bus_watch_name_on_connection
    original_unwatch = trayicon_mod.Gio.bus_unwatch_name
    trayicon_mod.Gio.bus_watch_name_on_connection = lambda *args, **kwargs: 12345
    trayicon_mod.Gio.bus_unwatch_name = lambda *args, **kwargs: None
    try:
        bus = _StubBus()
        sni = StatusNotifierItemService(session_bus=bus, menu_items=[])

        sni.register()
        sni_id = sni.registration_id
        menu_id = sni._menu.registration_id
        assert sni_id is not None, "SNI object failed to register"
        assert menu_id is not None, "menu object failed to register"

        sni.register()

        assert sni.registration_id is not None, (
            "SNI object registration lost after double register()"
        )
        assert sni._menu.registration_id is not None, (
            "double register() left the tray menu object unregistered; "
            f"registered={bus.registered} unregistered={bus.unregistered}"
        )
        assert len(bus.live) == 2, (
            "double register() must keep exactly the SNI and menu objects live; "
            f"registered={bus.registered}, unregistered={bus.unregistered}, "
            f"live={bus.live}"
        )
        assert sni.registration_id == sni_id, (
            f"SNI object id changed on double register(): {sni_id} -> "
            f"{sni.registration_id}"
        )
        assert sni._menu.registration_id == menu_id, (
            f"menu object id changed on double register(): {menu_id} -> "
            f"{sni._menu.registration_id}"
        )

        sni.unregister()
        assert not bus.live, f"unregister() left registrations live: {bus.live}"
        sni.register()
        assert sni.registration_id is not None
        assert sni._menu.registration_id is not None
        assert len(bus.live) == 2, (
            f"stop/start leaked registrations: live={bus.live} (expected 2)"
        )
        sni.unregister()
    finally:
        trayicon_mod.Gio.bus_watch_name_on_connection = original_watch
        trayicon_mod.Gio.bus_unwatch_name = original_unwatch
    print("PASS: StatusNotifierItemService double register() keeps both the "
          "SNI and menu objects live")
