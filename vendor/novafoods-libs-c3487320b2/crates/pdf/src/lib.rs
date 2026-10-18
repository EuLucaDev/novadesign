//! Shared optional PDF writing for Nova Foods applications.
//!
//! Applications retain layout, shaping, font policy, tagging and export policy.
//! Reading and rendering are separate capabilities; authoring does not enable them.
#![deny(clippy::unwrap_used, clippy::expect_used, clippy::panic, clippy::unimplemented, clippy::todo, clippy::unreachable)]

#[cfg(feature = "write")]
pub mod write;
