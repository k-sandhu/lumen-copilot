//! Pure format candidates; no production dispatch.
pub mod odt;
pub mod package;
pub mod presentations;
pub mod rtf;

pub fn extract_open_document_json(
    bytes: &[u8],
    format_name: &str,
    budget_json: &str,
) -> Result<String, crate::CoreError> {
    use crate::{
        CoreError,
        runtime::{Budget, Cancellation, context_json},
    };
    let ctx = context_json(budget_json, Cancellation::default())?;
    let budget: Budget = serde_json::from_str(budget_json).map_err(|_| CoreError::InvalidInput)?;
    let limits = package::Limits::from(budget);
    let document = match format_name {
        "odt" => odt::extract_with_context(bytes, limits, ctx.clone())?,
        "rtf" => rtf::extract_with_context(bytes, limits, ctx.clone())?,
        _ => return Err(CoreError::Unsupported),
    };
    // Cover retained models, rendering and worst-case escaped JSON simultaneously.
    let bytes = document.blocks.iter().try_fold(4096usize, |total, b| {
        let cell_bytes = b.table.as_ref().map_or(0, |t| {
            t.cells
                .iter()
                .map(|c| c.text.len().saturating_mul(16) + 2048)
                .sum()
        });
        total
            .checked_add(b.text.len().saturating_mul(16) + 4096 + cell_bytes)
            .ok_or(CoreError::Budget)
    })?;
    let _output = ctx.reserve(bytes)?;
    let json = serde_json::to_string(&crate::canonical::render(document)?)
        .map_err(|_| CoreError::Internal)?;
    ctx.checkpoint()?;
    Ok(json)
}
