"""Arm the rpyc guard when this directory is on a backend child's PYTHONPATH.
This path serves children without plugin venvs, and failures must not stop them."""
import contextlib

with contextlib.suppress(Exception):
    import deckard_rpyc_guard  # noqa: F401

# Remove this guard-only path and load the shadowed distro or venv sitecustomize
# so the backend and its child processes retain their site customization.
try:
    import importlib
    import os
    import sys

    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
    del sys.modules["sitecustomize"]
    importlib.invalidate_caches()
    with contextlib.suppress(ImportError):
        import sitecustomize  # noqa: F401
except Exception:
    pass
