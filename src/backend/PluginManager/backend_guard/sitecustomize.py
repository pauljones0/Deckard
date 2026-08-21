"""Imported by `site` when this directory rides on a child's PYTHONPATH.

It arms the rpyc loopback guard in a backend that runs on the app's own
interpreter. No plugin venv exists there to carry the .pth file. A failure
here must never stop the child interpreter.
"""
try:
    import deckard_rpyc_guard  # noqa: F401
except Exception:
    pass

# This directory is on PYTHONPATH only to arm the guard, and Python loads one
# module named sitecustomize, so this file shadows a distro or venv
# sitecustomize (for example Debian's apport hook) here and in every child
# process. Drop this directory from the path and load the shadowed one, so
# the backend keeps whatever site customization it had.
try:
    import importlib
    import os
    import sys

    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
    del sys.modules["sitecustomize"]
    importlib.invalidate_caches()
    try:
        import sitecustomize  # noqa: F401
    except ImportError:
        pass
except Exception:
    pass
