#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
arch="$(uname -m)"
targets=("${1:-all}")
if [[ "${targets[0]}" == all ]]; then
  targets=(ubuntu26 fedora44)
  [[ "$arch" != x86_64 ]] || targets+=(cachyos)
fi
container=""
trap '[[ -z "$container" ]] || docker rm "$container" >/dev/null' EXIT
for target in "${targets[@]}"; do
  dockerfile=packaging/linux/Dockerfile
  case "$target" in
    ubuntu26|fedora44) ;;
    cachyos) [[ "$arch" == x86_64 ]] || { echo 'CachyOS builds require x86_64.' >&2; exit 1; } ;;
    portable) echo "GTK builds use CachyOS, Fedora 44 or Ubuntu 26.04 packages; portable builds ended at v0.8.0." >&2; exit 1 ;;
    *) echo 'Usage: scripts/build-linux.sh [all|ubuntu26|fedora44|cachyos]' >&2; exit 1 ;;
  esac
  image="deckard-linux-$target:$arch"
  docker build --file "$dockerfile" --build-arg "BUILD_TARGET=$target" --build-arg "SOURCE_REVISION=$(git rev-parse HEAD)" --tag "$image" .
  container="$(docker create "$image")"
  destination="dist/linux/$target/$arch"
  mkdir -p "$destination"
  docker cp "$container:/artifacts/." "$destination/"
  docker cp "$container:/work/native-ui.png" "$destination/native-ui-$target-$arch.png"
  docker rm "$container" >/dev/null
  container=""
done
printf '%s\n' "Builds are in $PWD/dist/linux"
