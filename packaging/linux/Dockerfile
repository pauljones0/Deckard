# Oldest supported glibc baseline; produces relocatable, Python-free runtime bundles.
FROM ubuntu:22.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN sed 's/^deb /deb-src /' /etc/apt/sources.list > /etc/apt/sources.list.d/deckard-source.list \
    && apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git pkg-config libudev-dev \
    cmake nasm libxkbcommon-dev libxkbcommon-x11-0 libwayland-dev libx11-dev libxi-dev \
    libxrandr-dev libxcursor-dev libxinerama-dev libgl1-mesa-dev libegl1-mesa-dev \
    patchelf rpm squashfs-tools dpkg-dev zstd python3 ffmpeg xdotool wtype pulseaudio pulseaudio-utils \
    xvfb xauth dbus-x11 mesa-utils x11-utils xdg-utils

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup.sh \
    && sh /tmp/rustup.sh -y --profile minimal --default-toolchain 1.99.0 \
    && /root/.cargo/bin/rustup component add clippy rustfmt
ENV PATH=/root/.cargo/bin:$PATH
WORKDIR /work
COPY . /work
RUN --mount=type=cache,target=/root/.cargo/registry --mount=type=cache,target=/cargo-target \
    export CARGO_TARGET_DIR=/cargo-target; cargo build --workspace --release --locked \
    && dbus-run-session -- cargo test --workspace --locked \
    && cargo clippy --workspace --all-targets --locked -- -D warnings \
    && cargo fmt --all -- --check \
    && mkdir -p target/release \
    && cp /cargo-target/release/deckard /cargo-target/release/deckard-plugin-example target/release/
RUN target/release/deckard --doctor \
    && target/release/deckard --smoke-test --skip-load-hardware-decks --fake-deck-model plus --data /tmp/deckard-smoke
RUN python3 benchmarks/audio_fixture.py --native target/release/deckard
RUN mkdir -p /tmp/deckard-ui-smoke/pages \
    && cp examples/starter-page.json /tmp/deckard-ui-smoke/pages/Main.json \
    && mkdir -p /tmp/deckard-ui-smoke/plugins-native/example \
    && cp examples/plugin/manifest.json target/release/deckard-plugin-example /tmp/deckard-ui-smoke/plugins-native/example/ \
    && dbus-run-session -- xvfb-run -a -s "-screen 0 1280x900x24" \
    env LIBGL_ALWAYS_SOFTWARE=1 DECKARD_UI_CAPTURE=/work/native-ui.png \
    target/release/deckard --ui-smoke-test --skip-load-hardware-decks --fake-deck-model plus --data /tmp/deckard-ui-smoke
FROM build AS packages
RUN --mount=type=cache,target=/root/.cargo/registry python3 packaging/linux/build.py --output /artifacts
CMD ["bash"]
