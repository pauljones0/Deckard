# Flatpak Python manifest plan

## Problem

`requirements.txt` contains the vetted direct runtime dependencies, but
`pypi-requirements.yaml` contains an old environment freeze. The current
manifest includes packages outside the runtime closure and selects the full
OpenCV wheel instead of `opencv-python-headless`. `req2flatpak` does not resolve
transitive dependencies, so direct regeneration from `requirements.txt` would
also be incomplete.

## Design

1. Add small requirements files for pinned manifest-generation and Python build
   tools. `requirements-dev.txt` will include them instead of duplicating pins.
2. Add a Python 3.13 generator that resolves `requirements.txt` into a committed
   transitive runtime lock and the native-extension tools into a separate build
   lock. It will supply both locks to req2flatpak for CPython 3.13 on x86_64 and
   aarch64, and remove unstable tool paths and defective third-party header text
   from committed output.
3. Replace `pypi-requirements.yaml` with the generated two-architecture closure.
   The Flatpak build will install build backends into a temporary build-only
   target, install pycairo first, and then install the remaining locked runtime
   without shipping the build tools.
4. Add a checker that downloads each selected archive from
   `files.pythonhosted.org`, verifies its committed SHA-256, and sums archive
   member sizes without extracting files. It will enforce separate x86_64 and
   aarch64 budgets.
5. Set the initial budgets from the measured clean runtime and required build
   source closure plus 5%, rounded up to the next MiB: 297 MiB for x86_64 and
   222 MiB for aarch64.
6. Run regeneration drift and payload checks in a Python 3.13
   `test:flatpak-python` job that `build:flatpak` needs. The build is an existing
   direct dependency of `release:gate`, so a release cannot bypass the checks.
   Install the native headers required to read PyGObject source metadata, and
   give the test and build the same automatic rules for manifest inputs.
   Keep the download cache keyed by the app and Python manifests, which fully
   identify the sources that enter that cache.
7. Add offline scenario coverage for generator normalization, architecture
   selection, archive-size calculation, hash verification, and budget failure.

## Acceptance criteria

- When the generator runs with Python 3.13, the system shall resolve every
  direct and transitive runtime dependency and required build tool into separate
  pinned locks.
- When the generator reads the pinned lock, the system shall generate
  x86_64 and aarch64 Flatpak sources with stable URLs and SHA-256 values.
- When generated lock or manifest content differs from committed content, the
  check mode shall fail and show the stale file.
- When the clean manifest is generated, the system shall select
  `opencv-python-headless` and shall not select `opencv-python`.
- When Flatpak builds native Python extensions, the system shall make their
  pinned build backends available without installing those tools into the app.
- When the payload checker reads an archive, the system shall verify its
  SHA-256 before it counts uncompressed file-member sizes.
- When the selected payload is at or below 297 MiB for x86_64 and 222 MiB for
  aarch64, the payload checker shall pass.
- When either selected payload exceeds its architecture budget, the payload
  checker shall fail and identify the architecture, measured size, and budget.
- When a merge request changes a manifest input or checker, GitLab CI shall run
  `test:flatpak-python` before `build:flatpak` automatically.
- When a mainline or release-tag pipeline runs, `test:flatpak-python` shall
  verify generation drift and both payload budgets before `build:flatpak` builds
  the bundle.
- When implementation is complete, the repository shall pass the focused
  scenarios, full scenario suite, Ruff, ty, type-ignore guard, compile check,
  and module-size ratchet.
