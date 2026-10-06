#!/usr/bin/env python3
"""Package distro-built Rust binaries with system-managed dependencies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from build import ARCH, APP_ID, ROOT, VERSION, copy_application, plugin_packages, run

TARGETS = {
    "ubuntu26": ("ubuntu", "26.04"),
    "fedora44": ("fedora", "44"),
    "cachyos": ("cachyos", None),
}
PREFIX = Path("usr/lib/deckard")


def license_records(prefix):
    destination = prefix / "share/deckard/licenses"
    destination.mkdir()
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--format-version", "1"], cwd=ROOT
    ))
    records = []
    for package in metadata["packages"]:
        records.append({key: package[key] for key in ("name", "version", "license", "source")})
        source = Path(package["manifest_path"]).parent
        paths = [path for path in source.iterdir() if path.is_file()
                 and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))]
        if package["name"] == "turbojpeg-sys":
            paths.extend((source / "libjpeg-turbo").glob("LICENSE*"))
        for path in paths:
            target = destination / "rust" / f"{package['name']}-{package['version']}" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    (destination / "rust-dependencies.json").write_text(json.dumps(records, indent=2) + "\n")


def stage(staging):
    prefix = staging / PREFIX
    copy_application(prefix, bundled=False)
    license_records(prefix)
    applications = staging / "usr/share/applications"
    applications.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/linux/deckard.desktop", applications)
    icons = staging / "usr/share/icons/hicolor/512x512/apps"
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / f"Assets/icons/hicolor/512x512/apps/{APP_ID}.png", icons)
    rules = staging / "usr/lib/udev/rules.d"
    rules.mkdir(parents=True)
    shutil.copy2(ROOT / "udev.rules", rules / "60-deckard.rules")
    (staging / "usr/bin").mkdir()
    (staging / "usr/bin/deckard").symlink_to("../lib/deckard/bin/deckard-bin")
    forbidden = [p for p in staging.rglob("*") if p.suffix in (".py", ".pyc", ".pyo")
                 or "libpython" in p.name or re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?", p.name)
                 or ".so" in p.name or p.name in ("ffmpeg", "ffprobe", "pactl", "xdotool", "wtype", "ping")]
    if forbidden:
        raise ValueError(f"Bundled runtime or helper leaked into native package: {forbidden}")
    return prefix


def deb(staging, output, work):
    # Generate linked-library requirements from this Ubuntu build, including
    # future GTK linkage. Explicit requirements cover dlopen and subprocesses.
    source = work / "debian"
    source.mkdir(exist_ok=True)
    (source / "control").write_text("Source: deckard\nMaintainer: Deckard contributors\n\nPackage: deckard\nArchitecture: any\nDescription: Native Rust Stream Deck controller\n")
    elf = [staging / PREFIX / "bin/deckard-bin",
           staging / PREFIX / "share/deckard/plugins/example/deckard-plugin-example"]
    generated = subprocess.check_output(
        ["dpkg-shlibdeps", "-O", *[f"-e{p}" for p in elf]], cwd=work, text=True
    ).strip().removeprefix("shlibs:Depends=")
    dependencies = generated + ", libgtk-4-1 (>= 4.16), libadwaita-1-0 (>= 1.6), adwaita-icon-theme, ffmpeg, xdotool, wtype, pulseaudio-utils, iputils-ping, udev, xdg-utils, xdg-desktop-portal, hicolor-icon-theme"
    control = staging / "DEBIAN"
    control.mkdir()
    arch = "amd64" if ARCH == "x86_64" else "arm64"
    (control / "control").write_text(f"Package: deckard\nVersion: {VERSION}\nArchitecture: {arch}\nMaintainer: Deckard contributors\nSection: utils\nPriority: optional\nDepends: {dependencies}\nHomepage: https://github.com/pauljones0/Deckard\nDescription: Native Rust Stream Deck controller\n")
    post = control / "postinst"
    post.write_text("#!/bin/sh\nset -e\nif command -v udevadm >/dev/null; then\n udevadm control --reload-rules || true\n udevadm trigger --subsystem-match=usb || true\n udevadm trigger --subsystem-match=hidraw || true\n udevadm trigger --subsystem-match=misc || true\nfi\nif command -v modprobe >/dev/null; then modprobe uinput || true; fi\n")
    post.chmod(0o755)
    run("dpkg-deb", "--build", "--root-owner-group", staging,
        output / f"deckard-{VERSION}-ubuntu26-{ARCH}.deb")


def rpm(staging, output, work):
    top = work / "rpm"
    for name in ("SPECS", "SOURCES", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
        (top / name).mkdir(parents=True)
    spec = top / "SPECS/deckard.spec"
    spec.write_text(f"""Name: deckard
