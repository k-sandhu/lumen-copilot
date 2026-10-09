//! Recognition/refusal only; no legacy content is emitted as evidence.
use crate::{
    CoreError,
    canonical::Document,
    detection::{Format, detect},
    runtime::{Budget, Cancellation, Context, context_json},
};
pub fn extract(bytes: &[u8], limits: Budget) -> Result<Document, CoreError> {
    extract_with_context(bytes, Context::new(limits, Cancellation::default())?)
}
pub fn extract_with_context(bytes: &[u8], ctx: Context) -> Result<Document, CoreError> {
    ctx.checkpoint()?;
    ctx.input(bytes.len())?;
    if bytes.len() > 256 * 1024 {
        return Err(CoreError::Budget);
    }
    let _memory = ctx.reserve(
        bytes
            .len()
            .checked_mul(8)
            .and_then(|n| n.checked_add(8192))
            .ok_or(CoreError::Budget)?,
    )?;
    ctx.work(bytes.len() / 512 + 1)?;
    if !bytes.starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") {
        return Err(CoreError::Parse);
    }
    let detected = detect(bytes, None)?;
    ctx.checkpoint()?;
    match detected.format {
        Format::Doc | Format::Ppt => Err(CoreError::UnsupportedLegacyFormat),
        _ => Err(CoreError::Unsupported),
    }
}
pub fn reject_json(bytes: &[u8], budget_json: &str) -> Result<(), CoreError> {
    extract_with_context(bytes, context_json(budget_json, Cancellation::default())?).map(|_| ())
}
