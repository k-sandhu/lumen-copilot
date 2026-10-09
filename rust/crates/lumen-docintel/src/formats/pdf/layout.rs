//! Deterministic, heuristic layout over source glyph coordinates.
use super::{
    Page,
    text::{Glyph, PageText},
};
use crate::{
    CoreError,
    canonical::{Block, BlockKind, BoundingBox, Document, Origin, PartKind, SourceRegion},
    runtime::Context,
};
use std::collections::{BTreeMap, BTreeSet};

pub(super) fn union(a: &BoundingBox, b: &BoundingBox) -> BoundingBox {
    BoundingBox {
        x0: a.x0.min(b.x0),
        y0: a.y0.min(b.y0),
        x1: a.x1.max(b.x1),
        y1: a.y1.max(b.y1),
        unit: a.unit,
        origin: a.origin,
    }
}
#[derive(Clone)]
pub(super) struct Line {
    pub text: String,
    pub bbox: BoundingBox,
    pub size: f64,
    pub bold: bool,
}
pub(super) fn lines(text: &PageText, ctx: &Context) -> Result<Vec<Line>, CoreError> {
    let mut glyphs: Vec<&Glyph> = text.glyphs.iter().collect();
    ctx.work(glyphs.len() / 64 + 1)?;
    glyphs.sort_by(|a, b| {
        b.baseline.unwrap_or(b.bbox.y0)
            .total_cmp(&a.baseline.unwrap_or(a.bbox.y0))
            .then(a.bbox.x0.total_cmp(&b.bbox.x0))
    });
    let mut rows: Vec<Vec<&Glyph>> = vec![];
    for glyph in glyphs {
        ctx.work(1)?;
        if let Some(row) = rows.last_mut()
            && (row[0].baseline.unwrap_or(row[0].bbox.y0) - glyph.baseline.unwrap_or(glyph.bbox.y0)).abs() < row[0].size.min(glyph.size) * 0.35
        {
            row.push(glyph);
        } else {
            rows.push(vec![glyph]);
        }
    }
    let mut out = vec![];
    for mut row in rows {
        ctx.work(1)?;
        row.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
        let rtl = row.iter().any(|g| g.text.chars().any(|c| matches!(c as u32, 0x0590..=0x08ff | 0xfb1d..=0xfdff | 0xfe70..=0xfeff)));
        if rtl && row.iter().all(|g| g.source_order.is_some()) {
            row.sort_by_key(|g| g.source_order);
        }
        let mut line: Option<Line> = None;
        for glyph in row {
            ctx.work(1)?;
            let gap = line.as_ref().map_or(0., |l| if rtl {
                (glyph.bbox.x0 - l.bbox.x1).max(l.bbox.x0 - glyph.bbox.x1)
            } else { glyph.bbox.x0 - l.bbox.x1 });
            if gap > glyph.size * 2.5
                && let Some(l) = line.take()
                && !l.text.trim().is_empty()
            {
                out.push(l);
            }
            if let Some(l) = &mut line {
                if gap > glyph.size * 0.22
                    && !l.text.ends_with(char::is_whitespace)
                    && !glyph.text.starts_with(char::is_whitespace)
                {
                    ctx.output(1)?;
                    l.text.push(' ');
                }
                l.text.push_str(&glyph.text);
                l.bbox = union(&l.bbox, &glyph.bbox);
                l.size = l.size.max(glyph.size);
                l.bold |= glyph.bold;
            } else {
                line = Some(Line {
                    text: glyph.text.clone(),
                    bbox: glyph.bbox.clone(),
                    size: glyph.size,
                    bold: glyph.bold,
                });
            }
        }
        if let Some(l) = line
            && !l.text.trim().is_empty()
        {
            out.push(l);
        }
    }
    Ok(out)
}
pub(super) fn region(page: usize, bbox: BoundingBox) -> SourceRegion {
    SourceRegion {
        kind: Some(PartKind::Page),
        number: Some(page),
        bbox: Some(bbox),
        origin: Origin::Heuristic,
        ..SourceRegion::default()
    }
}
pub(super) fn blocks(page: &Page, text: &PageText, ctx: &Context) -> Result<Vec<Block>, CoreError> {
    let mut lines = lines(text, ctx)?;
    if lines.is_empty() {
        return Ok(vec![]);
    }
    let mut sizes: Vec<_> = text
        .glyphs
        .iter()
        .filter(|g| !g.text.trim().is_empty())
        .map(|g| g.size)
        .collect();
    sizes.sort_by(f64::total_cmp);
    let body = sizes[sizes.len() / 2];
    // A title opens a new vertical band; body columns are read within that band.
    let mut titles: Vec<_> = lines
        .iter()
        .filter(|l| l.size > body * 1.3 || l.bold && l.text.chars().count() < 100)
        .map(|l| l.bbox.y0)
        .collect();
    titles.sort_by(|a, b| b.total_cmp(a));
    let mut columns: Vec<f64> = vec![];
    let mut starts: Vec<_> = lines
        .iter()
        .filter(|l| {
            l.size <= body * 1.3 && !(l.bbox.y0 < page.height * 0.12 && l.size < body * 0.9)
        })
        .map(|l| l.bbox.x0)
        .collect();
    starts.sort_by(f64::total_cmp);
    for start in starts {
        ctx.work(1)?;
        if columns.last().is_none_or(|x| (start - x).abs() > body * 2.) {
            columns.push(start);
        }
    }
    let key = |l: &Line| {
        let foot = l.bbox.y0 < page.height * 0.12 && l.size < body * 0.9;
        let heading = l.size > body * 1.3 || l.bold && l.text.chars().count() < 100;
        let band = titles.partition_point(|y| *y > l.bbox.y0 + 1.);
        let col = columns.partition_point(|x| *x < l.bbox.x0 - body * 2.);
        (foot, band, if heading { 0 } else { col + 1 })
    };
    // Sorting runs are bounded by glyph work and the charged per-page allocation.
    lines.sort_by(|a, b| {
        key(a)
            .cmp(&key(b))
            .then(b.bbox.y0.total_cmp(&a.bbox.y0))
            .then(a.bbox.x0.total_cmp(&b.bbox.x0))
    });
    let mut out: Vec<Block> = vec![];
    let mut headings = vec![];
    for line in lines {
        ctx.work(1)?;
        let (foot, _, _) = key(&line);
        let heading = line.size > body * 1.3 || line.bold && line.text.chars().count() < 100;
        let kind = if heading {
            BlockKind::Heading
        } else if foot {
            BlockKind::Footnote
        } else {
            BlockKind::Paragraph
        };
        let heading_level = if heading {
            Some(if line.size > body * 1.8 { 1 } else { 2 })
        } else {
            None
        };
        if let Some(level) = heading_level {
            headings.truncate(level - 1);
            headings.push(line.text.clone());
        }
        let mut merged = false;
        if kind == BlockKind::Paragraph
            && let Some(previous) = out.last_mut()
        {
            let bbox = previous.regions[0].bbox.as_ref().unwrap();
            let gap = bbox.y0 - line.bbox.y1;
            if previous.kind == kind
                && (bbox.x0 - line.bbox.x0).abs() < body
                && (0.0..body * 1.4).contains(&gap)
            {
                ctx.output(1)?;
                previous.text.push('\n');
                previous.text.push_str(line.text.trim());
                previous.regions[0].bbox = Some(union(bbox, &line.bbox));
                merged = true;
            }
        }
        if !merged {
            if !out.is_empty() {
                ctx.output(2)?;
            }
            out.push(Block {
                id: format!("pdf/p{}/b{}", page.number, out.len() + 1),
                kind,
                text: line.text.trim().to_owned(),
                origin: Origin::Heuristic,
                heading_level,
                heading_path: headings.clone(),
                regions: vec![region(page.number, line.bbox)],
                ..Block::default()
            });
        }
    }
    Ok(out)
}
pub(super) fn furniture(
    document: &mut Document,
    pages: &[Page],
    ctx: &Context,
) -> Result<serde_json::Value, CoreError> {
    let mut counts: BTreeMap<(String, bool), BTreeSet<usize>> = BTreeMap::new();
    for block in &document.blocks {
        ctx.work(1)?;
        let r = &block.regions[0];
        let page = r.number.unwrap();
        let b = r.bbox.as_ref().unwrap();
        let height = pages[page - 1].height;
        if b.y0 < height * 0.05 || b.y1 > height * 0.95 {
            counts
                .entry((block.text.clone(), b.y0 < height * 0.05))
                .or_default()
                .insert(page);
        }
    }
    let mut furniture_ids = vec![];
    let mut derived = vec![];
    for block in &mut document.blocks {
        ctx.work(1)?;
        let r = &block.regions[0];
        let page = r.number.unwrap();
        let b = r.bbox.as_ref().unwrap();
        let height = pages[page - 1].height;
        if (b.y0 < height * 0.05 || b.y1 > height * 0.95)
            && counts
                .get(&(block.text.clone(), b.y0 < height * 0.05))
                .is_some_and(|seen| seen.len() >= 2 && seen.len() * 5 >= pages.len() * 3)
        {
            block.kind = BlockKind::Furniture;
            block.heading_level = None;
            furniture_ids.push(block.id.clone());
        }
        if block.text.contains("\u{ad}\n") {
            derived.push(serde_json::json!({"block_id":block.id,"text":block.text.replace("\u{ad}\n",""),"origin":"derived","policy":"soft_hyphen_only"}));
        }
    }
    Ok(
        serde_json::json!({"excluded_block_ids":furniture_ids,"normalized_blocks":derived,"hard_hyphen_policy":"retain_ambiguous_identifiers"}),
    )
}
