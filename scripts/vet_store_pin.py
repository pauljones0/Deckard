"""Diff the official store catalog between two pinned commits.

Usage: vet_store_pin.py OLD_SHA NEW_SHA [--manifests] [--app-version X]

Support tool for bumping StoreBackend.STORE_PIN. For each catalog family
it prints the entries added, removed and repinned between the two refs,
with per-shape counts, and flags entries whose repository owner is not
in OfficialAuthors.json at the new ref. With --manifests it also fetches
the manifest of every added or repinned plugin at its pinned revision
and compares its minimum-app-version against the app version, because a
hash-shape entry auto-updates to whatever the catalog pins: the update
path trusts the pin, so the vet must confirm the app satisfies every
pinned plugin's requirement. The app version comes from globals.py
beside this repository, or from --app-version.

The pin decision mirrors the app's resolver: a valid flat "hash" wins,
the "commits" map is the fallback, newest version first with no
compatibility filter, because the diff must show what the catalog pins,
not what one machine would install. Stdlib only, so it runs without the
app's environment.
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


def parse_version(v: str) -> tuple:
    parts = []
    for piece in re.split(r"[.-]", v):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def base_version(v: str) -> tuple:
    """The leading numeric part, so "1.5.0-beta.15" compares as (1, 5, 0).
    The app's own gate strips pre-release segments the same way."""
    m = re.match(r"\d+(?:\.\d+)*", v)
    if m is None:
        return ()
    return tuple(int(p) for p in m.group().split("."))


def app_version_from_repo() -> str | None:
    globals_py = Path(__file__).resolve().parent.parent / "globals.py"
    try:
        text = globals_py.read_text()
    except OSError:
        return None
    m = re.search(r'^app_version:\s*str\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def pinned_sha(entry: dict) -> tuple[str | None, str]:
    """(sha, shape) for one entry; shape names which key decided."""
    sha = entry.get("hash")
    if isinstance(sha, str) and COMMIT_SHA_RE.fullmatch(sha):
        return sha, "hash"
    commits = entry.get("commits")
    if isinstance(commits, dict) and commits:
        newest = max(commits, key=parse_version)
        return commits[newest], "commits"
    branch = entry.get("branch")
    if branch is not None:
        return None, f"branch:{branch}"
    return None, "none"


def load_family(ref: str, family: str) -> dict[str, dict]:
    try:
        entries = json.loads(fetch(ref, family))
    except urllib.error.HTTPError as e:
        print(f"   ({family} at {ref[:12]}: HTTP {e.code}; treated as empty)")
        return {}
    by_url = {}
    for entry in entries:
        url = entry.get("url")
        if url:
            by_url[url.rstrip("/")] = entry
    return by_url


def shape_counts(by_url: dict[str, dict]) -> str:
    counts: dict[str, int] = {}
    for entry in by_url.values():
        has_hash = isinstance(entry.get("hash"), str)
        has_map = bool(entry.get("commits")) and isinstance(entry.get("commits"), dict)
        shape = "mixed" if has_hash and has_map else "hash" if has_hash else "commits" if has_map else "other"
        counts[shape] = counts.get(shape, 0) + 1
    return ", ".join(f"{n} {shape}" for shape, n in sorted(counts.items())) or "empty"


def owner(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return parts[-2] if len(parts) >= 2 else url


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__)
        return 2
    old_ref, new_ref = args
    want_manifests = "--manifests" in sys.argv
    app_version = None
    if "--app-version" in sys.argv:
        app_version = sys.argv[sys.argv.index("--app-version") + 1]
    else:
        app_version = app_version_from_repo()

    authors = set(json.loads(fetch(new_ref, "OfficialAuthors.json")))
    print(f"official authors at {new_ref[:12]}: {len(authors)}")

    to_review: list[tuple[str, str, str]] = []  # (family, url, sha)
    for family in FAMILIES:
        old = load_family(old_ref, family)
        new = load_family(new_ref, family)
        print(f"\n== {family}: {len(old)} -> {len(new)} entries")
        print(f"   old shapes: {shape_counts(old)}")
        print(f"   new shapes: {shape_counts(new)}")
        for url in sorted(new.keys() - old.keys()):
            sha, shape = pinned_sha(new[url])
            flag = "" if owner(url) in authors else "  [UNLISTED AUTHOR]"
            print(f"   ADDED    {url} @ {str(sha)[:12]} ({shape}){flag}")
            if sha:
                to_review.append((family, url, sha))
        for url in sorted(old.keys() - new.keys()):
            print(f"   REMOVED  {url}")
        for url in sorted(new.keys() & old.keys()):
            old_sha, _ = pinned_sha(old[url])
            new_sha, shape = pinned_sha(new[url])
            if old_sha != new_sha:
                flag = "" if owner(url) in authors else "  [UNLISTED AUTHOR]"
                print(f"   REPINNED {url} {str(old_sha)[:12]} -> {str(new_sha)[:12]} ({shape}){flag}")
                if new_sha:
                    to_review.append((family, url, new_sha))

    if want_manifests and to_review:
        print(f"\n== manifests of the {len(to_review)} added/repinned entries (app version: {app_version})")
        gate_failures = 0
        for family, url, sha in to_review:
            raw_base = url.replace("github.com", "raw.githubusercontent.com")
            try:
                with urllib.request.urlopen(f"{raw_base}/{sha}/manifest.json", timeout=30) as resp:
                    manifest = json.loads(resp.read())
            except Exception as e:
                gate_failures += 1
                print(f"   FAIL {url}: manifest fetch failed: {e}")
                continue
            min_app = manifest.get("minimum-app-version")
            if app_version is None:
                verdict = "????"
            elif min_app is None or base_version(min_app) <= base_version(app_version):
                verdict = "ok  "
            else:
                verdict = "FAIL"
                gate_failures += 1
            print(f"   {verdict} {url}: id={manifest.get('id')!r} version={manifest.get('version')!r}"
                  f" min-app={min_app!r} app={manifest.get('app-version')!r}")
        if gate_failures:
            print(f"\n{gate_failures} entries fail the minimum-app-version gate or could not be read. Do not pin this ref.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
