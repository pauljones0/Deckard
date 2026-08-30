"""Verify permission toggles use each action's filtered index.
Enable writes valid own indices, disable writes None, and -1 or None indices are refused."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import types  # noqa: E402

from fixtures import start_watchdog  # noqa: E402
from src.windows.mainWindow.elements.Sidebar.elements.ActionManager import ActionRow  # noqa: E402


class _StubAction:
    def __init__(self, own_index):
        self._own_index = own_index

    def get_own_action_index(self):
        return self._own_index


def _toggle(own_index, active):
    # Call the method with a stub self, so no Gtk widget is realized. It reads
    # only self.action_object.
    stub = types.SimpleNamespace(action_object=_StubAction(own_index))
    return ActionRow._control_index_for_toggle(stub, active)


def main() -> int:
    start_watchdog(30, "permission_toggle_index")
    failures: list[str] = []

    # A failed sibling at slot 0 makes the second real action's filtered index
    # 1 while its raw row index is 2; enabling must write the filtered index.
    write, value = _toggle(own_index=1, active=True)
    if not (write and value == 1):
        failures.append(f"active with own_index 1 should write 1, got ({write}, {value})")

    # Turning it off writes None.
    write, value = _toggle(own_index=1, active=False)
    if not (write and value is None):
        failures.append(f"inactive should write None, got ({write}, {value})")

    # A -1 own index (screensaver showing the deck) is refused, not stored.
    write, value = _toggle(own_index=-1, active=True)
    if write:
        failures.append(f"a -1 own index must not be written, got ({write}, {value})")

    # A None own index (action absent) is refused.
    write, value = _toggle(own_index=None, active=True)
    if write:
        failures.append(f"a None own index must not be written, got ({write}, {value})")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: permission toggles write the filtered own index and refuse -1/None")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
