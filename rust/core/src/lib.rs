//! Native Deckard engine, persistence, rendering and plugin protocol.
pub mod animation;
pub mod archive;
pub mod cache;
pub mod desktop;
pub mod engine;
pub mod geometry;
pub mod ipc;
pub mod media;
pub mod model;
pub mod persistence;
pub mod plugin;
pub mod render;
pub mod store;
pub mod transport;

#[cfg(test)]
mod protocol_tests;
