#!/usr/bin/env bash
# Link the primary checkout's local environment into this worktree.
#
# Run it once, from anywhere inside a linked worktree:
#
#     bash scripts/worktree-env.sh
#
# Worktrees carry no local environment of their own, on purpose: they run
# with the primary checkout's. Two entries need to exist at the checkout
# root for root-anchored tooling to work there, and both arrive as symlinks
# to the primary's:
#
#   .venv    pyright resolves the environment from the checkout root it is
#            analysing (venvPath/venv in [tool.pyright]). A worktree with no
#            .venv loses every third-party import -- loguru degrades to Any,
#            and with it everything behind a @log.catch.
#   .claude  a session started inside the worktree reads its project
#            configuration from the worktree root. The directory is local
#            and untracked, so without the link such a session starts with
#            no project instructions and no local settings. The link is
#            self-referential in principle (the worktree lives inside the
#            directory it links); recursive scans are safe because none of
#            the usual tools follow directory symlinks -- never pass a
#            follow-symlinks flag to one from a worktree root.
#
# .gitignore covers both names with slashless entries, which is what keeps
# git from staging the symlinks: a "dir/" pattern does not match a symlink.
# The links still make git worktree remove refuse; use --force.
#
# Links are relative when the worktree sits under the primary checkout,
# which the .claude/worktrees convention guarantees, so the repo directory
# can move without breaking them.
set -euo pipefail

fail() {
    echo "worktree-env: $*" >&2
    exit 1
}

top="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || fail "not inside a git checkout"
cd "$top"

git_dir="$(git rev-parse --path-format=absolute --git-dir)"
common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"

# In the primary checkout the two are the same directory. The primary owns
# the real entries; a link there would be self-referential or clobber them.
if [ "$git_dir" = "$common_dir" ]; then
    fail "this is the primary checkout, which owns the real environment; run this inside a linked worktree"
fi

primary="$(dirname "$common_dir")"

# Symlink one entry from the primary checkout, refusing anything that is
# already there and is not the expected link. required=yes fails on a
# missing source; required=no skips it, for a checkout that never had one.
link_to_primary() {
    local name="$1" required="$2" target
    target="$primary/$name"

    if [ ! -e "$target" ]; then
        if [ "$required" = "yes" ]; then
            fail "the primary checkout has no $name at $target, so there is nothing to link"
        fi
        echo "worktree-env: the primary checkout has no $name; skipped"
        return 0
    fi

    if [ -L "$name" ]; then
        if [ "$(realpath "$name")" = "$(realpath "$target")" ]; then
            echo "worktree-env: $name already links to the primary's"
            return 0
        fi
        fail "$name is a symlink to $(readlink "$name"), which is not the primary's; remove it first"
    fi

    if [ -e "$name" ]; then
        fail "$name exists and is not a symlink; a worktree must not carry its own"
    fi

    ln -s "$(realpath --relative-to="$top" "$target")" "$name"
    echo "worktree-env: linked $name -> $(readlink "$name")"
}

link_to_primary .venv yes
link_to_primary .claude no
