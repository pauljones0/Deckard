"""Diff the official store catalog between two pinned commits.

Usage: vet_store_pin.py OLD_SHA NEW_SHA [--manifests] [--app-version X]

Support tool for bumping StoreBackend.STORE_PIN. For each catalog family
it prints the entries added, removed and repinned between the two refs,
with per-shape counts, and flags entries whose repository owner is not
in OfficialAuthors.json at the new ref. With --manifests it fetches the
manifest of EVERY pinned entry at the new ref, not only the changed
ones, and compares its minimum-app-version against the app version,
because a hash-shape entry auto-updates to whatever the catalog pins:
the update path trusts the pin, so the vet must confirm the app
satisfies every pinned plugin's requirement. The app version comes from
globals.py beside this repository, or from --app-version.

The exit code is the verdict: 0 only when the candidate ref pins every
entry to an immutable sha and every checked manifest passes the gate. A
plugin entry that names a branch fails the run, because the app follows
a branch tip in preference to any pin and a tip defeats the immutability
the pin exists to create. A value the tool cannot judge, a manifest it
cannot fetch, or a family missing at the new ref fails the run rather
than passing silently.

The pin decision mirrors the app's resolver: branch first for a plugin
entry, then a valid flat "hash", then the "commits" map at its newest
key, pre-releases ranking below their release the way the app's version
parse ranks them. No compatibility filter, because the diff must show
what the catalog pins, not what one machine would install. Stdlib only,
so it runs without the app's environment.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_RAW = "https://raw.githubusercontent.com/StreamController/StreamController-Store"
FAMILIES = ["Plugins.json", "Icons.json", "Wallpapers.json", "SDPlusBarWallpapers.json"]
COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def fetch(ref: str, path: str) -> bytes:
    url = f"{REPO_RAW}/{ref}/{path}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def _suffix_tokens(s: str) -> tuple:
    return tuple((0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"[.-]", s) if t)


def parse_version(v: str) -> tuple:
    """A sort key that ranks versions the way the app's parse does: by the
    numeric release first, and a pre-release below its own release, so
    "1.5.0" outranks "1.5.0-beta.15". Unparseable sorts below everything."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", v)
    if m is None:
        return ((), (0, ()))
    release = tuple(int(p) for p in m.group(1).split("."))
    rest = v[m.end():].lstrip(".-")
    if not rest:
        return (release, (1, ()))
    return (release, (0, _suffix_tokens(rest)))


