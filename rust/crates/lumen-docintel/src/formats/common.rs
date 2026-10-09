//! Shared conservative pre-allocation accounting for text candidates.
use crate::{
    CoreError,
    canonical::{Block, Document, Generation, SourceRegion, render},
    detection::{DecodedText, detect},
    runtime::{Budget, Cancellation, Context, Reservation},
};
use serde_json::Value;
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub budget: Budget,
    pub max_depth: usize,
    pub max_record_bytes: usize,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            budget: Budget::default(),
            max_depth: 64,
            max_record_bytes: 1024 * 1024,
        }
    }
}
impl From<Budget> for Limits {
    fn from(budget: Budget) -> Self {
        Self {
            budget,
            ..Self::default()
        }
    }
}
pub struct Session {
    pub ctx: Context,
    pub limits: Limits,
    reservations: Vec<Reservation>,
}
impl Session {
    pub fn new(bytes: &[u8], limits: Limits) -> Result<Self, CoreError> {
        if limits.max_depth == 0 || limits.max_depth > 128 || limits.max_record_bytes == 0 {
            return Err(CoreError::InvalidInput);
        }
        let mut s = Self {
            ctx: Context::new(limits.budget, Cancellation::default())?,
            limits,
            reservations: vec![],
        };
        s.ctx.input(bytes.len())?;
        s.reserve(bytes.len().checked_mul(8).ok_or(CoreError::Budget)?)?;
        Ok(s)
    }
    pub fn reserve(&mut self, bytes: usize) -> Result<(), CoreError> {
        self.reservations.push(
            self.ctx
                .reserve(bytes.checked_add(128).ok_or(CoreError::Budget)?)?,
        );
        Ok(())
    }
    pub fn push(&mut self, doc: &mut Document, mut block: Block) -> Result<(), CoreError> {
        self.ctx.work(1)?;
        self.ctx
            .output(block.text.chars().count() + if doc.blocks.is_empty() { 0 } else { 2 })?;
        self.reserve(
            block
                .text
                .len()
                .checked_mul(4)
                .and_then(|n| n.checked_add(1024))
                .ok_or(CoreError::Budget)?,
        )?;
        block.id = format!("b{}", doc.blocks.len() + 1);
        doc.blocks.push(block);
        Ok(())
    }
}
pub fn decoded(bytes: &[u8], s: &mut Session) -> Result<DecodedText, CoreError> {
    s.ctx.checkpoint()?;
    // Reserve the bounded decoder/detection workspace before dependency calls.
    let detection = detect(bytes, None)?;
    detection.decoded.ok_or(CoreError::Unsupported)
}
pub fn region(name: impl Into<String>) -> SourceRegion {
    SourceRegion {
        name: Some(name.into()),
        ..SourceRegion::default()
    }
}
pub fn generation(bytes: &[u8], parser: &str, source: &str) -> Generation {
    let mut build = Sha256::new();
    for component in [
        source,
        include_str!("common.rs"),
        include_str!("../canonical.rs"),
        include_str!("../detection.rs"),
        include_str!("../runtime.rs"),
        include_str!("../../../../Cargo.lock"),
    ] {
        build.update((component.len() as u64).to_le_bytes());
        build.update(component.as_bytes());
    }
    Generation {
        source_sha256: Some(
            Sha256::digest(bytes)
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect(),
        ),
        parser_id: Some(parser.into()),
        parser_version: Some("1".into()),
        build_id: Some(
            build
                .finalize()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect(),
        ),
        ..Generation::default()
    }
}
pub fn finish(
    doc: &mut Document,
    s: &mut Session,
    diagnostics: Value,
    partial: bool,
) -> Result<(), CoreError> {
    s.ctx.checkpoint()?;
    doc.generation.diagnostics = Some(diagnostics);
    doc.generation.outcome = Some(
        if partial {
            "partial"
        } else if doc.blocks.iter().all(|b| b.text.trim().is_empty()) {
            "empty"
        } else {
            "success"
        }
        .into(),
    );
    // Keep validation/rendering under the same reservation and deadline.
    let retained = doc.blocks.iter().try_fold(0usize, |n, b| {
        n.checked_add(
            b.text.len().saturating_mul(8)
                + 4096
                + b.table
                    .as_ref()
                    .map_or(0, |t| t.cells.len().saturating_mul(2048)),
        )
        .ok_or(CoreError::Budget)
    })?;
    s.reserve(retained)?;
    render(doc.clone())?;
    s.ctx.checkpoint()
}
