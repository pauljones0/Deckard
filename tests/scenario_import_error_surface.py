"""Surface a failed StreamDeck-UI import and close its dialog.
GTK updates must run on the main thread, and failure must not call on_finished."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os
import threading
import time
import types

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib

from loguru import logger as log

import globals as gl


class FakeProgressBar:
    """Records every set_text/set_fraction call and the calling thread."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.fractions: list[float] = []
        self.threads: list[threading.Thread] = []

    def set_text(self, text: str) -> None:
        self.texts.append(text)
        self.threads.append(threading.current_thread())

    def set_fraction(self, fraction: float) -> None:
        self.fractions.append(fraction)
        self.threads.append(threading.current_thread())


class FakeImporterSelf:
    """Stand in for Importer while using its real show_error method."""

    def __init__(self) -> None:
        self.progess_bar = FakeProgressBar()
        self.closed = False
        self.close_threads: list[threading.Thread] = []

    def close(self) -> bool:
        self.closed = True
        self.close_threads.append(threading.current_thread())
        return False  # do not reschedule the timeout


def _pump(ctx: GLib.MainContext, predicate, budget: float) -> None:
    """Iterate until the predicate or budget expires.
    The budget must exceed show_error's three-second close timeout."""
    deadline = time.monotonic() + budget
    while not predicate() and time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.02)


def main() -> int:
    fixtures.start_watchdog(30, "import_error_surface")

    from src.windows.PageManager.Importer.Importer import Importer
    from src.windows.PageManager.Importer.StreamDeckUI.StreamDeckUI import (
        StreamDeckUIImporter,
    )

    # Capture ERROR records to verify logging and user-visible surfacing.
    logged: list[str] = []

    def _sink(message) -> None:
        logged.append(message.record["message"])

    sink_id = log.add(_sink, level="ERROR")

    # A valid export file, so the os.path.exists and json.load pre-checks pass
    # and control reaches perform_import, where the injected failure lives.
    export_path = os.path.join(gl.DATA_PATH, "sdui_export.json")
    with open(export_path, "w") as f:
        json.dump({"state": {}}, f)

    # Inject an unexpected perform_import failure.
    def _boom(self) -> None:
        raise PermissionError("injected import failure")

    StreamDeckUIImporter.perform_import = _boom

    fake = FakeImporterSelf()
    # Bind the real error-and-close path onto the duck-typed self.
    fake.show_error = types.MethodType(Importer.show_error, fake)

    finished: list[bool] = []

    def on_finished() -> None:
        finished.append(True)

    failures: list[str] = []

    # Run exactly as production does: the unbound method on a worker thread.
    worker = threading.Thread(
        target=Importer.import_from_streamdeck_ui,
        args=(fake, export_path, on_finished),
        name="import_from_streamdeck_ui",
    )
    worker.start()
    worker.join(timeout=5)

    if worker.is_alive():
        failures.append("the import worker thread hung and never returned")

    # The worker must not touch the progress bar directly. Every touch is
    # deferred to the main context through show_error's GLib.idle_add.
    if fake.progess_bar.texts or fake.progess_bar.fractions:
        failures.append(
            "the progress bar was touched from the worker thread before the "
            f"main context ran: texts={fake.progess_bar.texts}"
        )

    ctx = GLib.MainContext.default()
    _pump(ctx, lambda: fake.closed, budget=6.0)

    if "Import failed" not in fake.progess_bar.texts:
        failures.append(
            f"error not surfaced; progress bar texts were {fake.progess_bar.texts}"
        )

    if not fake.closed:
        failures.append(
            "the dialog never closed after the import failed (the reported hang)"
        )

    off_main = [t for t in fake.progess_bar.threads + fake.close_threads
                if t is not threading.main_thread()]
    if off_main:
        failures.append(f"a GTK touch ran off the main thread: {off_main}")

    if finished:
        failures.append("on_finished fired even though the import failed")
    if "Imported!" in fake.progess_bar.texts:
        failures.append("the dialog reported 'Imported!' for a failed import")

    if not any("import failed" in m.lower() for m in logged):
        failures.append(f"the failure was not logged at ERROR level: {logged}")

    log.remove(sink_id)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print("PASS: a failed StreamDeck-UI import surfaces the error, closes the "
          "dialog on the main thread, logs the failure, and never reports "
          "success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
