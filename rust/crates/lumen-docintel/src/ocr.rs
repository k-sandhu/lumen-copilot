//! Selective OCR selection and exact canonical merge. No provider or network I/O.
use crate::{
    CoreError,
    canonical::{self, Block, Document, Origin, PartKind, SourceRegion},
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OcrText {
    pub page: usize,
    pub text: String,
    pub engine_id: String,
    pub confidence: Option<f64>,
}

pub fn selected_pages(document: &Document) -> Result<Vec<usize>, CoreError> {
    let pages = document
        .generation
        .diagnostics
        .as_ref()
        .and_then(|d| d.get("pages"))
        .and_then(|p| p.as_array())
        .ok_or(CoreError::InvalidInput)?;
    let mut selected = Vec::new();
    let mut seen = BTreeSet::new();
    for page in pages {
        let number = page["number"].as_u64().ok_or(CoreError::InvalidInput)? as usize;
        if number == 0 || !seen.insert(number) {
            return Err(CoreError::InvalidInput);
        }
        if page["outcome"] == "needs_ocr" {
            selected.push(number);
        }
    }
    Ok(selected)
}

pub fn merge(mut doc: Document, results: Vec<OcrText>) -> Result<Document, CoreError> {
    let selected = selected_pages(&doc)?;
    let old = canonical::render(doc.clone())?;
    let mut converted = BTreeMap::new();
    for result in results {
        if !selected.contains(&result.page)
            || result.text.trim().is_empty()
            || result.engine_id.trim().is_empty()
            || result
                .confidence
                .is_some_and(|v| !v.is_finite() || !(0.0..=1.0).contains(&v))
            || converted.insert(result.page, result).is_some()
        {
            return Err(CoreError::InvalidInput);
        }
    }
    let mut additions = converted
        .iter()
        .map(|(&page, value)| Block {
            id: format!("ocr-page-{page}"),
            text: value.text.clone(),
            origin: Origin::Ocr,
            regions: vec![SourceRegion {
                kind: Some(PartKind::Page),
                number: Some(page),
                origin: Origin::Ocr,
                ..Default::default()
            }],
            ..Default::default()
        })
        .peekable();
    let mut blocks = Vec::new();
    for block in doc.blocks {
        let page = block
            .regions
            .iter()
            .filter_map(|r| {
                if r.kind == Some(PartKind::Page) {
                    r.number
                } else {
                    None
                }
            })
            .min()
            .unwrap_or(usize::MAX);
        while additions
            .peek()
            .is_some_and(|b| b.regions[0].number.unwrap() <= page)
        {
            blocks.push(additions.next().unwrap());
        }
        if !converted.contains_key(&page) {
            blocks.push(block);
        }
    }
    blocks.extend(additions);
    doc.blocks = blocks;
    // Existing spans no longer address the new immutable render; rebuild before validation.
    let parts = std::mem::take(&mut doc.source_parts);
    let rendered = canonical::render(doc.clone())?;
    let translate = |position: usize| -> Result<usize, CoreError> {
        for span in &old.spans {
            if span.char_start <= position
                && position <= span.char_end
                && let Some(new) = rendered.spans.iter().find(|s| s.block_id == span.block_id)
            {
                return Ok(new.char_start + position - span.char_start);
            }
        }
        if position == 0 {
            Ok(0)
        } else {
            Err(CoreError::InvalidInput)
        }
    };
    for mut part in parts {
        if converted.contains_key(&part.number) {
            let id = format!("ocr-page-{}", part.number);
            let span = rendered
                .spans
                .iter()
                .find(|s| s.block_id == id)
                .ok_or(CoreError::InvalidInput)?;
            part.char_start = span.char_start;
            part.char_end = span.char_end;
        } else if part.char_start != part.char_end {
            part.char_start = translate(part.char_start)?;
            part.char_end = translate(part.char_end)?;
        } else {
            let previous = doc
                .source_parts
                .last()
                .map_or(0, |p: &canonical::SourcePart| p.char_end);
            part.char_start = previous;
            part.char_end = previous;
        }
        doc.source_parts.push(part);
    }
    let mut identities = doc
        .generation
        .ocr_identity
        .take()
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default();
    for result in converted.values() {
        identities.push(json!({"page":result.page,"block_id":format!("ocr-page-{}",result.page),"engine_id":result.engine_id,"confidence":result.confidence,"ocr":true}));
    }
    doc.generation.ocr_identity = Some(json!(identities));
    if let Some(pages) = doc
        .generation
        .diagnostics
        .as_mut()
        .and_then(|d| d.get_mut("pages"))
        .and_then(|v| v.as_array_mut())
    {
        for page in pages.iter_mut() {
            if converted.contains_key(&(page["number"].as_u64().unwrap_or(0) as usize)) {
                page["outcome"] = json!("ocr");
            }
        }
        let remaining = pages.iter().filter(|p| p["outcome"] == "needs_ocr").count();
        doc.generation.outcome = Some(
            if remaining == 0 {
                "indexed"
            } else if doc.blocks.is_empty() {
                "needs_ocr"
            } else {
                "partial"
            }
            .into(),
        );
    }
    canonical::render(doc.clone())?;
    Ok(doc)
}

pub fn merge_json(document: &str, results: &str) -> Result<String, CoreError> {
    if document.len() + results.len() > canonical::MAX_JSON_BYTES {
        return Err(CoreError::Budget);
    }
    let parsed: canonical::RenderedDocument =
        serde_json::from_str(&canonical::render_json(document)?)
            .map_err(|_| CoreError::InvalidInput)?;
    let results = serde_json::from_str(results).map_err(|_| CoreError::InvalidInput)?;
    serde_json::to_string(&canonical::render(merge(parsed.document, results)?)?)
        .map_err(|_| CoreError::Internal)
}