def base_version(v: str) -> "tuple | None":
    """The leading numeric part, so "1.5.0-beta.15" compares as (1, 5, 0).
    The app's own gate strips pre-release segments the same way. None when
    nothing numeric leads, and the caller must fail the entry rather than
    treat it as satisfied."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", v)
    if m is None:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def app_version_from_repo() -> "str | None":
    globals_py = Path(__file__).resolve().parent.parent / "globals.py"
    try:
        text = globals_py.read_text()
    except OSError:
        return None
    m = re.search(r'^app_version:\s*str\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def pinned_sha(entry: dict, is_plugin: bool) -> "tuple[str | None, str]":
    """(sha, shape) for one entry, in the app's own precedence: a plugin's
    branch wins over any pin, then a valid hash, then the map."""
    branch = entry.get("branch")
    if is_plugin and branch is not None:
        return None, f"branch:{branch}"
    sha = entry.get("hash")
    if isinstance(sha, str) and COMMIT_SHA_RE.fullmatch(sha):
        return sha, "hash"
    commits = entry.get("commits")
    if isinstance(commits, dict) and commits:
        newest = max(commits, key=parse_version)
        return commits[newest], "commits"
    return None, "none"


def load_family(ref: str, family: str) -> "dict[str, dict] | None":
    try:
        entries = json.loads(fetch(ref, family))
    except urllib.error.HTTPError as e:
        print(f"   ({family} at {ref[:12]}: HTTP {e.code})")
        return None
    by_url = {}
    for entry in entries:
        url = entry.get("url")
        if url:
            by_url[url.rstrip("/")] = entry
    return by_url


def shape_counts(by_url: dict, is_plugin: bool) -> str:
    counts: dict[str, int] = {}
    for entry in by_url.values():
        shape = pinned_sha(entry, is_plugin)[1].split(":")[0]
        counts[shape] = counts.get(shape, 0) + 1
    return ", ".join(f"{n} {shape}" for shape, n in sorted(counts.items())) or "empty"


def owner(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return parts[-2] if len(parts) >= 2 else url


def check_manifests(to_review: list, app_version: str) -> int:
    app_base = base_version(app_version)
    if app_base is None:
        print(f"App version {app_version!r} is not parseable. Failing the run.")
        return 1
    print(f"\n== manifests of all {len(to_review)} pinned entries (app version: {app_version})")
    failures = 0
    for family, url, sha in to_review:
        raw_base = url.replace("github.com", "raw.githubusercontent.com")
        try:
            with urllib.request.urlopen(f"{raw_base}/{sha}/manifest.json", timeout=30) as resp:
                manifest = json.loads(resp.read())
        except Exception as e:
            failures += 1
            print(f"   FAIL {url}: manifest fetch failed: {e}")
            continue
        min_app = manifest.get("minimum-app-version")
        if min_app is None:
            verdict = "ok  "
        elif not isinstance(min_app, str) or base_version(min_app) is None:
            # The tool cannot judge it, so it must not pass it.
            verdict = "FAIL"
            failures += 1
        elif base_version(min_app) <= app_base:
            verdict = "ok  "
        else:
            verdict = "FAIL"
            failures += 1
        print(f"   {verdict} {url}: id={manifest.get('id')!r} version={manifest.get('version')!r}"
              f" min-app={min_app!r} app={manifest.get('app-version')!r}")
    return failures


def main() -> int:
    argv = sys.argv[1:]
    app_version = None
    if "--app-version" in argv:
        i = argv.index("--app-version")
        if i + 1 >= len(argv):
            print("--app-version needs a value")
            return 2
        app_version = argv[i + 1]
        del argv[i:i + 2]
    want_manifests = "--manifests" in argv
    argv = [a for a in argv if a != "--manifests"]
    if len(argv) != 2 or any(a.startswith("-") for a in argv):
        print(__doc__)
        return 2
    old_ref, new_ref = argv
    if app_version is None:
        app_version = app_version_from_repo()
    if want_manifests and app_version is None:
        print("No app version: globals.py was not readable and --app-version was not given.")
        return 2

    authors = set(json.loads(fetch(new_ref, "OfficialAuthors.json")))
    print(f"official authors at {new_ref[:12]}: {len(authors)}")

    problems = 0
    to_review: list = []  # (family, url, sha) of every pinned entry at the new ref
    for family in FAMILIES:
        is_plugin = family == "Plugins.json"
        old = load_family(old_ref, family) or {}
        new = load_family(new_ref, family)
        if new is None:
            # A family the app will fetch and fail on. Refuse the ref.
            print(f"== {family}: missing at {new_ref[:12]}. Failing the run.")
            problems += 1
            continue
        print(f"\n== {family}: {len(old)} -> {len(new)} entries")
        print(f"   old shapes: {shape_counts(old, is_plugin)}")
        print(f"   new shapes: {shape_counts(new, is_plugin)}")
        for url, entry in sorted(new.items()):
            sha, shape = pinned_sha(entry, is_plugin)
            if sha is not None:
                to_review.append((family, url, sha))
                continue
            problems += 1
            if shape.startswith("branch"):
                print(f"   MOVABLE  {url} ({shape}): the app follows this tip; it defeats the pin")
            else:
                print(f"   UNPINNED {url}: the app drops this entry")
        for url in sorted(new.keys() - old.keys()):
            sha, shape = pinned_sha(new[url], is_plugin)
            flag = "" if owner(url) in authors else "  [UNLISTED AUTHOR]"
            print(f"   ADDED    {url} @ {str(sha)[:12]} ({shape}){flag}")
        for url in sorted(old.keys() - new.keys()):
            print(f"   REMOVED  {url}")
        for url in sorted(new.keys() & old.keys()):
            old_sha = pinned_sha(old[url], is_plugin)[0]
            new_sha, shape = pinned_sha(new[url], is_plugin)
            if old_sha != new_sha:
                flag = "" if owner(url) in authors else "  [UNLISTED AUTHOR]"
                print(f"   REPINNED {url} {str(old_sha)[:12]} -> {str(new_sha)[:12]} ({shape}){flag}")

    if want_manifests:
        problems += check_manifests(to_review, app_version)

    if problems:
        print(f"\n{problems} problems. Do not pin this ref.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
