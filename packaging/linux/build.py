#!/usr/bin/env python3
"""Build relocatable Linux bundles and native installers from the verified tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import zipfile
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
VERSION = (ROOT / "VERSION").read_text().strip()
ARCH = platform.machine()
APP_ID = "io.github.nazbert.Deckard"


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def copy_application(prefix, bundled=True):
    (prefix / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "target/release/deckard", prefix / "bin/deckard-bin")
    if bundled:
        (prefix / "bin/deckard").write_text('#!/bin/sh\nbundle="$(CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")/.." && pwd)"\nexport PATH="$bundle/bin:$PATH"\nexport LD_LIBRARY_PATH="$bundle/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\nexec "$bundle/bin/deckard-bin" "$@"\n')
        (prefix / "bin/deckard").chmod(0o755)
        for program in ("ffmpeg", "ffprobe", "xdotool", "wtype", "pactl", "ping"):
            shutil.copy2(shutil.which(program), prefix / "bin" / program)
    else:
        (prefix / "bin/deckard").symlink_to("deckard-bin")
    shutil.copy2(ROOT / "packaging/linux/install-udev.sh", prefix / "bin/install-udev.sh")
    (prefix / "bin/install-udev.sh").chmod(0o755)
    (prefix / "share/applications").mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/linux/deckard.desktop", prefix / "share/applications/deckard.desktop")
    shutil.copytree(ROOT / "Assets/icons/hicolor", prefix / "share/icons/hicolor")
    share = prefix / "share/deckard"
    share.mkdir(parents=True)
    shutil.copy2(ROOT / "udev.rules", share / "60-deckard.rules")
    for name in ("LICENSE", "README.md", "VERSION"):
        shutil.copy2(ROOT / name, share / name)
    if (ROOT / "docs").exists():
        shutil.copytree(ROOT / "docs", share / "docs")
    benchmark_docs = share / "benchmarks"
    benchmark_docs.mkdir()
    shutil.copy2(ROOT / "benchmarks/README.md", benchmark_docs / "README.md")
    if (ROOT / "benchmarks/results").exists():
        shutil.copytree(ROOT / "benchmarks/results", benchmark_docs / "results")
    plugin = share / "plugins/example"
    plugin.mkdir(parents=True)
    shutil.copy2(ROOT / "examples/plugin/manifest.json", plugin / "manifest.json")
    shutil.copy2(ROOT / "target/release/deckard-plugin-example", plugin / "deckard-plugin-example")


def copy_libraries(prefix):
    source_packages = {"ffmpeg", "xdotool", "wtype", "pulseaudio-utils", "iputils-ping"}
    library = prefix / "lib"
    library.mkdir()
    seeds = list((prefix / "bin").iterdir())
    # Keep graphics drivers on the host. Bundle the client interfaces that egui
    # dlopens and the FFmpeg/input-helper dependency closure. C++/GCC runtimes
    # stay on the host too: older copies prevent newer Mesa drivers from loading.
    triplet = "x86_64-linux-gnu" if ARCH == "x86_64" else "aarch64-linux-gnu"
    system = Path("/usr/lib") / triplet
    for pattern in ("libxkbcommon.so.*", "libxkbcommon-x11.so.*", "libvulkan.so.*", "libwayland-client.so.*", "libwayland-cursor.so.*", "libX11.so.*", "libXcursor.so.*", "libXi.so.*", "libXrandr.so.*"):
        seeds.extend(system.glob(pattern))
    visited = set()
    excluded = re.compile(r"^(libc\.so|libm\.so|libdl\.so|libpthread\.so|librt\.so|libstdc\+\+\.so|libgcc_s\.so|ld-linux|libGL[EX]|libGL\.so|libEGL|libGLdispatch|libGLES|libgbm|libdrm)")
    while seeds:
        path = seeds.pop()
        if str(path) in visited:
            continue
        visited.add(str(path))
        output = subprocess.run(["ldd", str(path)], text=True, capture_output=True)
        for resolved in re.findall(r"=> (/\S+)", output.stdout):
            dependency = Path(resolved)
            if excluded.match(dependency.name):
                continue
            destination = library / dependency.name
            if not destination.exists():
                shutil.copy2(dependency, destination)
                seeds.append(dependency)
        if path.parent == system and not excluded.match(path.name):
            if not (library / path.name).exists():
                shutil.copy2(path, library / path.name)
    for path in list(library.rglob("*.so*")) + list((prefix / "bin").iterdir()):
        if not path.is_file() or path.read_bytes()[:4] != b"\x7fELF":
            continue
        old = subprocess.run(["patchelf", "--print-rpath", str(path)], capture_output=True, text=True)
        if old.returncode == 0:
            relative = os.path.relpath(library, path.parent)
            run("patchelf", "--set-rpath", f"$ORIGIN/{relative}", path)
    licenses = prefix / "share/deckard/licenses"
    licenses.mkdir()
    shutil.copy2(ROOT / "Assets/Fonts/Attribution.md", licenses / "fonts.md")
    shutil.copy2(ROOT / "Assets/Fonts/LICENSE.txt", licenses / "Roboto-Apache-2.0.txt")
    for library_path in library.iterdir():
        owner = subprocess.run(["dpkg-query", "-S", str(Path("/usr/lib") / triplet / library_path.name)], text=True, capture_output=True)
        if owner.returncode != 0:
            owner = subprocess.run(["dpkg-query", "-S", f"*/{library_path.name}"], text=True, capture_output=True)
        if owner.returncode == 0:
            package = owner.stdout.split(": ", 1)[0].split(":", 1)[0]
            source_packages.add(package)
            copyright = Path("/usr/share/doc") / package / "copyright"
            if copyright.exists(): shutil.copy2(copyright, licenses / f"{package}-copyright")
    for name in ("ffmpeg", "xdotool", "wtype", "pulseaudio-utils", "iputils-ping"):
        copyright = Path("/usr/share/doc") / name / "copyright"
        if copyright.exists():
            shutil.copy2(copyright, licenses / f"{name}-copyright")
    metadata = json.loads(subprocess.check_output(["cargo", "metadata", "--locked", "--format-version", "1"], cwd=ROOT))
    packages = [{"name": p["name"], "version": p["version"], "license": p["license"], "source": p["source"]} for p in metadata["packages"]]
    (licenses / "rust-dependencies.json").write_text(json.dumps(packages, indent=2) + "\n")
    for package in metadata["packages"]:
        source = Path(package["manifest_path"]).parent
        for path in source.glob("*"):
            if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                destination = licenses / "rust" / f"{package['name']}-{package['version']}" / path.name
                destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, destination)
    for path in Path('/root/.cargo/registry/src').glob('*/turbojpeg-sys-*/libjpeg-turbo/LICENSE*'):
        shutil.copy2(path, licenses / f"libjpeg-turbo-{path.name}")
    source_records = []
    for package in sorted(source_packages):
        info = subprocess.check_output(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}", package], text=True).split("\t")
        binary, version, source, source_version = info
        metadata = subprocess.check_output(["apt-cache", "showsrc", source], text=True)
        record = next((paragraph for paragraph in metadata.split("\n\n") if f"\nVersion: {source_version}\n" in "\n" + paragraph + "\n"), None)
        if record is None: raise ValueError(f"No exact corresponding source record for {package} {source_version}")
        directory = re.search(r"^Directory: (.+)$", record, re.M).group(1)
        block = re.search(r"^Checksums-Sha256:\n((?: .+\n?)+)", record, re.M).group(1)
        files = []
        for line in block.splitlines():
            digest, size, name = line.split()
            files.append({"url": f"https://archive.ubuntu.com/ubuntu/{directory}/{name}", "sha256": digest, "size": int(size)})
        source_records.append({"binary_package": binary, "binary_version": version, "source_package": source, "source_version": source_version, "files": files})
    (licenses / "ubuntu-sources.json").write_text(json.dumps(source_records, indent=2) + "\n")
    # Executable distributions contain no interpreter, Python source, or Python libraries.
    forbidden = [p for p in prefix.rglob("*") if p.suffix in (".py", ".pyc", ".pyo") or "libpython" in p.name or re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?", p.name)]
    if forbidden:
        raise ValueError(f"Python runtime leaked into native bundle: {forbidden}")


def desktop_and_rules(staging):
    applications = staging / "usr/share/applications"
    applications.mkdir(parents=True)
    text = (ROOT / "packaging/linux/deckard.desktop").read_text().replace("Exec=deckard", "Exec=/opt/deckard/bin/deckard")
    (applications / "deckard.desktop").write_text(text)
    icons = staging / "usr/share/icons/hicolor/512x512/apps"
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / f"Assets/icons/hicolor/512x512/apps/{APP_ID}.png", icons)
    rules = staging / "usr/lib/udev/rules.d"
    rules.mkdir(parents=True)
    shutil.copy2(ROOT / "udev.rules", rules / "60-deckard.rules")
    (staging / "usr/bin").mkdir()
    (staging / "usr/bin/deckard").symlink_to("/opt/deckard/bin/deckard")


def deb(prefix, output, work):
    staging = work / "deb"
    shutil.copytree(prefix, staging / "opt/deckard", symlinks=True)
    desktop_and_rules(staging)
    control = staging / "DEBIAN"
    control.mkdir()
    arch = "amd64" if ARCH == "x86_64" else "arm64"
    (control / "control").write_text(f"Package: deckard\nVersion: {VERSION}\nArchitecture: {arch}\nMaintainer: Deckard contributors\nSection: utils\nPriority: optional\nDepends: libc6 (>= 2.35), libstdc++6 (>= 12), libgcc-s1, libgl1, libegl1, udev, xdg-utils\nHomepage: https://github.com/pauljones0/Deckard\nDescription: Native Rust Stream Deck controller\n")
    (control / "postinst").write_text("#!/bin/sh\nset -e\nif command -v udevadm >/dev/null; then\n udevadm control --reload-rules || true\n udevadm trigger --subsystem-match=usb || true\n udevadm trigger --subsystem-match=hidraw || true\n udevadm trigger --subsystem-match=misc || true\nfi\nif command -v modprobe >/dev/null; then modprobe uinput || true; fi\n")
    (control / "postinst").chmod(0o755)
    run("dpkg-deb", "--build", "--root-owner-group", staging, output / f"deckard-{VERSION}-{ARCH}.deb")


def rpm(prefix, output, work):
    top = work / "rpm"
    for name in ("SPECS", "SOURCES", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
        (top / name).mkdir(parents=True)
    staging = top / "payload"
    shutil.copytree(prefix, staging / "opt/deckard", symlinks=True)
    desktop_and_rules(staging)
    spec = top / "SPECS/deckard.spec"
    spec.write_text(f"""Name: deckard
