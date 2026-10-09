//! Budgeted PDF candidate. Supported subsets fail closed; Python stays live.
mod layout;
pub mod pdfium;
mod syntax;
mod tables;
mod text;
use crate::{
    CoreError,
    canonical::{self, Document, Generation, PartKind, SourcePart},
    runtime::{Budget, Cancellation, Context, Reservation, Runtime},
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use syntax::{File, Value};

#[derive(Debug, Clone, Copy, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PageOutcome {
    Extracted,
    NeedsOcr,
}

pub(super) struct Memory {
    pub ctx: Context,
    holds: Vec<Reservation>,
}
impl Memory {
    fn new(ctx: Context) -> Self {
        Self { ctx, holds: vec![] }
    }
    pub fn reserve(&mut self, n: usize) -> Result<(), CoreError> {
        let hold = self
            .ctx
            .reserve(n.checked_add(64).ok_or(CoreError::Budget)?)?;
        self.holds.push(hold);
        Ok(())
    }
}
#[derive(Clone)]
struct Page<'a> {
    number: usize,
    width: f64,
    height: f64,
    rotation: i32,
    resources: &'a Value,
    contents: Option<&'a Value>,
}
fn pages<'a>(file: &'a File, memory: &mut Memory) -> Result<Vec<Page<'a>>, CoreError> {
    struct Inherited<'a> {
        media: Option<&'a Value>,
        resources: Option<&'a Value>,
        rotation: Option<&'a Value>,
    }
    fn visit<'a>(
        file: &'a File,
        v: &'a Value,
        inherit: Inherited<'a>,
        seen: &mut BTreeSet<syntax::Key>,
        out: &mut Vec<Page<'a>>,
        depth: usize,
        m: &mut Memory,
    ) -> Result<(), CoreError> {
        m.ctx.work(1)?;
        if depth > 32 {
            return Err(CoreError::Budget);
        }
        if !seen.insert(v.key()?) {
            return Err(CoreError::Parse);
        }
        m.reserve(256)?;
        let d = file.dictionary(v)?;
        let media = d.get("MediaBox").or(inherit.media);
        let resources = d.get("Resources").or(inherit.resources);
        let rotation = d.get("Rotate").or(inherit.rotation);
        match d.get("Type").and_then(Value::name) {
            Some("Pages") => {
                let kids = file
                    .resolve(d.get("Kids").ok_or(CoreError::Parse)?)?
                    .array()?;
                let before = out.len();
                for kid in kids {
                    visit(
                        file,
                        kid,
                        Inherited {
                            media,
                            resources,
                            rotation,
                        },
                        seen,
                        out,
                        depth + 1,
                        m,
                    )?;
                }
                if d.get("Count").ok_or(CoreError::Parse)?.integer()? != out.len() - before {
                    return Err(CoreError::Parse);
                }
            }
            Some("Page") => {
                let media = file.resolve(media.ok_or(CoreError::Parse)?)?.array()?;
                if media.len() != 4 {
                    return Err(CoreError::Parse);
                }
                let nums = media
                    .iter()
                    .map(Value::number)
                    .collect::<Result<Vec<_>, _>>()?;
                // Nonzero box origins need an explicit translation, never fabricated boxes.
                if nums[0] != 0.0 || nums[1] != 0.0 {
                    return Err(CoreError::Unsupported);
                }
                if nums[2] <= 0.0 || nums[3] <= 0.0 || nums[2] > 100_000.0 || nums[3] > 100_000.0 {
                    return Err(CoreError::Parse);
                }
                if d.get("UserUnit").is_some_and(|v| v.number() != Ok(1.0)) {
                    return Err(CoreError::Unsupported);
                }
                let rotation = rotation.map(Value::number).transpose()?.unwrap_or(0.0);
                if rotation.fract() != 0.0 || rotation % 90.0 != 0.0 {
                    return Err(CoreError::Parse);
                }
                m.reserve(256)?;
                out.push(Page {
                    number: out.len() + 1,
                    width: nums[2],
                    height: nums[3],
                    rotation: (rotation as i32).rem_euclid(360),
                    resources: resources.unwrap_or(v),
                    contents: d.get("Contents"),
                });
            }
            _ => return Err(CoreError::Parse),
        }
        Ok(())
    }
    let root = file.dictionary(file.trailer.dict()?.get("Root").ok_or(CoreError::Parse)?)?;
    if root.get("Type").and_then(Value::name) != Some("Catalog") {
        return Err(CoreError::Parse);
    }
    let mut out = vec![];
    visit(
        file,
        root.get("Pages").ok_or(CoreError::Parse)?,
        Inherited {
            media: None,
            resources: None,
            rotation: None,
        },
        &mut BTreeSet::new(),
        &mut out,
        0,
        memory,
    )?;
    if out.is_empty() {
        return Err(CoreError::Parse);
    }
    Ok(out)
}

