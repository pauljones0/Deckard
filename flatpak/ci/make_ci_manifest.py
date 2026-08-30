#!/usr/bin/env python3
"""Derive a throwaway CI Flatpak manifest in place.
Usage: make_ci_manifest.py <manifest.yml> <src-dir-relative-to-manifest>."""
import sys

import yaml


def main() -> int:
    manifest_path, src_dir = sys.argv[1], sys.argv[2]
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    # Replace only Deckard's sources with the staged clean Git archive directory.
    for module in manifest["modules"]:
        if isinstance(module, dict) and module.get("name") == "Deckard":
            module["sources"] = [{"type": "dir", "path": src_dir}]
            break
    else:
        print("error: no 'Deckard' module in the manifest", file=sys.stderr)
        return 1

    # The rewrite happens in place, and it loses the comments and the
    # formatting of the source manifest.
    with open(manifest_path, "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False, width=100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
