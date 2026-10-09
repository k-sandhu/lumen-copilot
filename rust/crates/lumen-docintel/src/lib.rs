//! Pure ingestion computation; no Python, network, storage or trust authority.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CoreError {
    InvalidInput,
    Unsupported,
    UnsupportedLegacyFormat,
    Parse,
    Budget,
    Cancelled,
    Internal,
    Panic,
}

impl std::fmt::Display for CoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::InvalidInput => "invalid native input",
            Self::Unsupported => "unsupported document format",
            Self::UnsupportedLegacyFormat => "unsupported_legacy_format",
            Self::Parse => "native parsing failed",
            Self::Budget => "document budget exceeded",
            Self::Cancelled => "document computation cancelled",
            Self::Internal => "native computation failed",
            Self::Panic => "native computation panicked",
        })
    }
}

impl std::error::Error for CoreError {}

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

pub mod canonical;

pub mod detection;

pub mod runtime;

pub mod chunking;

pub mod formats;
pub mod normalization;