pub(super) fn stream(file: &File, v: &Value, m: &mut Memory) -> Result<Vec<u8>, CoreError> {
    let o = file.object(v)?;
    let (start, end) = o.stream.ok_or(CoreError::Parse)?;
    let raw = &file.bytes[start..end];
    let d = o.value.dict()?;
    let filter = if let Some(f) = d.get("Filter") {
        let f = file.resolve(f)?;
        match f {
            Value::Name(n) => Some(n.as_str()),
            Value::Array(a) if a.len() == 1 => Some(a[0].name().ok_or(CoreError::Unsupported)?),
            _ => return Err(CoreError::Unsupported),
        }
    } else {
        None
    };
    if let Some(params) = d.get("DecodeParms")
        && !matches!(file.resolve(params)?, Value::Null)
    {
        return Err(CoreError::Unsupported);
    }
    match filter {
        None => {
            m.ctx.work(raw.len() / 1024 + 1)?;
            m.reserve(raw.len())?;
            Ok(raw.to_vec())
        }
        Some("FlateDecode") => {
            let mut decoder = flate2::Decompress::new(true);
            m.reserve(128 * 1024)?;
            let mut out = vec![];
            let mut buffer = [0u8; 8192];
            loop {
                m.ctx.work(1)?;
                let old_in = decoder.total_in();
                let old_out = decoder.total_out();
                let at = usize::try_from(old_in).map_err(|_| CoreError::Budget)?;
                let status = decoder
                    .decompress(&raw[at..], &mut buffer, flate2::FlushDecompress::None)
                    .map_err(|_| CoreError::Parse)?;
                let n = (decoder.total_out() - old_out) as usize;
                if out
                    .len()
                    .checked_add(n)
                    .is_none_or(|n| n > 32 * 1024 * 1024)
                {
                    return Err(CoreError::Budget);
                }
                if out.len() + n > out.capacity() {
                    let capacity = (out.capacity().max(8192) * 2).max(out.len() + n);
                    m.reserve(capacity)?;
                    out.reserve_exact(capacity - out.len());
                }
                out.extend_from_slice(&buffer[..n]);
                if status == flate2::Status::StreamEnd {
                    if decoder.total_in() != raw.len() as u64 {
                        return Err(CoreError::Parse);
                    }
                    break;
                }
                if decoder.total_in() == old_in && n == 0 {
                    return Err(CoreError::Parse);
                }
            }
            Ok(out)
        }
        _ => Err(CoreError::Unsupported),
    }
}
fn metadata(file: &File, m: &mut Memory) -> Result<serde_json::Value, CoreError> {
    let mut values = BTreeMap::new();
    if let Some(info) = file.trailer.dict()?.get("Info") {
        for (k, v) in file.dictionary(info)? {
            m.ctx.work(1)?;
            if let Value::Bytes(b) = file.resolve(v)? {
                values.insert(k.clone(), text::metadata_text(b)?);
            }
        }
    }
    let root = file.dictionary(file.trailer.dict()?.get("Root").ok_or(CoreError::Parse)?)?;
    let mut outline = vec![];
    let mut seen = BTreeSet::new();
    fn walk(
        file: &File,
        v: &Value,
        depth: usize,
        out: &mut Vec<serde_json::Value>,
        seen: &mut BTreeSet<syntax::Key>,
        m: &mut Memory,
    ) -> Result<(), CoreError> {
        if depth > 32 {
            return Err(CoreError::Budget);
        }
        let mut next = Some(v);
        while let Some(v) = next {
            m.ctx.work(1)?;
            if !seen.insert(v.key()?) {
                return Err(CoreError::Parse);
            }
            let d = file.dictionary(v)?;
            if let Some(Value::Bytes(title)) = d.get("Title") {
                m.reserve(title.len() * 8 + 256)?;
                out.push(json!({"title":text::metadata_text(title)?,"depth":depth,"destination_page_object":d.get("Dest").and_then(|v|v.array().ok()).and_then(|a|a.first()).and_then(|v|v.key().ok()).map(|k|k.0)}));
            }
            if let Some(first) = d.get("First") {
                walk(file, first, depth + 1, out, seen, m)?;
            }
            next = d.get("Next");
        }
        Ok(())
    }
    if let Some(outlines) = root.get("Outlines")
        && let Some(first) = file.dictionary(outlines)?.get("First")
    {
        walk(file, first, 1, &mut outline, &mut seen, m)?;
    }
    Ok(json!({"metadata":values,"outline":outline}))
}

