//! Token-counted structural evidence; context never borrows citation offsets.
use crate::CoreError;
use crate::canonical::{Block, BlockKind, Document, HeaderRole, RenderedDocument, render};
use crate::runtime::Context;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokenizers::Tokenizer;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChunkSettings {
    pub max_tokens: usize,
    pub max_chars: usize,
    pub overlap_chars: usize,
    pub embedding_model: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Chunk {
    pub ord: usize,
    pub text: String,
    pub char_start: usize,
    pub char_end: usize,
    pub block_id: String,
    pub context: String,
    pub context_cell_indices: Vec<usize>,
    pub token_count: usize,
}
#[derive(Debug, Serialize)]
pub struct ChunkedDocument {
    pub rendered: RenderedDocument,
    pub chunks: Vec<Chunk>,
}

fn boundary(chars: &[char], start: usize, end: usize, overlap: usize) -> usize {
    let floor = (start + 1).max(end - (end - start) / 2);
    for sentence in [true, false] {
        for i in (floor..end).rev() {
            if (if sentence {
                matches!(chars[i], '.' | '!' | '?' | '\n')
            } else {
                chars[i].is_whitespace()
            }) && i + 1 > start + overlap
            {
                return i + 1;
            }
        }
    }
    end
}

fn count(
    tokenizer: &Tokenizer,
    context: &str,
    text: &str,
    ctx: &Context,
) -> Result<usize, CoreError> {
    ctx.work(1)?;
    // Tokenizer operations are bounded by max_chars and context size below.
    let _memory = ctx.reserve((context.len() + text.len()).saturating_mul(256) + 4096)?;
    let input = if context.is_empty() {
        text.to_owned()
    } else {
        format!("{context}\n{text}")
    };
    let n = tokenizer
        .encode(input, true)
        .map_err(|_| CoreError::InvalidInput)?
        .len();
    ctx.checkpoint()?;
    Ok(n)
}

fn table_rows(
    block: &Block,
    diagnostics: Option<&serde_json::Value>,
) -> Result<Vec<(usize, usize, usize)>, CoreError> {
    let table = block.table.as_ref().ok_or(CoreError::InvalidInput)?;
    // Bound grid before allocation, including sparse tables with huge dimensions.
    if table
        .rows
        .checked_mul(table.columns)
        .is_none_or(|n| n > 100_000)
    {
        return Err(CoreError::Budget);
    }
    let mut grid = vec![vec![""; table.columns]; table.rows];
    for cell in &table.cells {
        grid[cell.row - 1][cell.column - 1] = &cell.text;
    }
    let expected = grid
        .iter()
        .map(|r| r.join("\t"))
        .collect::<Vec<_>>()
        .join("\n");
    if expected != block.text {
        // Office candidate renderers publish exact cell spans in annotations.
        // Validate their labeled row serialization without searching cell values.
        let entries = diagnostics
            .and_then(|d| d.get("annotations"))
            .and_then(|a| a.as_array())
            .ok_or(CoreError::InvalidInput)?;
        let mut ranges = std::collections::BTreeMap::<usize, Vec<(usize, usize)>>::new();
        for entry in entries {
            if entry.get("kind").and_then(|v| v.as_str()) != Some("cell_span")
                || entry.get("block").and_then(|v| v.as_str()) != Some(block.id.as_str())
            {
                continue;
            }
            let number = |key| {
                entry
                    .get(key)
                    .and_then(|v| v.as_u64())
                    .and_then(|n| usize::try_from(n).ok())
                    .ok_or(CoreError::InvalidInput)
            };
            let (row, start, end) = (number("row")?, number("char_start")?, number("char_end")?);
            if row == 0 || row > table.rows || start > end {
                return Err(CoreError::InvalidInput);
            }
            ranges.entry(row).or_default().push((start, end));
        }
        if ranges.is_empty() {
            return Err(CoreError::InvalidInput);
        }
        let chars: Vec<char> = block.text.chars().collect();
        let mut markers = vec![];
        let mut offset = 0;
        for line in block.text.split_inclusive('\n') {
            if let Some(rest) = line.strip_prefix("Row ") {
                let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
                let row = digits
                    .parse::<usize>()
                    .map_err(|_| CoreError::InvalidInput)?;
                // Markers inside a cell's evidence are text, not row boundaries.
                if !ranges
                    .values()
                    .flatten()
                    .any(|&(s, e)| s <= offset && offset < e)
                {
                    markers.push((row, offset));
                }
            }
            offset += line.chars().count();
        }
        if markers.len() != ranges.len() {
            return Err(CoreError::InvalidInput);
        }
        let mut rows = vec![];
        for (i, &(row, start)) in markers.iter().enumerate() {
            if i > 0 && markers[i - 1].0 >= row {
                return Err(CoreError::InvalidInput);
            }
            let end = markers.get(i + 1).map(|m| m.1).unwrap_or(chars.len());
            let cells = ranges.get(&row).ok_or(CoreError::InvalidInput)?;
            if cells.iter().any(|&(s, e)| s < start || e > end) {
                return Err(CoreError::InvalidInput);
            }
            rows.push((if i == 0 { 0 } else { start }, end, row));
        }
        return Ok(rows);
    }
    let mut offset = 0;
    let mut rows = Vec::with_capacity(table.rows);
    for (i, row) in grid.iter().enumerate() {
        let end = offset + row.iter().map(|s| s.chars().count()).sum::<usize>() + table.columns - 1
            + usize::from(i + 1 < table.rows);
        rows.push((offset, end, i + 1));
        offset = end;
    }
    Ok(rows)
}

fn row_context(block: &Block, row: usize) -> (String, Vec<usize>) {
    let table = block.table.as_ref().expect("validated table");
    let mut parts = block.heading_path.clone();
    if let Some(caption) = &table.caption {
        parts.push(caption.clone());
    }
    let mut indices = vec![];
    for (n, cell) in table.cells.iter().enumerate() {
        let applies = cell.row <= row && row < cell.row + cell.row_span;
        if matches!(cell.header_role, HeaderRole::Column | HeaderRole::Both)
            || applies
                && (cell.header_role == HeaderRole::Row || cell.unit.is_some() || cell.row_span > 1)
        {
            indices.push(n);
            parts.push(cell.text.clone());
            if let Some(unit) = &cell.unit {
                parts.push(unit.clone());
            }
        }
    }
    (parts.join(" | "), indices)
}

pub fn chunk_document(
    mut document: Document,
    tokenizer_json: &str,
    settings: &ChunkSettings,
    ctx: &Context,
) -> Result<ChunkedDocument, CoreError> {
    ctx.checkpoint()?;
    if settings.max_tokens == 0
        || settings.max_tokens > 32768
        || settings.max_chars == 0
        || settings.max_chars > 32768
        || settings.overlap_chars >= settings.max_chars
        || settings.embedding_model.trim().is_empty()
    {
        return Err(CoreError::InvalidInput);
    }
    ctx.input(tokenizer_json.len())?;
    // Loading local inert JSON is bounded; no hub feature is compiled.
    let _tokenizer_memory = ctx.reserve(tokenizer_json.len().saturating_mul(16))?;
    let mut tokenizer =
        Tokenizer::from_bytes(tokenizer_json.as_bytes()).map_err(|_| CoreError::InvalidInput)?;
    tokenizer
        .with_truncation(None)
        .map_err(|_| CoreError::InvalidInput)?;
    tokenizer.with_padding(None);
    let hash = format!("{:x}", Sha256::digest(tokenizer_json.as_bytes()));
    let identity = format!(
        "{}:sha256:{hash}:tokenizers-0.22.2",
        settings.embedding_model
    );
    document.generation.tokenizer_id = Some(identity.clone());
    document.generation.chunker_settings =
        Some(serde_json::to_value(settings).map_err(|_| CoreError::Internal)?);
    let fingerprint = document
        .generation
        .fingerprint
        .get_or_insert_with(|| serde_json::json!({}));
    let map = fingerprint.as_object_mut().ok_or(CoreError::InvalidInput)?;
    map.insert(
        "native_chunker".into(),
        serde_json::json!({"version":"structure-1","tokenizer_id":identity,"settings":settings}),
    );
    // Account canonical clones, spans and output capacity before rendering.
    let _model_memory = ctx.reserve(
        serde_json::to_vec(&document)
            .map_err(|_| CoreError::Internal)?
            .len()
            .saturating_mul(8),
    )?;
    let mut headings: Vec<(usize, String)> = vec![];
    for block in &mut document.blocks {
        ctx.work(1)?;
        if let Some(level) = block.heading_level {
            while headings.last().is_some_and(|(prior, _)| *prior >= level) {
                headings.pop();
            }
            headings.push((level, block.text.clone()));
        }
        if block.heading_path.is_empty() {
            block.heading_path = headings.iter().map(|(_, text)| text.clone()).collect();
        }
    }
    let rendered = render(document)?;
    let mut chunks = vec![];
    let mut reservations = vec![];
    for (block, span) in rendered.document.blocks.iter().zip(&rendered.spans) {
        ctx.work(1)?;
        let _chars_memory = ctx.reserve(block.text.len().saturating_mul(8) + 4096)?;
        let chars: Vec<char> = block.text.chars().collect();
        let atomic = if block.kind == BlockKind::Table {
            Some(table_rows(
                block,
                rendered.document.generation.diagnostics.as_ref(),
            )?)
        } else {
            None
        };
        let mut start = 0;
        let mut row_index = 0;
        while start < chars.len() {
            ctx.work(1)?;
            let (context, indices) = if let Some(rows) = &atomic {
                row_context(block, rows[row_index].2)
            } else {
                (block.heading_path.join(" | "), vec![])
            };
            if context.chars().count() > 32768 {
                return Err(CoreError::Budget);
            }
            let mut end = if let Some(rows) = &atomic {
                rows[row_index].1
            } else {
                (start + settings.max_chars).min(chars.len())
            };
            let mut text: String = chars[start..end].iter().collect();
            let mut tokens = count(&tokenizer, &context, &text, ctx)?;
            // No monotonic-token-count assumption: every candidate is counted.
            while tokens > settings.max_tokens {
                if atomic.is_some() || end <= start + settings.overlap_chars + 1 {
                    return Err(CoreError::Budget);
                }
                end -= 1;
                text = chars[start..end].iter().collect();
                tokens = count(&tokenizer, &context, &text, ctx)?;
            }
            if atomic.is_none() && end < chars.len() {
                let cut = boundary(&chars, start, end, settings.overlap_chars);
                if cut < end {
                    let candidate: String = chars[start..cut].iter().collect();
                    let candidate_tokens = count(&tokenizer, &context, &candidate, ctx)?;
                    if candidate_tokens <= settings.max_tokens {
                        end = cut;
                        text = candidate;
                        tokens = candidate_tokens;
                    }
                }
            }
            if !text.trim().is_empty() {
                ctx.output(text.chars().count() + context.chars().count())?;
                reservations.push(ctx.reserve(
                    (text.len() + context.len()).saturating_mul(2) + indices.len() * 8 + 512,
                )?);
                chunks.push(Chunk {
                    ord: chunks.len(),
                    text,
                    char_start: span.char_start + start,
                    char_end: span.char_start + end,
                    block_id: block.id.clone(),
                    context,
                    context_cell_indices: indices,
                    token_count: tokens,
                });
            }
            if end == chars.len() {
                break;
            }
            if atomic.is_some() {
                start = end;
                row_index += 1;
            } else {
                if end <= start + settings.overlap_chars {
                    return Err(CoreError::Budget);
                }
                start = end - settings.overlap_chars;
            }
        }
    }
    ctx.checkpoint()?;
    Ok(ChunkedDocument { rendered, chunks })
}

pub fn chunk_json(
    document_json: &str,
    tokenizer_json: &str,
    settings_json: &str,
    ctx: &Context,
) -> Result<String, CoreError> {
    if settings_json.len() > 4096 {
        return Err(CoreError::Budget);
    }
    // Reuse bounded shape validation rather than directly deserializing arbitrary model JSON.
    let validated = crate::canonical::render_json(document_json)?;
    let rendered: RenderedDocument =
        serde_json::from_str(&validated).map_err(|_| CoreError::InvalidInput)?;
    let settings = serde_json::from_str(settings_json).map_err(|_| CoreError::InvalidInput)?;
    let result = chunk_document(rendered.document, tokenizer_json, &settings, ctx)?;
    let _serialization = ctx.reserve(
        ctx.stats().output_chars.saturating_mul(24) + validated.len().saturating_mul(3) + 4096,
    )?;
    serde_json::to_string(&result).map_err(|_| CoreError::Internal)
}
