//! Native Deckard engine, persistence, rendering and plugin protocol.
pub use elgato_streamdeck::info::Kind;
pub fn builtin_artwork(path: &str) -> Option<&'static [u8]> {
    artwork::bytes(path)
}
pub mod animation;
pub mod archive;
mod artwork;
pub mod audio;
pub mod builtins;
pub mod cache;
pub mod desktop;
pub mod engine;
pub mod geometry;
pub mod ipc;
pub mod legacy_actions;
pub mod live;
pub mod media;
pub mod model;
pub mod mpris;
pub mod obs;
pub mod persistence;
mod pixels;
pub mod plugin;
mod remote;
pub mod render;
pub mod store;
pub mod system;
pub mod transport;
pub mod uinput;

#[cfg(test)]
mod protocol_tests;
