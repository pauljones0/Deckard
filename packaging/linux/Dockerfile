# Each native installer is compiled against its own distribution's libraries.
ARG BUILD_TARGET=ubuntu26
FROM ubuntu:26.04 AS ubuntu26
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git pkg-config libudev-dev cmake nasm \
    libxkbcommon-dev libxkbcommon-x11-0 libwayland-dev libx11-dev libxi-dev \
    libxrandr-dev libxcursor-dev libgl1-mesa-dev libegl1-mesa-dev libvulkan1 mesa-vulkan-drivers \
    libgtk-4-dev libadwaita-1-dev hicolor-icon-theme \
    dpkg-dev python3 ffmpeg xdotool wtype pulseaudio pulseaudio-utils iputils-ping \
    xvfb xauth dbus-x11 xdg-utils

FROM fedora:44 AS fedora44
RUN dnf install -y gcc gcc-c++ ca-certificates curl git pkgconf-pkg-config systemd-devel cmake nasm \
    libxkbcommon-devel libxkbcommon-x11 wayland-devel libX11-devel libXi-devel \
    libXrandr-devel libXcursor-devel libglvnd-devel vulkan-loader mesa-vulkan-drivers mesa-dri-drivers \
    gtk4-devel libadwaita-devel hicolor-icon-theme rpm-build python3 \
    ffmpeg-free xdotool wtype pulseaudio pulseaudio-utils iputils \
    xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon xdg-utils

FROM cachyos/cachyos:latest AS cachyos
# Docker's default seccomp policy prevents pacman's hook network namespace.
# Relax that hook setting inside the build container so caches are generated.
RUN sed -i '/^\[options\]/a DisableSandboxNetwork' /etc/pacman.conf \
    && pacman -Syu --noconfirm && pacman -S --needed --noconfirm \
    base-devel ca-certificates curl git pkgconf systemd cmake nasm \
    libxkbcommon libxkbcommon-x11 wayland libx11 libxi libxrandr libxcursor libglvnd \
    vulkan-icd-loader vulkan-swrast mesa gtk4 libadwaita hicolor-icon-theme \
    python ffmpeg xdotool wtype pulseaudio libpulse iputils \
    xorg-server-xvfb xorg-xauth dbus xdg-utils \
    && useradd --create-home builder

FROM ${BUILD_TARGET} AS build
ARG BUILD_TARGET
ARG CARGO_BUILD_JOBS=2
ARG SOURCE_REVISION=unknown
ENV DECKARD_SOURCE_REVISION=${SOURCE_REVISION}
ENV DECKARD_PACKAGE_TARGET=${BUILD_TARGET}
ENV CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS}
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup.sh \
    && sh /tmp/rustup.sh -y --profile minimal --default-toolchain 1.99.0 \
    && /root/.cargo/bin/rustup component add clippy rustfmt
ENV PATH=/root/.cargo/bin:$PATH
WORKDIR /work
COPY . /work
RUN --mount=type=cache,target=/root/.cargo/registry --mount=type=cache,target=/cargo-target \
    export CARGO_TARGET_DIR=/cargo-target/${BUILD_TARGET}; cargo build --workspace --release --locked \
    && dbus-run-session -- cargo test --workspace --locked \
    && cargo clippy --workspace --all-targets --locked -- -D warnings \
    && cargo fmt --all -- --check \
    && dbus-run-session -- xvfb-run -a -s "-screen 0 1600x1000x24" \
       env GTK_A11Y=none GSK_RENDERER=cairo G_DEBUG=fatal-criticals \
       cargo test -p deckard gtk_editor_workflows --locked -- --ignored --test-threads=1 \
    && mkdir -p target/release \
    && cp "$CARGO_TARGET_DIR/release/deckard" "$CARGO_TARGET_DIR/release/deckard-plugin-example" target/release/
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
RUN --mount=type=cache,target=/root/.cargo/registry \
    python3 packaging/linux/build-native.py --target "$DECKARD_PACKAGE_TARGET" --output /artifacts
CMD ["bash"]
