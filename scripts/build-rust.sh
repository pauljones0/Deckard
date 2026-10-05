#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
cargo build --workspace --release --locked
cargo test --workspace --locked
target/release/deckard --doctor