Version: {VERSION}
Release: 1%{{?dist}}
Summary: Native Rust Stream Deck controller
License: GPL-3.0-or-later
URL: https://github.com/pauljones0/Deckard
Requires: gtk4 >= 4.16, libadwaita >= 1.6, adwaita-icon-theme
Requires: /usr/bin/ffmpeg, /usr/bin/ffprobe, xdotool, wtype, pulseaudio-utils, iputils, systemd-udev, xdg-utils, xdg-desktop-portal, hicolor-icon-theme
%global _build_id_links none
%global debug_package %{{nil}}
%description
Deckard controls Stream Deck hardware using Rust and native executable plugins.
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
/usr/lib/deckard
/usr/bin/deckard
/usr/share/applications/deckard.desktop
/usr/share/icons/hicolor/512x512/apps/{APP_ID}.png
/usr/lib/udev/rules.d/60-deckard.rules
""")
    # Leave automatic ELF dependency generation enabled.
    run("rpmbuild", "--define", f"_topdir {top}", "-bb", spec)
    packages = list((top / "RPMS").rglob("*.rpm"))
    if len(packages) != 1:
        raise ValueError(f"Expected one application RPM: {packages}")
    shutil.copy2(packages[0], output / f"deckard-{VERSION}-fedora44-{ARCH}.rpm")


def pacman(staging, output, work):
    recipe = work / "arch"
    recipe.mkdir()
    (recipe / "PKGBUILD").write_text(f"""pkgname=deckard
pkgver={VERSION}
pkgrel=1
pkgdesc='Native Rust Stream Deck controller'
arch=('x86_64')
url='https://github.com/pauljones0/Deckard'
license=('GPL-3.0-or-later')
depends=('glibc' 'gcc-libs' 'systemd-libs' 'gtk4>=4.16' 'libadwaita>=1.6' 'adwaita-icon-theme' 'ffmpeg' 'libpulse' 'iputils' 'xdg-utils' 'xdg-desktop-portal' 'xdotool' 'wtype' 'hicolor-icon-theme')
optdepends=('kdotool: KDE automatic page switching')
options=('!debug' '!strip')
install=deckard.install
package() {{
  cp -a '{staging}/.' "$pkgdir/"
}}
""")
    shutil.copy2(ROOT / "packaging/aur/deckard-git/deckard-git.install", recipe / "deckard.install")
    run("chmod", "-R", "a+rX", work)
    run("chown", "-R", "builder:builder", recipe)
    run("runuser", "-u", "builder", "--", "env", "PKGEXT=.pkg.tar.zst", "makepkg", "--nodeps", cwd=recipe)
    packages = list(recipe.glob("*.pkg.tar.zst"))
    if len(packages) != 1:
        raise ValueError(f"Expected one pacman package: {packages}")
    shutil.copy2(packages[0], output / f"deckard-{VERSION}-cachyos-{ARCH}.pkg.tar.zst")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    distro = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    expected_id, expected_version = TARGETS[args.target]
    if distro["ID"].strip('"') != expected_id or (expected_version and distro["VERSION_ID"].strip('"') != expected_version):
        raise ValueError(f"Build {args.target} inside its target distribution")
    if ARCH not in ("x86_64", "aarch64") or (args.target == "cachyos" and ARCH != "x86_64"):
        raise ValueError(f"Unsupported {args.target} architecture: {ARCH}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deckard-native-package-") as directory:
        work = Path(directory)
        staging = work / ("debian/deckard" if args.target == "ubuntu26" else "payload")
        prefix = stage(staging)
        plugin_packages(prefix, output, target=args.target)
        if args.target == "ubuntu26":
            # Source builds use the generic catalog; official packages use
            # their own target-specific catalog and native plugin binary.
            shutil.copy2(output / f"native-store-ubuntu26-{ARCH}.json",
                         output / f"native-store-{ARCH}.json")
        {"ubuntu26": deb, "fedora44": rpm, "cachyos": pacman}[args.target](staging, output, work)
    suffix = f"{args.target}-{ARCH}"
    artifacts = sorted([*output.glob(f"deckard-{VERSION}-*{suffix}.*"), output / f"native-store-{suffix}.json"])
    if args.target == "ubuntu26":
        artifacts.append(output / f"native-store-{ARCH}.json")
    (output / f"SHA256SUMS-{suffix}").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in artifacts
    ))
    (output / f"BUILD-{suffix}.json").write_text(json.dumps({
        "version": VERSION, "architecture": ARCH, "target": args.target,
        "distribution": distro["PRETTY_NAME"].strip('"'),
        "rust": subprocess.check_output(["rustc", "--version"], text=True).strip(),
        "runtime": "Rust; no Python", "frontend": "GTK4/libadwaita",
        "source_revision": os.environ.get("DECKARD_SOURCE_REVISION", "unknown"), "bundled_system_libraries": False,
        "system_dependencies": "Installed and updated by the distribution package manager",
        "artifacts": [p.name for p in artifacts],
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