Version: {VERSION}
Release: 1
Summary: Native Rust Stream Deck controller
License: GPL-3.0-or-later
URL: https://github.com/pauljones0/Deckard
AutoReqProv: no
Requires: glibc >= 2.35, libstdc++, libgcc, libglvnd-glx, libglvnd-egl, systemd-udev, xdg-utils
%global _build_id_links none
%global __os_install_post %{{nil}}
%description
Deckard controls Stream Deck hardware with a native Rust engine and executable plugins.
%install
mkdir -p %{{buildroot}}
cp -a {staging}/. %{{buildroot}}/
%post
udevadm control --reload-rules >/dev/null 2>&1 || :
udevadm trigger --subsystem-match=usb >/dev/null 2>&1 || :
udevadm trigger --subsystem-match=hidraw >/dev/null 2>&1 || :
udevadm trigger --subsystem-match=misc >/dev/null 2>&1 || :
command -v modprobe >/dev/null && modprobe uinput >/dev/null 2>&1 || :
%files
/opt/deckard
/usr/bin/deckard
/usr/share/applications/deckard.desktop
/usr/share/icons/hicolor/512x512/apps/{APP_ID}.png
/usr/lib/udev/rules.d/60-deckard.rules
""")
    run("rpmbuild", "--define", f"_topdir {top}", "-bb", spec)
    for package in (top / "RPMS").rglob("*.rpm"):
        shutil.copy2(package, output / f"deckard-{VERSION}-{ARCH}.rpm")


def appimage(prefix, output, work):
    appdir = work / "Deckard.AppDir"
    shutil.copytree(prefix, appdir / "usr", symlinks=True)
    shutil.copy2(ROOT / "packaging/linux/deckard.desktop", appdir / "deckard.desktop")
    shutil.copy2(ROOT / f"Assets/icons/hicolor/512x512/apps/{APP_ID}.png", appdir / f"{APP_ID}.png")
    (appdir / "AppRun").write_text('#!/bin/sh\nAPPDIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexec "$APPDIR/usr/bin/deckard" "$@"\n')
    (appdir / "AppRun").chmod(0o755)
    record = json.loads((ROOT / "packaging/linux/appimage-runtime.json").read_text())[f"runtime-{ARCH}"]
    request = urllib.request.Request(record["url"], headers={"User-Agent": "Deckard-build"})
    runtime = urllib.request.urlopen(request, timeout=60).read()
    actual = "sha256:" + hashlib.sha256(runtime).hexdigest()
    if actual != record["digest"]:
        raise ValueError(f"AppImage runtime checksum changed: expected {record['digest']}, got {actual}")
    squash = work / "app.squashfs"
    run("mksquashfs", appdir, squash, "-root-owned", "-noappend", "-comp", "zstd", "-no-progress", "-processors", "2", stdout=subprocess.DEVNULL)
    destination = output / f"deckard-{VERSION}-{ARCH}.AppImage"
    with destination.open("wb") as stream:
        stream.write(runtime)
        with squash.open("rb") as source:
            shutil.copyfileobj(source, stream)
    destination.chmod(0o755)
    # Extract-and-run works on systems without FUSE. Run in a different cwd.
    run(destination, "--appimage-extract-and-run", "--doctor", cwd=work)


def plugin_packages(prefix, output, target=None):
    plugin = prefix / "share/deckard/plugins/example"
    suffix = f"{target}-{ARCH}" if target else ARCH
    destination = output / f"deckard-{VERSION}-example-plugin-{suffix}.zip"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(plugin.iterdir()): archive.write(path, path.name)
    page = json.loads((ROOT / "examples/starter-page.json").read_text())
    bundle = output / f"deckard-{VERSION}-starter-page-{suffix}.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("page.json", json.dumps(page))
        archive.writestr("package.json", json.dumps({"api": 1, "id": "rust-starter", "name": "Rust Starter", "required_plugins": ["example"]}))
        for path in sorted(plugin.iterdir()): archive.write(path, f"plugins/example/{path.name}")
    catalog = []
    for path, kind, name, description in ((destination, "plugin", "Rust example", "Native executable plugin with a label and color action."), (bundle, "page", "Rust Starter", "A ready-to-use page with built-in actions and its native plugin included.")):
        catalog.append({"kind": kind, "name": name, "description": description, "url": f"https://github.com/pauljones0/Deckard/releases/download/v{VERSION}/{path.name}", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "source": "https://github.com/pauljones0/Deckard", "branch": "main", "architectures": [ARCH]})
    (output / f"native-store-{suffix}.json").write_text(json.dumps(catalog, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if ARCH not in ("x86_64", "aarch64"):
        raise ValueError(f"unsupported build architecture: {ARCH}")
    with tempfile.TemporaryDirectory(prefix="deckard-package-") as directory:
        work = Path(directory)
        prefix = work / "deckard"
        prefix.mkdir()
        (ROOT / "docs/images").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "native-ui.png", ROOT / "docs/images/native-editor.png")
        copy_application(prefix)
        copy_libraries(prefix)
        shutil.copy2(prefix / "share/deckard/licenses/ubuntu-sources.json", output / f"SOURCES-{ARCH}.json")
        run(prefix / "bin/deckard", "--doctor", cwd=work)
        run(prefix / "bin/pactl", "--version", cwd=work)
        alias = work / "installed-command"
        alias.symlink_to(prefix / "bin/deckard")
        run(alias, "--doctor", cwd=work)
        # Exercise helpers and the bundled plugin after relocation, without host FFmpeg.
        pixels = subprocess.check_output([str(prefix / "bin/ffmpeg"), "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=16x16", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-threads", "1", "pipe:1"], cwd=work)
        if len(pixels) != 16 * 16 * 4 or pixels[0] < 240 or pixels[1] > 10:
            raise ValueError("Relocated FFmpeg produced an invalid frame")
        video = work / "probe-video.mkv"
        run(prefix / "bin/ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=16x16:r=60", "-frames:v", "3", "-c:v", "ffv1", "-threads", "1", video, cwd=work)
        probe = json.loads(subprocess.check_output([str(prefix / "bin/ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,width,height", "-of", "json", str(video)], cwd=work))
        if probe["streams"][0] != {"width": 16, "height": 16, "avg_frame_rate": "60/1"}:
            raise ValueError("Relocated FFprobe did not preserve source dimensions/rate")
        plugin = prefix / "share/deckard/plugins/example/deckard-plugin-example"
        request = json.dumps({"jsonrpc": "2.0", "id": 73, "method": "event", "params": {"action": "hello", "settings": {"label": "Relocated Rust"}}}) + "\n"
        response = json.loads(subprocess.check_output([str(plugin)], input=request, text=True, cwd=work))
        if response.get("id") != 73 or response.get("result", {}).get("label") != "Relocated Rust":
            raise ValueError("Relocated native plugin failed its RPC check")
        run("dbus-run-session", "--", "xvfb-run", "-a", "-s", "-screen 0 1280x900x24", "env", "LIBGL_ALWAYS_SOFTWARE=1", prefix / "bin/deckard", "--ui-smoke-test", "--skip-load-hardware-decks", "--fake-deck-model", "plus", "--data", work / "relocated-data", cwd=work)
        plugin_packages(prefix, output)
        with tarfile.open(output / f"deckard-{VERSION}-{ARCH}.tar.gz", "w:gz") as archive:
            archive.add(prefix, arcname="deckard")
        deb(prefix, output, work)
        rpm(prefix, output, work)
        appimage(prefix, output, work)
    names = sorted([*output.glob(f"deckard-{VERSION}-*"), output / f"native-store-{ARCH}.json"])
    (output / f"SHA256SUMS-{ARCH}").write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in names))
    (output / f"BUILD-{ARCH}.json").write_text(json.dumps({"version": VERSION, "architecture": ARCH, "baseline": "Ubuntu 22.04, glibc 2.35", "rust": subprocess.check_output(["rustc", "--version"], text=True).strip(), "runtime": "Rust; no Python", "artifacts": [path.name for path in names]}, indent=2) + "\n")


if __name__ == "__main__":
    main()
