"""Verify set, unchanged, and cleared page-background viewport settings.
Also verify that background loading renders the stored page view."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import os

import globals as gl
from fixtures import make_headless_controller, make_test_png, start_watchdog, teardown


def main() -> int:
    start_watchdog(90, "page_background_view")
    controller = make_headless_controller(serial="page-view-1")
    failures: list[str] = []
    try:
        page = controller.active_page
        page_path = page.json_path
        media = make_test_png(os.path.join(gl.DATA_PATH, "media", "page_bg.png"),
                              color=(200, 40, 40))
        pm = gl.page_manager

        # Store the view beside other override keys
        pm.overwrite_background_settings(page_path, overwrite=True, show=True,
                                         media_path=media,
                                         view={"x": 0.2, "y": 0.5, "scale": 2.0})
        stored = pm.get_background_settings(page_path)
        if stored.get("view") != {"x": 0.2, "y": 0.5, "scale": 2.0}:
            failures.append(f"the view was not stored: {stored}")
        if stored.get("media-path") != media or not stored.get("overwrite"):
            failures.append(f"sibling keys were disturbed by the view write: {stored}")

        # Keep the view when another key changes
        pm.overwrite_background_settings(page_path, fps=15)
        stored = pm.get_background_settings(page_path)
        if stored.get("view") != {"x": 0.2, "y": 0.5, "scale": 2.0}:
            failures.append(f"a write of another key changed the view: {stored}")
        if stored.get("fps") != 15:
            failures.append(f"the other key did not land: {stored}")

        # Load the stored page view
        controller.load_background(page, update=False)
        image = controller.background.image
        if image is None or image.view != (0.2, 0.5, 2.0):
            failures.append(f"load_background did not render the page view: "
                            f"{None if image is None else image.view}")

        # Clear the key to restore the default view
        pm.overwrite_background_settings(page_path, view=None)
        stored = pm.get_background_settings(page_path)
        if "view" in stored:
            failures.append(f"view=None did not clear the key: {stored}")
        controller.load_background(page, update=False)
        image = controller.background.image
        if image is None or image.view != (0.5, 0.5, 1.0):
            failures.append(f"a cleared view did not render as the default: "
                            f"{None if image is None else image.view}")
    finally:
        teardown(controller)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: the page override stores, keeps, renders and clears its view")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