/// Independent engineering profile; no production cutover is implied.
pub fn extract(bytes: &[u8], limits: Budget) -> Result<Document, CoreError> {
    let ctx = Context::new(limits, Cancellation::default())?;
    let runtime = Runtime::new(2, 1)?;
    extract_with_context(bytes, &ctx, &runtime)
}
pub fn extract_with_context(
    bytes: &[u8],
    ctx: &Context,
    runtime: &Runtime,
) -> Result<Document, CoreError> {
    let mut memory = Memory::new(ctx.clone());
    let file = File::open(bytes, &mut memory)?;
    let pages = pages(&file, &mut memory)?;
    let metadata = metadata(&file, &mut memory)?;
    let extracted = runtime.execute(ctx, &pages, |page, ctx| {
        let mut memory = Memory::new(ctx.clone());
        let page_text = text::page(&file, page, &mut memory)?;
        // Retain all reservations with their owned per-page result until assembly.
        ctx.allocate(256, || (page_text, memory))
    })?;
    assemble(
        bytes,
        &pages,
        &extracted.iter().map(|u| &u.value.0).collect::<Vec<_>>(),
        metadata,
        ctx,
        &mut memory,
        false,
    )
}

fn assemble_page(
    page: &Page<'_>,
    page_text: &text::PageText,
    ctx: &Context,
    memory: &mut Memory,
) -> Result<(Vec<canonical::Block>, serde_json::Value), CoreError> {
    ctx.work(1)?;
    memory.reserve(page_text.glyphs.len() * 512 + 1024)?;
    let mut candidates = tables::detect(page, page_text, memory)?;
    candidates.sort_by(|a, b| {
        b.bbox
            .y1
            .total_cmp(&a.bbox.y1)
            .then(a.bbox.x0.total_cmp(&b.bbox.x0))
    });
    let remaining = tables::remaining(page_text, &candidates, ctx)?;
    let mut blocks = layout::blocks(page, &remaining, ctx)?;
    let summaries: Vec<_> = candidates
        .iter()
        .map(|c| json!({"columns":c.edges,"rows":c.block.table.as_ref().unwrap().rows}))
        .collect();
    for (i, mut candidate) in candidates.into_iter().enumerate() {
        candidate.block.id = format!("pdf/p{}/t{}", page.number, i + 1);
        let index = blocks
            .iter()
            .position(|b| {
                let bbox = b.regions[0].bbox.as_ref().unwrap();
                bbox.y1 < candidate.bbox.y1
                    && bbox.x0 >= candidate.bbox.x0 - 12.
                    && bbox.x0 <= candidate.bbox.x1
            })
            .unwrap_or(blocks.len());
        blocks.insert(index, candidate.block);
    }
    let diagnostic = json!({"number":page.number,"width":page.width,"height":page.height,"rotation":page.rotation,"glyph_count":page_text.glyphs.len(),"has_images":page_text.has_images,"ocr_reason":if page_text.needs_ocr {Some("unusable_glyphs")} else if blocks.is_empty() {Some(if page_text.has_images {"image_only"} else {"blank_or_empty"})} else {None},"tables":summaries,"outcome":if page_text.needs_ocr || blocks.is_empty(){PageOutcome::NeedsOcr}else{PageOutcome::Extracted}});
    Ok((blocks, diagnostic))
}

