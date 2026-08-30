"""Reload installed plugins after invalidating import caches and report load failures."""

import importlib

from loguru import logger as log

import globals as gl


def reload_after_install(plugin_id: str) -> "str | None":
    """Reload after cache invalidation and return the installed plugin's load error."""
    importlib.invalidate_caches()
    plugin_manager = gl.plugin_manager
    if plugin_manager is None:
        return None
    plugin_manager.load_plugins()
    plugin_manager.init_plugins()
    plugin_manager.generate_action_index()
    error = plugin_manager.load_error_of(plugin_id)
    if error is not None:
        # Report that installed files did not produce a working plugin.
        body = f"{plugin_id} was installed but could not load: {error}"
        log.error(f"Install of {plugin_id}: {body}")
        gl.notify.error(body, title="Plugin not loaded")
    return error
