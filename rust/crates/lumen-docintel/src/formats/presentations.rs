//! Source-only slide content, notes, charts and diagram text.
use super::package::{Content, Limits, Node, Package, Relationship, Session, finish, generation};
use crate::{
    CoreError,
    canonical::{
        Block, BlockKind, BoundingBox, Cell, CellRange, CoordinateOrigin, CoordinateUnit, Document,
        HeaderRole, Origin, PartKind, SourcePart, SourceRegion, Table,
    },
    runtime::Context,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;

fn num(n: &Node, key: &str, default: usize) -> Result<usize, CoreError> {
    n.attr(key)
        .map(|v| v.parse().map_err(|_| CoreError::Parse))
        .unwrap_or(Ok(default))
}
fn region(
    slide: usize,
    path: &str,
    bbox: Option<BoundingBox>,
    range: Option<CellRange>,
) -> SourceRegion {
    SourceRegion {
        kind: Some(PartKind::Slide),
        number: Some(slide),
        name: Some(path.to_owned()),
        bbox,
        cell_range: range,
        ..SourceRegion::default()
    }
}
fn inline(n: &Node, out: &mut String, s: &mut Session) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    if n.name == "t" {
        s.emit(out, &n.text)?;
        return Ok(());
    }
    if n.name == "br" {
        s.emit(out, "\u{000b}")?;
        return Ok(());
    }
    if n.name == "pPr" || n.name == "rPr" || n.name == "endParaRPr" {
        return Ok(());
    }
    for child in &n.children {
        inline(child, out, s)?;
    }
    Ok(())
}
fn frame(n: &Node, s: &mut Session) -> Result<String, CoreError> {
    let mut text = String::new();
    for (i, p) in n.children_named("p").enumerate() {
        if i > 0 {
            s.emit(&mut text, "\n")?;
        }
        inline(p, &mut text, s)?;
    }
    Ok(text)
}
fn odf_inline(n: &Node, out: &mut String, s: &mut Session) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    match n.name.as_str() {
        "s" => {
            let count = num(n, "c", 1)?;
            s.ctx.work(count)?;
            for _ in 0..count {
                s.emit(out, " ")?;
            }
            return Ok(());
        }
        "tab" => {
            s.emit(out, "\t")?;
            return Ok(());
        }
        "line-break" => {
            s.emit(out, "\n")?;
            return Ok(());
        }
        _ => {}
    }
    for content in &n.content {
        match content {
            Content::Text(text) => s.emit(out, text)?,
            Content::Child(i) => odf_inline(&n.children[*i], out, s)?,
        }
    }
    Ok(())
}
fn odf_frame(n: &Node, s: &mut Session) -> Result<String, CoreError> {
    let mut paragraphs = Vec::new();
    n.descendants("p", &mut paragraphs);
    let mut text = String::new();
    for (i, p) in paragraphs.iter().enumerate() {
        if i > 0 {
            s.emit(&mut text, "\n")?;
        }
        odf_inline(p, &mut text, s)?;
    }
    Ok(text)
}
#[derive(Clone, Copy)]
struct Transform {
    sx: f64,
    sy: f64,
    tx: f64,
    ty: f64,
}
impl Default for Transform {
    fn default() -> Self {
        Self {
            sx: 1.0,
            sy: 1.0,
            tx: 0.0,
            ty: 0.0,
        }
    }
}
fn value(n: &Node, key: &str) -> Option<f64> {
    n.attr(key)?.parse::<f64>().ok().filter(|v| v.is_finite())
}
fn xfrm(n: &Node) -> Option<&Node> {
    if n.name == "grpSp" {
        n.child("grpSpPr")?.child("xfrm")
    } else {
        n.find("xfrm")
    }
}
fn bbox(n: &Node, parent: Option<Transform>) -> Option<BoundingBox> {
    let parent = parent?;
    let transform = xfrm(n)?;
    let off = transform.child("off")?;
    let ext = transform.child("ext")?;
    let x = parent.tx + value(off, "x")? * parent.sx;
    let y = parent.ty + value(off, "y")? * parent.sy;
    let w = value(ext, "cx")? * parent.sx;
    let h = value(ext, "cy")? * parent.sy;
    if [x, y, w, h].iter().any(|v| !v.is_finite() || *v < 0.0) {
        return None;
    }
    Some(BoundingBox {
        x0: x / 12700.0,
        y0: y / 12700.0,
        x1: (x + w) / 12700.0,
        y1: (y + h) / 12700.0,
        unit: CoordinateUnit::Point,
        origin: CoordinateOrigin::TopLeft,
    })
}
fn group_transform(n: &Node, parent: Option<Transform>) -> Option<Transform> {
    let parent = parent?;
    let transform = xfrm(n)?;
    let off = transform.child("off")?;
    let ext = transform.child("ext")?;
    let child_off = transform.child("chOff")?;
    let child_ext = transform.child("chExt")?;
    let cw = value(child_ext, "cx")?;
    let ch = value(child_ext, "cy")?;
    if cw == 0.0 || ch == 0.0 {
        return None;
    }
    let sx = parent.sx * value(ext, "cx")? / cw;
    let sy = parent.sy * value(ext, "cy")? / ch;
    Some(Transform {
        sx,
        sy,
        tx: parent.tx + parent.sx * value(off, "x")? - sx * value(child_off, "x")?,
        ty: parent.ty + parent.sy * value(off, "y")? - sy * value(child_off, "y")?,
    })
}
fn title(n: &Node) -> bool {
    n.find("ph")
        .is_some_and(|p| matches!(p.attr("type"), Some("title" | "ctrTitle")))
}
fn table(
    n: &Node,
    id: &str,
    slide: usize,
    odf: bool,
    s: &mut Session,
    annotations: &mut Vec<Value>,
) -> Result<Block, CoreError> {
    let rows: Vec<&Node> = if odf {
        let mut rows = Vec::new();
        n.descendants("table-row", &mut rows);
        rows
    } else {
        n.children_named("tr").collect()
    };
    let columns = rows
        .iter()
        .map(|r| {
            r.children
                .iter()
                .filter(|c| {
                    if odf {
                        matches!(c.name.as_str(), "table-cell" | "covered-table-cell")
                    } else {
                        c.name == "tc"
                    }
                })
                .count()
        })
        .max()
        .ok_or(CoreError::Parse)?;
    if columns == 0 {
        return Err(CoreError::Parse);
    }
    s.ctx
        .work(rows.len().checked_mul(columns).ok_or(CoreError::Budget)?)?;
    let mut cells = Vec::new();
    let mut headers = Vec::new();
    let mut text = String::new();
    s.emit(&mut text, "[Table]")?;
    let mut chars = 0;
    let mut byte = 0;
    let mut spans: Vec<CellRange> = Vec::new();
    for (r, row) in rows.iter().enumerate() {
        s.emit(&mut text, &format!("\nRow {}: ", r + 1))?;
        let source: Vec<&Node> = row
            .children
            .iter()
            .filter(|c| {
                if odf {
                    matches!(c.name.as_str(), "table-cell" | "covered-table-cell")
                } else {
                    c.name == "tc"
                }
            })
            .collect();
        for c in 0..columns {
            s.ctx.work(spans.len() + 1)?;
            let node = source.get(c).copied();
            let covered = spans.iter().any(|m| {
                r + 1 >= m.row_start
                    && r < m.row_end
                    && c + 1 >= m.column_start
                    && c < m.column_end
                    && (r + 1, c + 1) != (m.row_start, m.column_start)
            }) || node.is_some_and(|n| {
                n.name == "covered-table-cell"
                    || n.attr("hMerge") == Some("1")
                    || n.attr("vMerge") == Some("1")
            });
            let value = if covered {
                String::new()
            } else if let Some(n) = node {
                if odf {
                    odf_frame(n, s)?
                } else {
                    n.child("txBody")
                        .map(|n| frame(n, s))
                        .transpose()?
                        .unwrap_or_default()
                }
            } else {
                String::new()
            };
            if r == 0 {
                headers.push(value.replace('\n', " / "));
            }
            let label = if r > 0 && !headers[c].is_empty() {
                format!(" [{}]", headers[c])
            } else {
                String::new()
            };
            if c > 0 {
                s.emit(&mut text, " | ")?;
            }
            s.emit(&mut text, &format!("C{}{label}=", c + 1))?;
            chars += text[byte..].chars().count();
            byte = text.len();
            let start = chars;
            s.emit(&mut text, &value)?;
            chars += text[byte..].chars().count();
            byte = text.len();
            annotations.push(json!({"kind":"cell_span","block":id,"slide":slide,"row":r+1,"column":c+1,"char_start":start,"char_end":chars,"covered":covered}));
            if !covered {
                let row_span = node
                    .map(|n| {
                        num(
                            n,
                            if odf {
                                "number-rows-spanned"
                            } else {
                                "rowSpan"
                            },
                            1,
                        )
                    })
                    .transpose()?
                    .unwrap_or(1);
                let column_span = node
                    .map(|n| {
                        num(
                            n,
                            if odf {
                                "number-columns-spanned"
                            } else {
                                "gridSpan"
                            },
                            1,
                        )
                    })
                    .transpose()?
                    .unwrap_or(1);
                if row_span == 0
                    || column_span == 0
                    || row_span > rows.len() - r
                    || column_span > columns - c
                {
                    return Err(CoreError::Parse);
                }
                let range = CellRange {
                    row_start: r + 1,
                    row_end: r + row_span,
                    column_start: c + 1,
                    column_end: c + column_span,
                };
                if row_span > 1 || column_span > 1 {
                    spans.push(range.clone());
                }
                s.reserve(1024 + value.len() * 4)?;
                cells.push(Cell {
                    row: r + 1,
                    column: c + 1,
                    row_span,
                    column_span,
                    text: value,
                    header_role: if r == 0 {
                        HeaderRole::Column
                    } else {
                        HeaderRole::Unknown
                    },
                    role_origin: if r == 0 {
                        Origin::Heuristic
                    } else {
                        Origin::Source
                    },
                    regions: vec![region(
                        slide,
                        &format!("{id}/R{}C{}", r + 1, c + 1),
                        None,
                        Some(range),
                    )],
                    ..Cell::default()
                });
            }
        }
    }
    s.emit(&mut text, "\n[/Table]")?;
    Ok(Block {
        id: id.to_owned(),
        kind: BlockKind::Table,
        text,
        table: Some(Table {
            rows: rows.len(),
            columns,
            cells,
            caption: None,
        }),
        regions: vec![region(slide, id, None, None)],
        ..Block::default()
    })
}
fn chart(
    n: &Node,
    id: &str,
    slide: usize,
    s: &mut Session,
    annotations: &mut Vec<Value>,
) -> Result<Vec<Block>, CoreError> {
    let mut series = Vec::new();
    n.descendants("ser", &mut series);
    let mut blocks = Vec::new();
    for (i, series) in series.iter().enumerate() {
        s.ctx.work(1)?;
        let name = series
            .child("tx")
            .and_then(|n| n.find("v"))
            .map(|n| n.text.as_str())
            .unwrap_or("");
        let mut categories = BTreeMap::new();
        if let Some(cat) = series.child("cat").or_else(|| series.child("xVal")) {
            let mut points = Vec::new();
            cat.descendants("pt", &mut points);
            for point in points {
                categories.insert(
                    num(point, "idx", 0)?,
                    point.child("v").map(|n| n.text.clone()).unwrap_or_default(),
                );
            }
        }
        let mut points = Vec::new();
        if let Some(values) = series.child("val").or_else(|| series.child("yVal")) {
            values.descendants("pt", &mut points);
        }
        points.sort_by_key(|n| num(n, "idx", 0).unwrap_or(0));
        let mut text = String::new();
        s.emit(&mut text, "Chart series")?;
        if !name.is_empty() {
            s.emit(&mut text, ": ")?;
            s.emit(&mut text, name)?;
        }
        for point in points {
            s.ctx.work(1)?;
            let index = num(point, "idx", 0)?;
            let value = point.child("v").ok_or(CoreError::Parse)?;
            s.emit(&mut text, "\n")?;
            s.emit(
                &mut text,
                categories
                    .get(&index)
                    .map(String::as_str)
                    .unwrap_or("Value"),
            )?;
            s.emit(&mut text, "=")?;
            s.emit(&mut text, &value.text)?;
            annotations.push(json!({"kind":"chart_cache","block":id,"series":i+1,"point":index,"category":categories.get(&index),"value":value.text,"freshness":"unknown"}));
        }
        blocks.push(Block {
            id: format!("{id}/series[{}]", i + 1),
            kind: BlockKind::Paragraph,
            text,
            regions: vec![region(slide, id, None, None)],
            ..Block::default()
        });
    }
    Ok(blocks)
}
#[allow(clippy::too_many_arguments)]
fn shapes(
    n: &Node,
    part: &str,
    slide: usize,
    parent: Option<Transform>,
    package: &mut Package<'_>,
    rels: &BTreeMap<String, Relationship>,
    s: &mut Session,
    annotations: &mut Vec<Value>,
) -> Result<Vec<Block>, CoreError> {
    let mut blocks = Vec::new();
    let mut ordered: Vec<(usize, &Node)> = n
        .children
        .iter()
        .enumerate()
        .filter(|(_, n)| {
            matches!(
                n.name.as_str(),
                "sp" | "grpSp" | "pic" | "graphicFrame" | "cxnSp"
            )
        })
        .collect();
    // Position ordering applies only when both shapes supply usable geometry.
    ordered.sort_by(|(i, a), (j, b)| {
        let primary = (!title(a)).cmp(&(!title(b)));
        if !primary.is_eq() {
            return primary;
        }
        match (bbox(a, parent), bbox(b, parent)) {
            (Some(a), Some(b)) => {
                a.y0.total_cmp(&b.y0)
                    .then(a.x0.total_cmp(&b.x0))
                    .then(i.cmp(j))
            }
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            _ => i.cmp(j),
        }
    });
    for (index, n) in ordered {
        s.ctx.work(1)?;
        let shape = n
            .find("cNvPr")
            .and_then(|n| n.attr("id"))
            .map(str::to_owned)
            .unwrap_or_else(|| format!("source-{index}"));
        let id = format!("{part}/shape[{shape}]");
        if n.name == "grpSp" {
            annotations.push(json!({"kind":"group","shape":id,"slide":slide,"source_order":index}));
            blocks.extend(shapes(
                n,
                &id,
                slide,
                group_transform(n, parent),
                package,
                rels,
                s,
                annotations,
            )?);
            continue;
        }
        if let Some(grid) = n.find("tbl") {
            let mut block = table(grid, &id, slide, false, s, annotations)?;
            block.regions[0].bbox = bbox(n, parent);
            blocks.push(block);
            continue;
        }
        if let Some(c) = n.find("chart") {
            let rel = rels
                .get(c.attr("id").ok_or(CoreError::Parse)?)
                .ok_or(CoreError::Parse)?;
            if rel.external {
                annotations.push(json!({"kind":"external_chart_unavailable","shape":id}));
                continue;
            }
            let data = package.xml(&rel.target, s)?;
            blocks.extend(chart(&data, &id, slide, s, annotations)?);
            continue;
        }
        if let Some(diagram) = n.find("relIds") {
            let rel = diagram
                .attr("dm")
                .and_then(|id| rels.get(id))
                .ok_or(CoreError::Parse)?;
            if rel.external {
                return Err(CoreError::Unsupported);
            }
            let data = package.xml(&rel.target, s)?;
            let mut points = Vec::new();
            data.descendants("pt", &mut points);
            for (i, point) in points.iter().enumerate() {
                if let Some(body) = point.child("t") {
                    let text = frame(body, s)?;
                    if !text.trim().is_empty() {
                        blocks.push(Block {
                            id: format!("{id}/diagram[{}]", i + 1),
                            text,
                            regions: vec![region(slide, &rel.target, None, None)],
                            ..Block::default()
                        });
                    }
                }
            }
            continue;
        }
        if n.name == "pic" {
            let alt = n
                .find("cNvPr")
                .and_then(|n| n.attr("descr").or(n.attr("title")))
                .unwrap_or("");
            let mut text = String::new();
            s.emit(&mut text, "[Image")?;
            if !alt.is_empty() {
                s.emit(&mut text, ": ")?;
                s.emit(&mut text, alt)?;
            }
            s.emit(&mut text, "]")?;
            annotations.push(
                json!({"kind":"image_placeholder","shape":id,"alt_text":alt,"ocr":"unavailable"}),
            );
            blocks.push(Block {
                id: id.clone(),
                kind: BlockKind::Figure,
                text,
                origin: Origin::Derived,
                regions: vec![region(slide, &id, bbox(n, parent), None)],
                ..Block::default()
            });
            continue;
        }
        if let Some(body) = n.child("txBody") {
            let text = frame(body, s)?;
            if !text.trim().is_empty() {
                blocks.push(Block {
                    id: id.clone(),
                    text,
                    regions: vec![region(slide, &id, bbox(n, parent), None)],
                    ..Block::default()
                });
            }
        }
    }
    Ok(blocks)
}
fn add_slide(
    doc: &mut Document,
    number: usize,
    name: String,
    mut content: Vec<Block>,
    s: &mut Session,
) -> Result<(), CoreError> {
    let offset = doc.source_parts.last().map_or(0, |p| p.char_end);
    if content.iter().all(|b| {
        b.kind == BlockKind::Table
            && b.table
                .as_ref()
                .is_some_and(|t| t.cells.iter().all(|c| c.text.trim().is_empty()))
            || b.text.trim().is_empty()
    }) {
        doc.source_parts.push(SourcePart {
            kind: PartKind::Slide,
            name,
            number,
            char_start: offset,
            char_end: offset,
        });
        return Ok(());
    }
    let heading_id = format!("slide/{number}/heading");
    let mut heading = String::new();
    s.emit(&mut heading, &name)?;
    let start = offset + if doc.blocks.is_empty() { 0 } else { 2 };
    s.ctx.output(if doc.blocks.is_empty() { 0 } else { 2 })?;
    doc.blocks.push(Block {
        id: heading_id.clone(),
        kind: BlockKind::Heading,
        text: heading,
        heading_level: Some(1),
        regions: vec![region(number, &format!("slide/{number}"), None, None)],
        ..Block::default()
    });
    let mut end = start + name.chars().count();
    for block in &mut content {
        s.reserve(1024 + block.text.len() * 4)?;
        block.parent_id = Some(heading_id.clone());
        s.ctx.output(2)?;
        end += 2 + block.text.chars().count();
    }
    doc.blocks.extend(content);
    doc.source_parts.push(SourcePart {
        kind: PartKind::Slide,
        name,
        number,
        char_start: start,
        char_end: end,
    });
    Ok(())
}
fn pptx(bytes: &[u8], mut package: Package<'_>, s: &mut Session) -> Result<Document, CoreError> {
    let presentation = package.xml("ppt/presentation.xml", s)?;
    let rels = package.relationships("ppt/presentation.xml", s)?;
    let mut doc = Document {
        generation: generation(bytes, "rust-presentation", include_str!("presentations.rs")),
        ..Document::default()
    };
    let mut annotations = Vec::new();
    for (index, slide) in presentation
        .child("sldIdLst")
        .ok_or(CoreError::Parse)?
        .children_named("sldId")
        .enumerate()
    {
        let number = index + 1;
        let rel = rels
            .get(slide.relationship_attr("id").ok_or(CoreError::Parse)?)
            .ok_or(CoreError::Parse)?;
        if rel.external {
            return Err(CoreError::Unsupported);
        }
        let part = &rel.target;
        let xml = package.xml(part, s)?;
        let slide_rels = package.relationships(part, s)?;
        let tree = xml.find("spTree").ok_or(CoreError::Parse)?;
        let title = tree
            .children
            .iter()
            .find(|n| title(n))
            .and_then(|n| n.child("txBody"))
            .map(|n| frame(n, s))
            .transpose()?
            .unwrap_or_default();
        let name = format!(
            "Slide {number}{}",
            if title.trim().is_empty() {
                String::new()
            } else {
                format!(": {title}")
            }
        );
        let mut content = shapes(
            tree,
            part,
            number,
            Some(Transform::default()),
            &mut package,
            &slide_rels,
            s,
            &mut annotations,
        )?;
        for notes in slide_rels
            .values()
            .filter(|r| r.kind.ends_with("/notesSlide"))
        {
            if notes.external {
                return Err(CoreError::Unsupported);
            }
            let xml = package.xml(&notes.target, s)?;
            let mut shapes = Vec::new();
            xml.descendants("sp", &mut shapes);
            for (i, n) in shapes.iter().enumerate() {
                if n.find("ph").and_then(|n| n.attr("type")) == Some("body")
                    && let Some(body) = n.child("txBody")
                {
                    let text = frame(body, s)?;
                    if !text.trim().is_empty() {
                        let mut labelled = String::new();
                        s.emit(&mut labelled, &format!("Notes (Slide {number}):\n"))?;
                        s.emit(&mut labelled, &text)?;
                        content.push(Block {
                            id: format!("{}/notes[{i}]", notes.target),
                            text: labelled,
                            regions: vec![region(number, &notes.target, None, None)],
                            ..Block::default()
                        });
                    }
                }
            }
        }
        add_slide(&mut doc, number, name, content, s)?;
    }
    let stats = s.ctx.stats();
    finish(
        &mut doc,
        s,
        json!({"annotations":annotations,"stats":stats,"reading_policy":"supplied_title_then_position_then_source","macro_policy":"inert_values_only"}),
    )?;
    Ok(doc)
}
fn odf_box(n: &Node) -> Option<BoundingBox> {
    fn point(text: &str) -> Option<f64> {
        let (number, factor) = if let Some(v) = text.strip_suffix("cm") {
            (v, 72.0 / 2.54)
        } else if let Some(v) = text.strip_suffix("mm") {
            (v, 72.0 / 25.4)
        } else if let Some(v) = text.strip_suffix("in") {
            (v, 72.0)
        } else if let Some(v) = text.strip_suffix("pt") {
            (v, 1.0)
        } else {
            return None;
        };
        number
            .parse::<f64>()
            .ok()
            .map(|v| v * factor)
            .filter(|v| v.is_finite() && *v >= 0.0)
    }
    let x = point(n.attr("x")?)?;
    let y = point(n.attr("y")?)?;
    let w = point(n.attr("width")?)?;
    let h = point(n.attr("height")?)?;
    Some(BoundingBox {
        x0: x,
        y0: y,
        x1: x + w,
        y1: y + h,
        unit: CoordinateUnit::Point,
        origin: CoordinateOrigin::TopLeft,
    })
}
fn odp_shapes(
    n: &Node,
    path: &str,
    slide: usize,
    s: &mut Session,
    annotations: &mut Vec<Value>,
) -> Result<Vec<Block>, CoreError> {
    let mut result = Vec::new();
    let mut ordered: Vec<(usize, &Node)> = n.children.iter().enumerate().collect();
    ordered.sort_by(|(i, a), (j, b)| {
        let rank = |n: &Node| {
            if n.attr("class") == Some("title") {
                0
            } else if n.name == "notes" {
                2
            } else {
                1
            }
        };
        rank(a)
            .cmp(&rank(b))
            .then_with(|| match (odf_box(a), odf_box(b)) {
                (Some(a), Some(b)) => {
                    a.y0.total_cmp(&b.y0)
                        .then(a.x0.total_cmp(&b.x0))
                        .then(i.cmp(j))
                }
                (Some(_), None) => std::cmp::Ordering::Less,
                (None, Some(_)) => std::cmp::Ordering::Greater,
                _ => i.cmp(j),
            })
    });
    for (i, child) in ordered {
        s.ctx.work(1)?;
        let id = format!(
            "{path}/shape[{}]",
            child
                .attr("name")
                .map(str::to_owned)
                .unwrap_or_else(|| i.to_string())
        );
        match child.name.as_str() {
            "notes" => {
                let text = odf_frame(child, s)?;
                if !text.trim().is_empty() {
                    let mut labelled = String::new();
                    s.emit(&mut labelled, &format!("Notes (Slide {slide}):\n"))?;
                    s.emit(&mut labelled, &text)?;
                    result.push(Block {
                        id: id.clone(),
                        text: labelled,
                        regions: vec![region(slide, &id, None, None)],
                        ..Block::default()
                    });
                }
            }
            "g" => result.extend(odp_shapes(child, &id, slide, s, annotations)?),
            "frame" | "custom-shape" => {
                if let Some(grid) = child.find("table") {
                    let mut block = table(grid, &id, slide, true, s, annotations)?;
                    block.regions[0].bbox = odf_box(child);
                    result.push(block);
                } else if child.find("image").is_some() {
                    let alt = child
                        .child("desc")
                        .or_else(|| child.child("title"))
                        .map(Node::full_text)
                        .unwrap_or_default();
                    let mut placeholder = String::new();
                    s.emit(&mut placeholder, "[Image")?;
                    if !alt.is_empty() {
                        s.emit(&mut placeholder, ": ")?;
                        s.emit(&mut placeholder, &alt)?;
                    }
                    s.emit(&mut placeholder, "]")?;
                    result.push(Block {
                        id: id.clone(),
                        kind: BlockKind::Figure,
                        text: placeholder,
                        origin: Origin::Derived,
                        regions: vec![region(slide, &id, odf_box(child), None)],
                        ..Block::default()
                    });
                } else {
                    let text = odf_frame(child, s)?;
                    if !text.trim().is_empty() {
                        result.push(Block {
                            id: id.clone(),
                            text,
                            regions: vec![region(slide, &id, odf_box(child), None)],
                            ..Block::default()
                        });
                    }
                }
            }
            _ => {}
        }
    }
    Ok(result)
}
fn odp(bytes: &[u8], mut package: Package<'_>, s: &mut Session) -> Result<Document, CoreError> {
    let xml = package.xml("content.xml", s)?;
    let presentation = xml.find("presentation").ok_or(CoreError::Parse)?;
    let mut doc = Document {
        generation: generation(bytes, "rust-presentation", include_str!("presentations.rs")),
        ..Document::default()
    };
    let mut annotations = Vec::new();
    for (index, page) in presentation.children_named("page").enumerate() {
        let number = index + 1;
        let name = format!(
            "Slide {number}{}",
            page.attr("name")
                .filter(|v| !v.is_empty())
                .map(|v| format!(": {v}"))
                .unwrap_or_default()
        );
        let content = odp_shapes(
            page,
            &format!("content.xml/page[{number}]"),
            number,
            s,
            &mut annotations,
        )?;
        add_slide(&mut doc, number, name, content, s)?;
    }
    let stats = s.ctx.stats();
    finish(
        &mut doc,
        s,
        json!({"annotations":annotations,"stats":stats,"reading_policy":"source_group_order","macro_policy":"inert_values_only"}),
    )?;
    Ok(doc)
}
pub fn extract(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    extract_session(bytes, Session::new(limits)?)
}
pub fn extract_with_context(
    bytes: &[u8],
    limits: Limits,
    ctx: &Context,
) -> Result<Document, CoreError> {
    extract_session(bytes, Session::with_context(limits, ctx.clone())?)
}
fn extract_session(bytes: &[u8], mut s: Session) -> Result<Document, CoreError> {
    let mut package = Package::open(bytes, &mut s)?;
    if package.names.contains("ppt/presentation.xml") {
        pptx(bytes, package, &mut s)
    } else if package.names.contains("mimetype")
        && package.bytes("mimetype", &mut s)? == b"application/vnd.oasis.opendocument.presentation"
    {
        odp(bytes, package, &mut s)
    } else {
        Err(CoreError::Unsupported)
    }
}
