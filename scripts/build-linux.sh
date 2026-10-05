#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
arch="$(uname -m)"
image="deckard-linux-build:$arch"
docker build --file packaging/linux/Dockerfile --tag "$image" .
container="$(docker create "$image")"
trap 'docker rm "$container" >/dev/null' EXIT
mkdir -p dist/linux
docker cp "$container:/artifacts/." dist/linux/
docker cp "$container:/work/native-ui.png" "dist/linux/native-ui-$arch.png"
printf '%s\n' "Builds are in $PWD/dist/linux"
