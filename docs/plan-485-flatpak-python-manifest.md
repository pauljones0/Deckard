# Flatpak Python manifest plan

## Problem

`requirements.txt` contains the vetted direct runtime dependencies, but
`pypi-requirements.yaml` contains an old environment freeze. The current
manifest includes packages outside the runtime closure and selects the full
OpenCV wheel instead of `opencv-python-headless`. `req2flatpak` does not resolve
transitive dependencies, so direct regeneration from `requirements.txt` would
also be incomplete.

## Design

1. Add a small requirements file for the pinned manifest-generation tools.
   `requirements-dev.txt` will include it instead of duplicating those pins.
2. Add a Python 3.13 generator that resolves `requirements.txt` into a committed
   transitive lock with pip-tools, then supplies that lock to req2flatpak for
   CPython 3.13 on x86_64 and aarch64. The generator will remove unstable tool
   paths and defective third-party header text from committed output.
3. Replace `pypi-requirements.yaml` with the generated two-architecture runtime
   closure.
4. Add a checker that downloads each selected archive from
   `files.pythonhosted.org`, verifies its committed SHA-256, and sums archive
   member sizes without extracting files. It will enforce separate x86_64 and
   aarch64 budgets.
5. Set the initial budgets from the measured clean closure plus 5%, rounded up
   to the next MiB: 288 MiB for x86_64 and 213 MiB for aarch64.
6. Run regeneration drift and payload checks in `build:flatpak`. This job is an
   existing direct dependency of `release:gate`, so a release cannot bypass the
   checks. Extend its merge-request change rules and cache inputs for all new
   manifest inputs.
7. Add offline scenario coverage for generator normalization, architecture
   selection, archive-size calculation, hash verification, and budget failure.

## Acceptance criteria

- When the generator runs with Python 3.13, the system shall resolve every
  direct and transitive runtime dependency into one pinned lock.
- When the generator reads the pinned lock, the system shall generate
  x86_64 and aarch64 Flatpak sources with stable URLs and SHA-256 values.
- When generated lock or manifest content differs from committed content, the
  check mode shall fail and show the stale file.
- When the clean manifest is generated, the system shall select
  `opencv-python-headless` and shall not select `opencv-python`.
- When the payload checker reads an archive, the system shall verify its
  SHA-256 before it counts uncompressed file-member sizes.
- When the selected payload is at or below 288 MiB for x86_64 and 213 MiB for
  aarch64, the payload checker shall pass.
- When either selected payload exceeds its architecture budget, the payload
  checker shall fail and identify the architecture, measured size, and budget.
- When a merge request changes a manifest input or checker, GitLab CI shall run
  `build:flatpak` automatically.
- When a mainline or release-tag pipeline runs, `build:flatpak` shall verify
  generation drift and both payload budgets before it builds the bundle.
- When implementation is complete, the repository shall pass the focused
  scenarios, full scenario suite, Ruff, ty, type-ignore guard, compile check,
  and module-size ratchet.
