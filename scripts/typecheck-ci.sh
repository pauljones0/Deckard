#!/usr/bin/env bash
# Run mypy the way CI's lint:types job runs it.
#
# CI installs only the type checker and its stubs, and treats every runtime
# dependency as Any through ignore_missing_imports. The local .venv has the
# real, typed dependencies installed (Pillow, requests, loguru, ...), so a
# `# type: ignore` or a re-export can pass `.venv/bin/python -m mypy` and still
# fail the CI gate, and the reverse. Run this next to the local mypy so both
# views are green before a push.
#
# The installed packages must stay in step with the lint:types job in
# .gitlab-ci.yml. Both read their pinned versions from the same requirements
# files, so only the package list can drift.
#
# The environment is cached in .typecheck-ci-venv (gitignored) and rebuilt
# when either requirements file changes.
set -euo pipefail
cd "$(dirname "$0")/.."

venv=".typecheck-ci-venv"
stamp="$venv/.stamp"
want="$(sha1sum requirements-dev.txt requirements.txt | sha1sum | cut -c1-16)"

if [ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]; then
    echo "typecheck-ci: building $venv to match CI's lint:types job..." >&2
    rm -rf "$venv"
    python3 -m venv "$venv"
    "$venv/bin/pip" install --quiet --upgrade pip
    "$venv/bin/pip" install --quiet \
        $(grep -oE '^(mypy|types-setuptools|types-requests)==[^ #]+' requirements-dev.txt) typing_extensions
    # --no-deps for the stubs: PyGObject-stubs pulls in PyGObject, whose pycairo
    # chain needs a C toolchain the check does not.
    "$venv/bin/pip" install --quiet --no-deps \
        $(grep -oE '^PyGObject-stubs==[^ #]+' requirements-dev.txt)
    # Mirror any extra runtime package the lint:types job installs. Reading the
    # job instead of hardcoding keeps this script honest when that list moves:
    # a package installed there and not here (or the reverse) makes this check
    # disagree with the gate it exists to predict.
    for pkg in $(sed -n '/^lint:types:/,/^[a-z]/p' .gitlab-ci.yml \
                 | grep -oE "\^([a-z0-9_-]+)==" | tr -d '^=' \
                 | grep -vE '^(mypy|types-setuptools|types-requests|PyGObject-stubs)$'); do
        spec=$(grep -hoE "^${pkg}==[^ #]+" requirements.txt requirements-dev.txt | head -1)
        [ -n "$spec" ] && "$venv/bin/pip" install --quiet "$spec"
    done
    printf '%s\n' "$want" > "$stamp"
fi

exec "$venv/bin/python" -m mypy "$@"
