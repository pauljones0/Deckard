#!/usr/bin/env bash
# Verify finished packages on fresh distributions, outside the build image.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
arch="$(uname -m)"
version="$(cat VERSION)"
packages="$PWD/dist/linux"
docker run --rm -i -v "$packages:/packages:ro" ubuntu:22.04 sh -s -- "$version" "$arch" <<'VERIFY'
set -eu
version="$1"
arch="$2"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends "/packages/deckard-$version-$arch.deb" xvfb xauth dbus-x11 libgl1-mesa-dri > /tmp/install.log
/usr/bin/deckard --doctor
/usr/bin/deckard --smoke-test --skip-load-hardware-decks --fake-deck-model studio --fake-deck-model mirabox-293s --fake-deck-model ulanzi-d200 --data /tmp/deckard-models
LIBGL_ALWAYS_SOFTWARE=1 dbus-run-session -- xvfb-run -a /usr/bin/deckard --ui-smoke-test --skip-load-hardware-decks --fake-deck-model plus --data /tmp/deckard-deb
/opt/deckard/bin/ffmpeg -loglevel error -f lavfi -i color=c=red:s=16x16 -frames:v 1 -f rawvideo -pix_fmt rgba -threads 1 /tmp/frame.rgba
[ "$(wc -c < /tmp/frame.rgba)" -eq 1024 ]
"/packages/deckard-$version-$arch.AppImage" --appimage-extract-and-run --doctor
mkdir /tmp/portable
tar -xzf "/packages/deckard-$version-$arch.tar.gz" -C /tmp/portable
/tmp/portable/deckard/bin/deckard --doctor
LIBGL_ALWAYS_SOFTWARE=1 dbus-run-session -- xvfb-run -a /tmp/portable/deckard/bin/deckard --ui-smoke-test --skip-load-hardware-decks --fake-deck-model plus --data /tmp/deckard-portable
printf '%s\n' 'Ubuntu: installed DEB, portable GUI, AppImage and bundled FFmpeg passed.'
VERIFY

docker run --rm -i -v "$packages:/packages:ro" fedora:43 sh -s -- "$version" "$arch" <<'VERIFY'
set -eu
version="$1"
arch="$2"
dnf install -y "/packages/deckard-$version-$arch.rpm" xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon mesa-dri-drivers > /tmp/install.log 2>&1
/usr/bin/deckard --doctor
LIBGL_ALWAYS_SOFTWARE=1 dbus-run-session -- xvfb-run -a /usr/bin/deckard --ui-smoke-test --skip-load-hardware-decks --fake-deck-model plus --data /tmp/deckard-rpm
printf '%s\n' 'Fedora: installed RPM and GUI passed.'
VERIFY
