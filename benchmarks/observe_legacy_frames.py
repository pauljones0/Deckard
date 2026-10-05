#!/usr/bin/env python3
"""Validation-only wrapper: observe actual fake-HID images without patching sources.
This observer is never loaded during performance samples.
"""

import collections, hashlib, io, json, pathlib, runpy, sys, threading, time

source = pathlib.Path(sys.argv[1]).resolve()
output = pathlib.Path(sys.argv[2]).resolve()
sys.path.insert(0, str(source))
sys.argv = [str(source / "main.py"), *sys.argv[3:]]
from PIL import Image

counts = collections.Counter()
distinct = collections.defaultdict(set)
dimensions = {}
errors = []
lock = threading.Lock()


def capture(self, key, image, *args, **kwargs):
    try:
        with lock:
            data = bytes(image)
            counts[str(key)] += 1
            distinct[str(key)].add(hashlib.sha256(data).hexdigest())
            if str(key) not in dimensions:
                with Image.open(io.BytesIO(data)) as decoded:
                    dimensions[str(key)] = list(decoded.size)
    except Exception as error:
        errors.append(str(error))
    return original(self, key, image, *args, **kwargs)


import builtins

base_import = builtins.__import__


def intercept(name, *args, **kwargs):
    result = base_import(name, *args, **kwargs)
    module = sys.modules.get("src.backend.DeckManagement.Subclasses.FakeDeck")
    if (
        module is not None
        and hasattr(module, "FakeDeck")
        and module.FakeDeck.set_key_image is not capture
    ):
        global original
        original = module.FakeDeck.set_key_image
        module.FakeDeck.set_key_image = capture
        builtins.__import__ = base_import
    return result


builtins.__import__ = intercept
started = time.monotonic()
samples = []


def publish():
    while True:
        time.sleep(1)
        with lock:
            samples.append(
                {"elapsed": time.monotonic() - started, "writes": dict(counts)}
            )
            output.write_text(
                json.dumps(
                    {
                        "samples": samples[-120:],
                        "elapsed": time.monotonic() - started,
                        "writes": dict(counts),
                        "distinct": {k: len(v) for k, v in distinct.items()},
                        "dimensions": dimensions,
                        "errors": errors[-5:],
                    },
                    indent=2,
                )
            )


threading.Thread(target=publish, daemon=True).start()
runpy.run_path(str(source / "main.py"), run_name="__main__")