fn retain_page(
    document: &mut Document,
    diagnostics: &mut Vec<serde_json::Value>,
    blocks: Vec<canonical::Block>,
    diagnostic: serde_json::Value,
    memory: &mut Memory,
) -> Result<(), CoreError> {
    // Page scratch covers construction. Retained evidence, table cells, regions,
    // heading strings and vector growth are charged before leaving that scope.
    let size = blocks.iter().try_fold(2048usize, |n, b| {
        n.checked_add(
            b.text.len().saturating_mul(16)
                + 4096
                + b.heading_path
                    .iter()
                    .map(|h| h.len() * 4 + 64)
                    .sum::<usize>()
                + b.table.as_ref().map_or(0, |t| {
                    t.cells
                        .iter()
                        .map(|c| c.text.len() * 16 + 2048)
                        .sum::<usize>()
                }),
        )
        .ok_or(CoreError::Budget)
    })?;
    memory.reserve(size)?;
    diagnostics.push(diagnostic);
    document.blocks.extend(blocks);
    Ok(())
}

fn assemble(
    bytes: &[u8],
    pages: &[Page<'_>],
    extracted: &[&text::PageText],
    metadata: serde_json::Value,
    ctx: &Context,
    memory: &mut Memory,
    pdfium: bool,
) -> Result<Document, CoreError> {
    let mut document = Document::default();
    let mut diagnostics = vec![];
    for (page, page_text) in pages.iter().zip(extracted.iter()) {
        let mut scratch = Memory::new(ctx.clone());
        let (blocks, diagnostic) = assemble_page(page, page_text, ctx, &mut scratch)?;
        retain_page(&mut document, &mut diagnostics, blocks, diagnostic, memory)?;
    }
    finish_document(
        bytes,
        pages,
        document,
        diagnostics,
        metadata,
        ctx,
        memory,
        pdfium,
    )
}

#[allow(clippy::too_many_arguments)]
fn finish_document(
    bytes: &[u8],
    pages: &[Page<'_>],
    mut document: Document,
    diagnostics: Vec<serde_json::Value>,
    metadata: serde_json::Value,
    ctx: &Context,
    memory: &mut Memory,
    pdfium: bool,
) -> Result<Document, CoreError> {
    let normalization_hooks = layout::furniture(&mut document, pages, ctx)?;
    let _joined_segments = tables::join(&mut document, pages, memory)?;
    let table_segments = tables::table_segments(&document, ctx)?;
    let mut outcome = "indexed";
    let missing = diagnostics
        .iter()
        .filter(|d| d["outcome"] == "needs_ocr")
        .count();
    if missing > 0 {
        outcome = if missing == pages.len() {
            "needs_ocr"
        } else {
            "partial"
        };
    }
    memory.reserve(
        bytes.len().min(64 * 1024)
            + document
                .blocks
                .iter()
                .map(|b| b.text.len() * 16 + 2048)
                .sum::<usize>(),
    )?;
    let mut hash = Sha256::new();
    for part in bytes.chunks(8192) {
        ctx.work(1)?;
        hash.update(part);
    }
    document.generation = Generation {
        source_sha256: Some(format!("{:x}", hash.finalize())),
        parser_id: Some(
            if pdfium {
                "rust-pdfium"
            } else {
                "rust-pdf-bounded"
            }
            .into(),
        ),
        parser_version: Some(crate::VERSION.into()),
        build_id: Some(format!(
            "{:x}",
            Sha256::digest(concat!(
                include_str!("mod.rs"),
                include_str!("syntax.rs"),
                include_str!("text.rs"),
                include_str!("layout.rs"),
                include_str!("tables.rs"),
                include_str!("pdfium.rs")
            ))
        )),
        dependency_versions: if pdfium {
            BTreeMap::from([
                ("pdfium-render".into(), "0.9.4".into()),
                ("pdfium".into(), "chromium/7881".into()),
                ("sha2".into(), "0.10.9".into()),
            ])
        } else {
            BTreeMap::from([
                ("flate2".into(), "1.1.10".into()),
                ("sha2".into(), "0.10.9".into()),
            ])
        },
        diagnostics: Some(
            json!({"normalization_hooks":normalization_hooks,"pages":diagnostics,"metadata":metadata["metadata"],"outline":metadata["outline"],"coordinate_policy":"unrotated_source_points_bottom_left","box_policy":if pdfium {"pdfium_loose_char_bounds"} else {"advance_width_heuristic"},"annotation_policy":"exclude_annotations_and_widgets","furniture_policy":"retain_evidence_exclude_in_normalization","runtime":ctx.stats()}),
        ),
        outcome: Some(outcome.into()),
        ..Generation::default()
    };
    let rendered = canonical::render(document.clone())?;
    for page in pages {
        let spans: Vec<_> = rendered
            .spans
            .iter()
            .zip(&document.blocks)
            .filter(|(_, b)| {
                b.regions
                    .iter()
                    .any(|r| r.kind == Some(PartKind::Page) && r.number == Some(page.number))
            })
            .flat_map(|(s, b)| {
                if let Some(segments) = table_segments.get(&b.id) {
                    segments
                        .iter()
                        .filter(|segment| segment.0 == page.number)
                        .map(|segment| (s.char_start + segment.1, s.char_start + segment.2))
                        .collect::<Vec<_>>()
                } else {
                    vec![(s.char_start, s.char_end)]
                }
            })
            .collect();
        let fallback = document
            .source_parts
            .last()
            .map_or(0, |p: &SourcePart| p.char_end);
        document.source_parts.push(SourcePart {
            kind: PartKind::Page,
            name: format!("Page {}", page.number),
            number: page.number,
            char_start: spans.first().map_or(fallback, |s| s.0),
            char_end: spans.last().map_or(fallback, |s| s.1),
        });
    }
    canonical::render(document.clone())?;
    ctx.checkpoint()?;
    Ok(document)
}
pub fn extract_json(bytes: &[u8], ctx: &Context, runtime: &Runtime) -> Result<String, CoreError> {
    let document = extract_with_context(bytes, ctx, runtime)?;
    serialize(document, ctx)
}
fn serialize(mut document: Document, ctx: &Context) -> Result<String, CoreError> {
    let size = document
        .blocks
        .iter()
        .map(|b| {
            b.text.len() * 16
                + 4096
                + b.table.as_ref().map_or(0, |t| {
                    t.cells
                        .iter()
                        .map(|c| c.text.len() * 16 + 2048)
                        .sum::<usize>()
                })
        })
        .sum::<usize>()
        + document.source_parts.len() * 2048;
    // Original bytes are retained/charged by extraction, never serialized here.
    // Covers model/rendering scratch. Measure the actual JSON rather than
    // multiplying every tiny block's pessimistic fixed allowance by four.
    let _model = ctx.reserve(size)?;
    let rendered = canonical::render(document.clone())?;
    struct Counter<'a> {
        bytes: usize,
        ctx: &'a Context,
    }
    impl std::io::Write for Counter<'_> {
        fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
            self.ctx
                .work(buffer.len() / 1024 + 1)
                .map_err(std::io::Error::other)?;
            self.bytes = self
                .bytes
                .checked_add(buffer.len())
                .filter(|n| *n <= canonical::MAX_JSON_BYTES)
                .ok_or_else(|| std::io::Error::other(self.ctx.structural_limit()))?;
            Ok(buffer.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut counter = Counter { bytes: 0, ctx };
    serde_json::to_writer(&mut counter, &rendered).map_err(|_| {
        if ctx.stats().limit.is_some() {
            CoreError::Budget
        } else {
            CoreError::Internal
        }
    })?;
    drop(rendered);
    // Two times measured size bounds serde String growth; an extra KiB covers
    // the updated numeric runtime counters. Reserve before serialization.
    let _json = ctx.reserve(
        counter
            .bytes
            .checked_mul(2)
            .and_then(|n| n.checked_add(1024))
            .ok_or(CoreError::Budget)?,
    )?;
    if let Some(diagnostics) = document.generation.diagnostics.as_mut() {
        diagnostics["runtime"] =
            serde_json::to_value(ctx.stats()).map_err(|_| CoreError::Internal)?;
    }
    let rendered = canonical::render(document)?;
    let result = serde_json::to_string(&rendered).map_err(|_| CoreError::Internal)?;
    ctx.checkpoint()?;
    Ok(result)
}
