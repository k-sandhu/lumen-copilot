//! Bounded classification evidence. No model calls or permission authority.
use crate::CoreError;
use crate::canonical::{BlockKind, Document, HeaderRole, PartKind, SourceRegion, render};
use crate::runtime::Context;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use tokenizers::Tokenizer;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeatureSettings {
    pub max_tokens: usize,
    pub max_excerpt_chars: usize,
    pub format: String,
    pub override_path: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rule {
    pub id: String,
    pub path: String,
    pub all_signatures: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuleSet {
    pub taxonomy_version: String,
    pub rules_version: String,
    pub rules: Vec<Rule>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Excerpt {
    pub text: String,
    pub block_id: String,
    pub char_start: usize,
    pub char_end: usize,
    pub regions: Vec<SourceRegion>,
    pub role: String,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Features {
    pub schema_version: usize,
    pub format: String,
    pub extraction_id: Option<String>,
    pub source_sha256: Option<String>,
    pub taxonomy_version: String,
    pub rules_version: String,
    pub part_counts: BTreeMap<String, usize>,
    pub table_density: f64,
    pub heading_profile: BTreeMap<usize, usize>,
    pub form_like: bool,
    pub language: Option<String>,
    pub scanned_ratio: Option<f64>,
    pub signatures: Vec<String>,
    pub excerpts: Vec<Excerpt>,
    pub token_count: usize,
    pub rule_path: Option<String>,
    pub rule_ids: Vec<String>,
    // Local verification only; facade removes full source before stage/model use.
    #[serde(skip_serializing, default)]
    pub rendered_text: String,
}
pub fn classify_features(
    document: Document,
    tokenizer: &Tokenizer,
    settings: &FeatureSettings,
    rules: &RuleSet,
    ctx: &Context,
) -> Result<Features, CoreError> {
    if settings.max_tokens == 0
        || settings.max_excerpt_chars == 0
        || settings.max_excerpt_chars > 32768
        || settings.format.len() > 128
        || rules.rules.len() > 256
    {
        return Err(CoreError::InvalidInput);
    }
    ctx.checkpoint()?;
    let size = document.blocks.iter().try_fold(4096usize, |n, b| {
        n.checked_add(b.text.len().saturating_mul(16) + 2048)
            .ok_or(CoreError::Budget)
    })?;
    let _memory = ctx.reserve(size)?;
    let rendered = render(document)?;
    let doc = &rendered.document;
    let mut part_counts = BTreeMap::new();
    for part in &doc.source_parts {
        ctx.work(1)?;
        *part_counts
            .entry(
                match part.kind {
                    PartKind::Page => "page",
                    PartKind::Slide => "slide",
                    PartKind::Sheet => "sheet",
                }
                .into(),
            )
            .or_default() += 1;
    }
    let mut heading_profile = BTreeMap::new();
    let mut tables = 0;
    let mut form_lines = 0;
    let mut candidates = vec![];
    let mut matches = BTreeSet::new();
    let mut seen_ids = BTreeSet::new();
    for rule in &rules.rules {
        if rule.id.is_empty()
            || !seen_ids.insert(&rule.id)
            || rule.all_signatures.is_empty()
            || rule.all_signatures.len() > 16
            || rule
                .all_signatures
                .iter()
                .any(|s| s.trim().is_empty() || s.len() > 256)
        {
            return Err(CoreError::InvalidInput);
        }
    }
    // Match fixed literal signatures, never executable regex or document instructions.
    let mut signature_hits = BTreeSet::new();
    for (i, (block, span)) in doc.blocks.iter().zip(&rendered.spans).enumerate() {
        ctx.work(1)?;
        if block.kind == BlockKind::Heading {
            *heading_profile
                .entry(block.heading_level.unwrap_or(0))
                .or_default() += 1;
        }
        if block.kind == BlockKind::Table {
            tables += 1;
        }
        for window in block.text.as_bytes().chunks(4096) {
            ctx.work(window.len() / 1024 + 1)?;
        }
        let lower = block.text.to_lowercase();
        for rule in &rules.rules {
            for signature in &rule.all_signatures {
                ctx.work(1)?;
                if lower.contains(&signature.to_lowercase()) {
                    signature_hits.insert(signature.clone());
                }
            }
        }
        form_lines += block
            .text
            .lines()
            .filter(|l| l.contains("___") || l.trim_end().ends_with(':'))
            .count();
        let signature = lower.contains("signature") || lower.contains("signed by");
        let role = if block.kind == BlockKind::Heading {
            "heading"
        } else if signature {
            "signature"
        } else if block
            .table
            .as_ref()
            .is_some_and(|t| t.cells.iter().any(|c| c.header_role != HeaderRole::Unknown))
        {
            "table_headers"
        } else {
            "representative"
        };
        let priority = if i == 0 {
            0
        } else if role == "heading" {
            1
        } else if role == "table_headers" {
            2
        } else if signature {
            3
        } else {
            4
        };
        if priority < 4 || i < 3 || i == doc.blocks.len() / 2 || i + 1 == doc.blocks.len() {
            candidates.push((priority, i, span.char_start, span.char_end, role));
        }
    }
    for rule in &rules.rules {
        if rule
            .all_signatures
            .iter()
            .all(|s| signature_hits.contains(s))
        {
            matches.insert(rule.path.clone());
        }
    }
    let rule_ids = rules
        .rules
        .iter()
        .filter(|r| r.all_signatures.iter().all(|s| signature_hits.contains(s)))
        .map(|r| r.id.clone())
        .collect();
    let rule_path = if settings.override_path.is_none() && matches.len() == 1 {
        matches.into_iter().next()
    } else {
        None
    };
    candidates.sort_by_key(|(p, i, _, _, _)| (*p, *i));
    let mut excerpts = Vec::new();
    let mut evidence = String::new();
    let mut used_chars = 0;
    let mut token_count = 0;
    for (_, i, start, _, role) in candidates {
        ctx.work(1)?;
        let block = &doc.blocks[i];
        let room = settings.max_excerpt_chars.saturating_sub(used_chars);
        if room == 0 {
            break;
        }
        let chars: Vec<char> = block.text.chars().take(room.min(512)).collect();
        let _tokens = ctx.reserve((evidence.len() + chars.len() * 4 + 4096).saturating_mul(256))?;
        let mut length = chars.len();
        let (text, count) = loop {
            ctx.work(1)?;
            let text: String = chars[..length].iter().collect();
            let combined = if evidence.is_empty() {
                text.clone()
            } else {
                format!("{evidence}\n{text}")
            };
            let count = tokenizer
                .encode(combined, true)
                .map_err(|_| CoreError::InvalidInput)?
                .len();
            if count <= settings.max_tokens || length == 0 {
                break (text, count);
            }
            // Conservative shrinking, with an exact final tokenizer check.
            length /= 2;
        };
        if text.is_empty() {
            continue;
        }
        if !evidence.is_empty() {
            evidence.push('\n');
        }
        evidence.push_str(&text);
        token_count = count;
        used_chars += length;
        ctx.output(length)?;
        excerpts.push(Excerpt {
            text,
            block_id: block.id.clone(),
            char_start: start,
            char_end: start + length,
            regions: block.regions.clone(),
            role: role.into(),
        });
    }
    let diagnostics = doc
        .generation
        .diagnostics
        .as_ref()
        .and_then(|v| v.get("native_normalization"));
    let scanned_ratio = doc
        .generation
        .ocr_identity
        .as_ref()
        .and_then(|v| v.get("scanned_ratio"))
        .and_then(|v| v.as_f64())
        .filter(|v| v.is_finite() && (0.0..=1.0).contains(v));
    ctx.checkpoint()?;
    Ok(Features {
        schema_version: 1,
        format: settings.format.clone(),
        extraction_id: doc.generation.extraction_id.clone(),
        source_sha256: doc.generation.source_sha256.clone(),
        taxonomy_version: rules.taxonomy_version.clone(),
        rules_version: rules.rules_version.clone(),
        part_counts,
        table_density: tables as f64 / doc.blocks.len().max(1) as f64,
        heading_profile,
        form_like: form_lines >= 3,
        language: diagnostics
            .and_then(|v| v.get("language"))
            .and_then(|v| v.as_str())
            .map(str::to_owned),
        scanned_ratio,
        signatures: signature_hits.into_iter().collect(),
        excerpts,
        token_count,
        rule_path,
        rule_ids,
        rendered_text: rendered.rendered_text,
    })
}
pub fn features_json(
    document: &str,
    tokenizer: &str,
    settings: &str,
    rules: &str,
    ctx: &Context,
) -> Result<String, CoreError> {
    if tokenizer.len() > 16 * 1024 * 1024 || settings.len() > 4096 || rules.len() > 256 * 1024 {
        return Err(CoreError::Budget);
    }
    ctx.input(document.len())?;
    let _memory = ctx.reserve(
        document.len().saturating_mul(4)
            + tokenizer.len().saturating_mul(32)
            + rules.len().saturating_mul(8),
    )?;
    let validated = crate::canonical::render_json(document)?;
    let rendered: crate::canonical::RenderedDocument =
        serde_json::from_str(&validated).map_err(|_| CoreError::InvalidInput)?;
    let mut tokenizer = Tokenizer::from_bytes(tokenizer).map_err(|_| CoreError::InvalidInput)?;
    tokenizer
        .with_truncation(None)
        .map_err(|_| CoreError::InvalidInput)?;
    tokenizer.with_padding(None);
    let result = classify_features(
        rendered.document,
        &tokenizer,
        &serde_json::from_str(settings).map_err(|_| CoreError::InvalidInput)?,
        &serde_json::from_str(rules).map_err(|_| CoreError::InvalidInput)?,
        ctx,
    )?;
    let _serialized = ctx.reserve(1024 * 1024)?;
    serde_json::to_string(&result).map_err(|_| CoreError::Internal)
}
