//! Bounded ODF text; source locations and supplied roles, without conversion.
use super::package::{Content, Limits, Node, Package, Session, finish, generation};
use crate::{
    CoreError,
    canonical::{Block, BlockKind, Cell, Document, SourceRegion, Table},
    runtime::Context,
};
use serde_json::json;
use std::collections::BTreeMap;

const OFFICE: &str = "urn:oasis:names:tc:opendocument:xmlns:office:1.0";
const TEXT: &str = "urn:oasis:names:tc:opendocument:xmlns:text:1.0";
const TABLE: &str = "urn:oasis:names:tc:opendocument:xmlns:table:1.0";
fn is(n: &Node, ns: &str, name: &str) -> bool {
    n.ns == ns && n.name == name
}
fn count(n: &Node, name: &str) -> Result<usize, CoreError> {
    n.attr(name)
        .map(|v| v.parse().map_err(|_| CoreError::Parse))
        .unwrap_or(Ok(1))
}
fn location(path: &str) -> SourceRegion {
    SourceRegion {
        name: Some(path.to_owned()),
        ..SourceRegion::default()
    }
}
fn push(
    d: &mut Document,
    s: &mut Session,
    mut block: Block,
    path: &str,
) -> Result<String, CoreError> {
    s.ctx.work(1)?;
    s.reserve(1024 + path.len() * 4)?;
    block.id = format!("b{}", d.blocks.len() + 1);
    block.regions.push(location(path));
    let id = block.id.clone();
    d.blocks.push(block);
    Ok(id)
}
fn inline(
    n: &Node,
    out: &mut String,
    s: &mut Session,
    notes: &mut Vec<String>,
) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    if is(n, TEXT, "note") {
        let body = n
            .children
            .iter()
            .find(|c| is(c, TEXT, "note-body"))
            .ok_or(CoreError::Parse)?;
        let mut text = String::new();
        for (i, p) in body.children.iter().enumerate() {
            if !is(p, TEXT, "p") {
                return Err(CoreError::Unsupported);
            }
            if i > 0 {
                s.emit(&mut text, "\n")?;
            }
            let mut nested = Vec::new();
            inline(p, &mut text, s, &mut nested)?;
            if !nested.is_empty() {
                return Err(CoreError::Unsupported);
            }
        }
        s.reserve(128)?;
        notes.push(text);
        return Ok(());
    }
    if is(n, TEXT, "s") {
        let c = count(n, "c")?;
        s.ctx.work(c)?;
        for _ in 0..c {
            s.emit(out, " ")?;
        }
        return Ok(());
    }
    if is(n, TEXT, "tab") {
        return s.emit(out, "\t");
    }
    if is(n, TEXT, "line-break") {
        return s.emit(out, "\n");
    }
    if n.ns != TEXT
        || !matches!(
            n.name.as_str(),
            "p" | "h"
                | "span"
                | "a"
                | "bookmark"
                | "bookmark-start"
                | "bookmark-end"
                | "soft-page-break"
        )
    {
        return Err(CoreError::Unsupported);
    }
    for c in &n.content {
        match c {
            Content::Text(t) => s.emit(out, t)?,
            Content::Child(i) => inline(&n.children[*i], out, s, notes)?,
        }
    }
    Ok(())
}
fn table(n: &Node, s: &mut Session, path: &str) -> Result<Block, CoreError> {
    let mut cells = Vec::new();
    let mut rows = 0;
    let mut columns = None;
    let mut text = String::new();
    for row in &n.children {
        if is(row, TABLE, "table-column") {
            if count(row, "number-columns-repeated")? != 1 {
                return Err(CoreError::Unsupported);
            }
            continue;
        }
        if !is(row, TABLE, "table-row") || count(row, "number-rows-repeated")? != 1 {
            return Err(CoreError::Unsupported);
        }
        rows += 1;
        let mut width = 0;
        if rows > 1 {
            s.emit(&mut text, "\n")?;
        }
        for cell in &row.children {
            s.ctx.work(1)?;
            if !is(cell, TABLE, "table-cell")
                || [
                    "number-columns-repeated",
                    "number-columns-spanned",
                    "number-rows-spanned",
                ]
                .iter()
                .any(|k| count(cell, k) != Ok(1))
            {
                return Err(CoreError::Unsupported);
            }
            width += 1;
            let mut scalar = String::new();
            for (i, p) in cell.children.iter().enumerate() {
                if !is(p, TEXT, "p") {
                    return Err(CoreError::Unsupported);
                }
                if i > 0 {
                    s.emit(&mut scalar, "\n")?;
                }
                let mut notes = Vec::new();
                inline(p, &mut scalar, s, &mut notes)?;
                if !notes.is_empty() {
                    return Err(CoreError::Unsupported);
                }
            }
            if width > 1 {
                s.emit(&mut text, "\t")?;
            }
            s.reserve(scalar.len() * 4 + 1024)?;
            text.push_str(&scalar);
            cells.push(Cell {
                row: rows,
                column: width,
                row_span: 1,
                column_span: 1,
                text: scalar,
                regions: vec![location(&format!(
                    "{path}/table:table-row[{rows}]/table:table-cell[{width}]"
                ))],
                ..Cell::default()
            });
        }
        if width == 0 || columns.is_some_and(|c| c != width) {
            return Err(CoreError::Unsupported);
        }
        columns = Some(width);
    }
    if rows == 0 {
        return Err(CoreError::Unsupported);
    }
    Ok(Block {
        kind: BlockKind::Table,
        text,
        table: Some(Table {
            rows,
            columns: columns.unwrap(),
            cells,
            caption: None,
        }),
        ..Block::default()
    })
}
fn walk(
    n: &Node,
    d: &mut Document,
    s: &mut Session,
    path: &str,
    list: bool,
    headings: &mut Vec<(usize, String, String)>,
) -> Result<(), CoreError> {
    let mut counters = BTreeMap::new();
    for c in &n.children {
        s.ctx.work(1)?;
        let key = format!(
            "{}:{}",
            if c.ns == TEXT {
                "text"
            } else if c.ns == TABLE {
                "table"
            } else {
                "unknown"
            },
            c.name
        );
        let ordinal = counters.entry(key.clone()).or_insert(0usize);
        *ordinal += 1;
        let next = format!("{path}/{key}[{ordinal}]");
        if is(c, TEXT, "p") || is(c, TEXT, "h") {
            let level = if is(c, TEXT, "h") {
                let l = count(c, "outline-level")?;
                if !(1..=6).contains(&l) {
                    return Err(CoreError::Unsupported);
                }
                Some(l)
            } else {
                None
            };
            if let Some(l) = level {
                while headings.last().is_some_and(|(prior, _, _)| *prior >= l) {
                    headings.pop();
                }
            }
            let mut text = String::new();
            let mut notes = Vec::new();
            inline(c, &mut text, s, &mut notes)?;
            let heading_text = text.clone();
            s.reserve(heading_text.len() * 4)?;
            let id = push(
                d,
                s,
                Block {
                    kind: if level.is_some() {
                        BlockKind::Heading
                    } else if list {
                        BlockKind::List
                    } else {
                        BlockKind::Paragraph
                    },
                    text,
                    heading_level: level,
                    parent_id: headings.last().map(|(_, _, id)| id.clone()),
                    heading_path: headings.iter().map(|(_, t, _)| t.clone()).collect(),
                    ..Block::default()
                },
                &next,
            )?;
            if let Some(l) = level {
                headings.push((l, heading_text, id.clone()));
            }
            for (i, text) in notes.into_iter().enumerate() {
                push(
                    d,
                    s,
                    Block {
                        kind: BlockKind::Footnote,
                        text,
                        parent_id: Some(id.clone()),
                        ..Block::default()
                    },
                    &format!("{next}/text:note[{}]", i + 1),
                )?;
            }
        } else if is(c, TABLE, "table") {
            let b = table(c, s, &next)?;
            push(d, s, b, &next)?;
        } else if c.ns == TEXT && matches!(c.name.as_str(), "list" | "list-item" | "section") {
            walk(c, d, s, &next, list || c.name == "list", headings)?;
        } else {
            return Err(CoreError::Unsupported);
        }
    }
    Ok(())
}
pub fn extract(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    extract_with_context(bytes, limits, Session::new(limits)?.ctx)
}
pub fn extract_with_context(
    bytes: &[u8],
    limits: Limits,
    ctx: Context,
) -> Result<Document, CoreError> {
    let mut s = Session::with_context(limits, ctx)?;
    let mut package = Package::open(bytes, &mut s)?;
    if package.bytes("mimetype", &mut s)? != b"application/vnd.oasis.opendocument.text" {
        return Err(CoreError::Unsupported);
    }
    if package
        .names
        .iter()
        .any(|p| p.starts_with("Scripts/") || p.starts_with("Basic/"))
    {
        return Err(CoreError::Unsupported);
    }
    if package.names.contains("META-INF/manifest.xml") {
        let manifest = package.xml("META-INF/manifest.xml", &mut s)?;
        if manifest.find("encryption-data").is_some() {
            return Err(CoreError::Unsupported);
        }
    }
    let xml = package.xml("content.xml", &mut s)?;
    if !is(&xml, OFFICE, "document-content") {
        return Err(CoreError::Unsupported);
    }
    let body = xml
        .children
        .iter()
        .find(|n| is(n, OFFICE, "body"))
        .and_then(|n| n.children.iter().find(|c| is(c, OFFICE, "text")))
        .ok_or(CoreError::Parse)?;
    let mut d = Document {
        generation: generation(bytes, "odt-rust", include_str!("odt.rs")),
        ..Document::default()
    };
    walk(
        body,
        &mut d,
        &mut s,
        "content.xml/office:text",
        false,
        &mut Vec::new(),
    )?;
    finish(&mut d, &mut s, json!({"format":"odt","candidate":true}))?;
    let rendered = crate::canonical::render(d.clone())?;
    if rendered.rendered_text.chars().count() > limits.budget.max_output_chars {
        return Err(CoreError::Budget);
    }
    Ok(d)
}
