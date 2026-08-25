"""The plugin reload an install runs, made visible.

A store install downloads the tree, runs the gated install steps, and then
reloads the plugin layer in the running process. Two things go wrong at that
seam without help. The steps can pip-install the plugin's requirements, and
the import finders cache directory listings, so a package added to
site-packages mid-process stays invisible to the reload until the caches
drop; the plugin then imports only after an app restart. And a reload that
fails leaves the files on disk with the store reading installed, so the
failure needs to reach the user, not the log alone.
"""

import importlib

from loguru import logger as log

import globals as gl


def reload_after_install(plugin_id: str) -> "str | None":
    """Reload the plugin layer and report the installed plugin's load error.

    Drops the import-finder caches first, so a dependency the install steps
    just put into site-packages is importable in this process. Returns the
    recorded load failure of plugin_id, or None when it came up (or when no
    plugin manager exists to ask).
    """
    importlib.invalidate_caches()
    plugin_manager = gl.plugin_manager
    if plugin_manager is None:
        return None
    plugin_manager.load_plugins()
    plugin_manager.init_plugins()
    plugin_manager.generate_action_index()
    error = plugin_manager.load_error_of(plugin_id)
    if error is not None:
        # The files landed, so the store button reads installed, but the
        # reload could not bring the plugin up. Say so now: the success log
        # alone reads as a working install.
        body = f"{plugin_id} was installed but could not load: {error}"
        log.error(f"Install of {plugin_id}: {body}")
        gl.notify.error(body, title="Plugin not loaded")
    return error
