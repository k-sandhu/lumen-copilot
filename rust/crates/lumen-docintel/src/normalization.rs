//! Conservative derived text and inspection; original evidence is immutable.
use crate::CoreError;
use crate::canonical::{BlockKind, Document, Origin, PartKind, RenderedDocument, render};
use crate::runtime::Context;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use unicode_normalization::UnicodeNormalization;

#[derive(Debug, Serialize, Deserialize)]
pub struct NormalizedBlock {
    pub block_id: String,
    pub text: String,
    pub origin: Origin,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct PageDiagnostics {
    pub kind: PartKind,
    pub number: usize,
    pub name: String,
    pub character_count: usize,
    pub has_text: bool,
    pub replacement_characters: usize,
    pub suspicious_controls: usize,
    pub mojibake_indicators: usize,
    pub garbled_ratio: f64,
    pub language: Option<String>,
    pub warnings: Vec<String>,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Diagnostics {
    pub schema_version: usize,
    pub policy: String,
    pub outcome: String,
    pub character_count: usize,
    pub replacement_characters: usize,
    pub suspicious_controls: usize,
    pub mojibake_indicators: usize,
    pub garbled_ratio: f64,
    pub language: Option<String>,
    pub pages: Vec<PageDiagnostics>,
    pub excluded_block_ids: Vec<String>,
    pub table_regions: usize,
    pub table_cells: usize,
    pub missing_table_cells: usize,
    pub warnings: Vec<String>,
}
#[derive(Debug, Serialize)]
pub struct NormalizedDocument {
    pub rendered: RenderedDocument,
    pub normalized_blocks: Vec<NormalizedBlock>,
    pub diagnostics: Diagnostics,
}

fn derived_prose(text: &str, ctx: &Context) -> Result<String, CoreError> {
    let chars: Vec<char> = text.chars().collect();
    let mut prepared = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        if i % 1024 == 0 {
            ctx.work(1)?;
        }
        // U+00AD denotes an optional presentation break, unlike a hard hyphen.
        if chars[i] == '\u{ad}' && i > 0 && chars[i - 1].is_ascii_lowercase() {
            let mut next = i + 1;
            if chars.get(next) == Some(&'\r') {
                next += 1;
            }
            if chars.get(next) == Some(&'\n')
                && chars.get(next + 1).is_some_and(char::is_ascii_lowercase)
            {
                i = next + 1;
                continue;
            }
        }
        if chars[i] == '\r' && chars.get(i + 1) == Some(&'\n') {
            i += 1;
        }
        prepared.push(chars[i]);
        i += 1;
    }
    let mut output = String::with_capacity(prepared.len());
    let mut token = String::new();
    let flush = |token: &mut String, output: &mut String| {
        if token.chars().all(|c| c.is_alphabetic() && c.is_lowercase()) {
            let expanded = token
                .replace('ﬀ', "ff")
                .replace('ﬁ', "fi")
                .replace('ﬂ', "fl")
                .replace('ﬃ', "ffi")
                .replace('ﬄ', "ffl")
                .replace('ﬅ', "st")
                .replace('ﬆ', "st");
            output.extend(expanded.nfc());
        } else {
            output.push_str(token);
        }
        token.clear();
    };
    for (n, c) in prepared.chars().enumerate() {
        if n % 1024 == 0 {
            ctx.work(1)?;
        }
        if c.is_whitespace() {
            flush(&mut token, &mut output);
            if c != ' ' || !output.ends_with(' ') {
                output.push(c);
            }
        } else {
            token.push(c);
        }
    }
    flush(&mut token, &mut output);
    Ok(output)
}
fn measures(text: &str) -> (usize, usize, usize, usize, f64, Option<String>, Vec<String>) {
    let total = text.chars().count();
    let replacements = text.matches('�').count();
    let controls = text
        .chars()
        .filter(|c| c.is_control() && !matches!(c, '\r' | '\n' | '\t'))
        .count();
    let mojibake = text.matches("Ã").count()
        + text.matches("Â").count()
        + text.matches("â€").count()
        + text.matches("ðŸ").count();
    let ratio = ((replacements + controls + mojibake) as f64 / (total.max(1) as f64)).min(1.0);
    let sample: String = text.chars().take(32768).collect();
    let language = if ratio > 0.05 {
        None
    } else {
        whatlang::detect(&sample)
            .filter(|i| i.is_reliable())
            .map(|i| i.lang().code().to_string())
    };
    let mut warnings = vec![];
    if !text.trim().is_empty() && ratio > 0.05 {
        warnings.push("garbled_native_text".into());
    }
    if replacements > 0 {
        warnings.push("replacement_characters".into());
    }
    if controls > 0 {
        warnings.push("suspicious_controls".into());
    }
    if mojibake > 0 {
        warnings.push("possible_mojibake".into());
    }
    if text.contains("-\n") || text.contains("-\r\n") {
        warnings.push("ambiguous_hyphenation".into());
    }
    (
        total,
        replacements,
        controls,
        mojibake,
        ratio,
        language,
        warnings,
    )
}
pub fn normalize_document(
    document: Document,
    ctx: &Context,
) -> Result<NormalizedDocument, CoreError> {
    ctx.checkpoint()?;
    let bytes = document.blocks.iter().try_fold(4096usize, |n, b| {
        n.checked_add(b.text.len().saturating_mul(16) + 1024)
            .ok_or(CoreError::Budget)
    })?;
    let _memory = ctx.reserve(bytes)?;
    let mut rendered = render(document)?;
    let mut repeated: BTreeMap<&str, BTreeSet<usize>> = BTreeMap::new();
    for b in &rendered.document.blocks {
        ctx.work(1)?;
        if b.kind == BlockKind::Furniture && b.origin == Origin::Source && !b.text.trim().is_empty()
        {
            for r in &b.regions {
                if r.kind == Some(PartKind::Page)
                    && r.origin == Origin::Source
                    && let Some(page) = r.number
                {
                    repeated.entry(&b.text).or_default().insert(page);
                }
            }
        }
    }
    let excluded: Vec<String> = rendered
        .document
        .blocks
        .iter()
        .filter(|b| {
            b.kind == BlockKind::Furniture
                && b.origin == Origin::Source
                && repeated.get(b.text.as_str()).is_some_and(|p| p.len() >= 2)
                && b.regions.iter().any(|r| {
                    r.kind == Some(PartKind::Page)
                        && r.number.is_some()
                        && r.origin == Origin::Source
                })
        })
        .map(|b| b.id.clone())
        .collect();
    let mut normalized_blocks = vec![];
    let mut reservations = vec![];
    let mut table_regions = 0;
    let mut table_cells = 0;
    let mut missing_table_cells = 0;
    for b in &rendered.document.blocks {
        ctx.work(1)?;
        if let Some(table) = &b.table {
            table_regions += 1;
            for c in &table.cells {
                ctx.work(1)?;
                table_cells += 1;
                if !c.text.is_empty() && !b.text.contains(&c.text) {
                    missing_table_cells += 1;
                }
            }
        }
        reservations.push(ctx.reserve(b.text.len().saturating_mul(8) + 1024)?);
        let text = if matches!(b.kind, BlockKind::Table | BlockKind::Code) {
            b.text.clone()
        } else {
            derived_prose(&b.text, ctx)?
        };
        ctx.output(text.chars().count())?;
        normalized_blocks.push(NormalizedBlock {
            block_id: b.id.clone(),
            text,
            origin: Origin::Derived,
        });
    }
    let chars: Vec<char> = rendered.rendered_text.chars().collect();
    let mut pages = vec![];
    for part in &rendered.document.source_parts {
        ctx.work(1)?;
        let text: String = chars[part.char_start..part.char_end].iter().collect();
        let (total, replacements, controls, mojibake, ratio, language, mut warnings) =
            measures(&text);
        let has_text = !text.trim().is_empty();
        if !has_text {
            warnings.push(
                if part.kind == PartKind::Page {
                    "no_native_text_layer"
                } else {
                    "empty_native_part"
                }
                .into(),
            );
        }
        pages.push(PageDiagnostics {
            kind: part.kind,
            number: part.number,
            name: part.name.clone(),
            character_count: total,
            has_text,
            replacement_characters: replacements,
            suspicious_controls: controls,
            mojibake_indicators: mojibake,
            garbled_ratio: ratio,
            language,
            warnings,
        });
    }
    let (total, replacements, controls, mojibake, ratio, language, mut warnings) =
        measures(&rendered.rendered_text);
    let empty = rendered.rendered_text.trim().is_empty();
    if pages.iter().any(|p| !p.has_text) {
        warnings.push("blank_native_parts".into());
    }
    if missing_table_cells > 0 {
        warnings.push("missing_native_table_text".into());
    }
    if empty {
        warnings.push("no_native_text".into());
    }
    let outcome = if empty {
        "empty"
    } else if ratio > 0.05
        || pages.iter().any(|p| !p.has_text || p.garbled_ratio > 0.05)
        || missing_table_cells > 0
    {
        "partial"
    } else {
        "text_present"
    };
    let diagnostics = Diagnostics {
        schema_version: 1,
        policy: "conservative-derived-1".into(),
        outcome: outcome.into(),
        character_count: total,
        replacement_characters: replacements,
        suspicious_controls: controls,
        mojibake_indicators: mojibake,
        garbled_ratio: ratio,
        language,
        pages,
        excluded_block_ids: excluded,
        table_regions,
        table_cells,
        missing_table_cells,
        warnings,
    };
    let generation = &mut rendered.document.generation;
    let existing = generation
        .diagnostics
        .get_or_insert_with(|| serde_json::json!({}));
    existing
        .as_object_mut()
        .ok_or(CoreError::InvalidInput)?
        .insert(
            "native_normalization".into(),
            serde_json::to_value(&diagnostics).map_err(|_| CoreError::Internal)?,
        );
    let fingerprint = generation
        .fingerprint
        .get_or_insert_with(|| serde_json::json!({}));
    fingerprint.as_object_mut().ok_or(CoreError::InvalidInput)?.insert("native_normalizer".into(),serde_json::json!({"policy":"conservative-derived-1","build_sha256":format!("{:x}",<sha2::Sha256 as sha2::Digest>::digest(include_str!("normalization.rs"))),"unicode_normalization":"0.1.25","whatlang":"0.18.0"}));
    ctx.checkpoint()?;
    Ok(NormalizedDocument {
        rendered,
        normalized_blocks,
        diagnostics,
    })
}
pub fn normalize_json(input: &str, ctx: &Context) -> Result<String, CoreError> {
    let _input_memory = ctx.reserve(input.len().saturating_mul(4))?;
    let validated = crate::canonical::render_json(input)?;
    let rendered: RenderedDocument =
        serde_json::from_str(&validated).map_err(|_| CoreError::InvalidInput)?;
    let result = normalize_document(rendered.document, ctx)?;
    let _serialization = ctx.reserve(
        validated.len().saturating_mul(4) + ctx.stats().output_chars.saturating_mul(24) + 4096,
    )?;
    serde_json::to_string(&result).map_err(|_| CoreError::Internal)
}
