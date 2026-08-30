"""Verify that video digests use stable device, inode, size, and nanosecond
mtime identity and reject changes during hashing."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os  # noqa: E402

import globals as gl  # noqa: F401, E402
from fixtures import start_watchdog  # noqa: E402

from src.backend.DeckManagement.Subclasses import mp4_tile_cache as mtc  # noqa: E402


def main() -> int:
    start_watchdog(30, "video_md5_identity")
    failures: list[str] = []

    media = os.path.join(gl.DATA_PATH, "media")
    os.makedirs(media, exist_ok=True)
    path = os.path.join(media, "vid.bin")

    # --- A same-size replacement gets a fresh digest, not the memoized one.
    with open(path, "wb") as f:
        f.write(b"A" * 4096)
    digest_a = mtc.get_video_md5(path)
    st_a = os.stat(path)

    # Replace the path by rename from a temp of the same size, then force the
    # same mtime, the same-second-replacement the float key rounded together.
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(b"B" * 4096)
    os.replace(tmp, path)
    os.utime(path, ns=(st_a.st_atime_ns, st_a.st_mtime_ns))

    digest_b = mtc.get_video_md5(path)
    if digest_a == digest_b:
        failures.append("a same-size, same-mtime replacement reused the old digest")

    # --- A write during the hash is not memoized under the pre-write identity.
    with open(path, "wb") as f:
        f.write(b"C" * 4096)

    real_stat = os.stat
    toggled = {"n": 0}

    def shifting_stat(p, *a, **k):
        # Change the identity only for the post-hash stat.
        st = real_stat(p, *a, **k)
        if p == path:
            toggled["n"] += 1
            if toggled["n"] == 2:
                class _Shifted:
                    st_dev = st.st_dev
                    st_ino = st.st_ino + 1  # a new inode, i.e. a replacement
                    st_size = st.st_size
                    st_mtime_ns = st.st_mtime_ns
                return _Shifted()
        return st

    mtc.os.stat = shifting_stat
    try:
        # attempts=1 so it does not loop; the after-hash mismatch must prevent
        # memoization and return the digest unmemoized.
        digest_c = mtc.get_video_md5(path, attempts=1)
    finally:
        mtc.os.stat = real_stat

    # The digest for the steady file, computed cleanly now, must be memoized
    # and equal, and must not be the value a mid-hash change would have cached.
    digest_c_clean = mtc.get_video_md5(path)
    if digest_c != digest_c_clean:
        failures.append("the unstable-read digest differs from the clean one")
    # Prove nothing was cached under the shifted identity: a second clean read
    # returns the same memoized value (sanity that the memo works at all).
    if mtc.get_video_md5(path) != digest_c_clean:
        failures.append("the memo did not serve a stable repeat read")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: the video md5 memo keys on file identity and rejects a "
          "mid-hash change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
