#!/usr/bin/env bash
# Install native packages on fresh images using only declared dependencies.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
arch="$(uname -m)"
version="$(cat VERSION)"
targets=("${1:-all}")
if [[ "${targets[0]}" == all ]]; then
  targets=(ubuntu26 fedora44)
  [[ "$arch" != x86_64 ]] || targets+=(cachyos)
fi
for target in "${targets[@]}"; do
  case "$target" in
    ubuntu26) image=ubuntu:26.04 ;;
    fedora44) image=fedora:44 ;;
    cachyos) image=cachyos/cachyos:latest ;;
    portable) exec scripts/verify-portable-linux-packages.sh ;;
    *) echo 'Usage: scripts/verify-linux-packages.sh [all|ubuntu26|fedora44|cachyos|portable]' >&2; exit 1 ;;
  esac
  packages="$PWD/dist/linux/$target/$arch"
  docker run --rm -i -v "$packages:/packages:ro" "$image" sh -s -- "$version" "$arch" "$target" <<'VERIFY'
set -eu
version="$1"
arch="$2"
target="$3"
cd /packages
sha256sum -c "SHA256SUMS-$target-$arch"
case "$target" in
  ubuntu26)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends "/packages/deckard-$version-$target-$arch.deb" > /tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; }
    ;;
  fedora44)
    dnf install -y "/packages/deckard-$version-$target-$arch.rpm" > /tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; }
    ;;
  cachyos)
    # Container-only adjustment: allow package hooks to generate their caches.
    sed -i '/^\[options\]/a DisableSandboxNetwork' /etc/pacman.conf
    pacman -Syu --noconfirm > /tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; }
    pacman -U --noconfirm "/packages/deckard-$version-$target-$arch.pkg.tar.zst" >> /tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; }
    ;;
esac
# Check the installed runtime before adding test-only desktop infrastructure.
/usr/bin/deckard --doctor
pactl --version
/usr/bin/deckard --smoke-test --skip-load-hardware-decks --fake-deck-model studio --fake-deck-model mirabox-293s --fake-deck-model ulanzi-d200 --data /tmp/deckard-models
ffmpeg -loglevel error -f lavfi -i color=c=red:s=16x16 -frames:v 1 -f rawvideo -pix_fmt rgba -threads 1 /tmp/frame.rgba
[ "$(wc -c < /tmp/frame.rgba)" -eq 1024 ]
ffmpeg -loglevel error -f lavfi -i color=c=red:s=16x16:r=60 -frames:v 3 -c:v ffv1 -threads 1 /tmp/probe.mkv
ffprobe -v error -show_entries stream=width,height,avg_frame_rate -of default=noprint_wrappers=1 /tmp/probe.mkv > /tmp/probe.txt
grep -qx 'width=16' /tmp/probe.txt
grep -qx 'height=16' /tmp/probe.txt
grep -qx 'avg_frame_rate=60/1' /tmp/probe.txt
printf '%s\n' '{"jsonrpc":"2.0","id":73,"method":"event","params":{"action":"hello","settings":{"label":"Installed Rust"}}}' | /usr/lib/deckard/share/deckard/plugins/example/deckard-plugin-example | grep -q 'Installed Rust'
# Export checks discovery through the installed /usr/bin symlink, rather than
# invoking only the plugin executable directly. ZIP member names are uncompressed.
/usr/bin/deckard --data /tmp/deckard-export --rpc '{"method":"put-page","params":{"page":"Plugin","document":{"keys":{"0x0":{"states":{"0":{"actions":[{"id":"example::hello"}]}}}}}}}'
/usr/bin/deckard --data /tmp/deckard-export --export-page Plugin /tmp/plugin-page.zip
grep -aq 'plugins/example/manifest.json' /tmp/plugin-page.zip
grep -aq 'plugins/example/deckard-plugin-example' /tmp/plugin-page.zip
find /usr/lib/deckard -type f | grep -E '\.so($|\.)|\.py(c|o)?$|/libpython|/(ffmpeg|ffprobe|pactl|xdotool|wtype|ping)$' > /tmp/forbidden-payload || :
if [ -s /tmp/forbidden-payload ]; then cat /tmp/forbidden-payload; exit 1; fi
case "$target" in
  ubuntu26) apt-get install -y --no-install-recommends xvfb xauth dbus-x11 libgl1-mesa-dri mesa-vulkan-drivers > /tmp/ui-install.log 2>&1 ;;
  fedora44) dnf install -y xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon mesa-dri-drivers mesa-vulkan-drivers > /tmp/ui-install.log 2>&1 ;;
  cachyos) pacman -S --needed --noconfirm xorg-server-xvfb xorg-xauth dbus mesa vulkan-swrast > /tmp/ui-install.log 2>&1 ;;
esac
LIBGL_ALWAYS_SOFTWARE=1 dbus-run-session -- xvfb-run -a /usr/bin/deckard --ui-smoke-test --skip-load-hardware-decks --fake-deck-model plus --data /tmp/deckard-ui
LIBGL_ALWAYS_SOFTWARE=1 VK_ICD_FILENAMES=/tmp/deckard-no-vulkan-driver.json dbus-run-session -- xvfb-run -a /usr/bin/deckard --ui-smoke-test --skip-load-hardware-decks --fake-deck-model plus --data /tmp/deckard-gl-fallback > /tmp/deckard-gl-fallback.log 2>&1
cat /tmp/deckard-gl-fallback.log
grep -q 'Deckard graphics: OpenGL' /tmp/deckard-gl-fallback.log
printf '%s\n' "$target: installed package, native plugin, system helpers and GUI passed; no bundled system libraries."
VERIFY
done
