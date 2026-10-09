//! Offline HTML recovery and saved-page extraction; never executes or fetches.
use super::common::{Limits, Session, finish, generation, region};
use super::html_dom::{ElementRef, Html, Node, Selector};
use crate::{
    CoreError,
    canonical::{
        Block, BlockKind, Cell, CellRange, Document, HeaderRole, Origin, SourceRegion, Table,
    },
    detection::{DecodedText, decode_text},
};
use encoding_rs::{DecoderResult, Encoding};
use mailparse::{MailHeaderMap, ParsedMail};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

fn selector(value: &str) -> Result<Selector, CoreError> {
    Selector::parse(value).map_err(|_| CoreError::Internal)
}
fn charset(bytes: &[u8], hint: Option<&str>, s: &mut Session) -> Result<DecodedText, CoreError> {
    let Some(label) = hint else {
        return decode_text(bytes);
    };
    let encoding = Encoding::for_label(label.trim().as_bytes()).ok_or(CoreError::Unsupported)?;
    let mut decoder = encoding.new_decoder_with_bom_removal();
    let mut text = String::new();
    let mut pos = 0;
    let mut errors = 0;
    let mut buffer = [0u8; 4096];
    loop {
        s.ctx.work(1)?;
        let (result, read, written) =
            decoder.decode_to_utf8_without_replacement(&bytes[pos..], &mut buffer, true);
        pos += read;
        let part = std::str::from_utf8(&buffer[..written]).map_err(|_| CoreError::Internal)?;
        s.reserve(part.len().saturating_mul(4) + 64)?;
        text.push_str(part);
        match result {
            DecoderResult::InputEmpty => break,
            DecoderResult::OutputFull => {}
            DecoderResult::Malformed(..) => {
                text.push('\u{fffd}');
                errors += 1;
            }
        }
        if text.len() > s.limits.budget.max_output_chars.saturating_mul(4) {
            return Err(CoreError::Budget);
        }
    }
    Ok(DecodedText {
        text,
        encoding: encoding.name().into(),
        evidence: "supplied_charset".into(),
        errors,
    })
}
fn preflight(source: &str, s: &mut Session) -> Result<(), CoreError> {
    let nodes = source.bytes().filter(|&b| b == b'<').count();
    s.reserve(nodes.checked_mul(2048).ok_or(CoreError::Budget)?)?;
    let mut depth = 0usize;
    let mut chars = source.char_indices().peekable();
    while let Some((start, c)) = chars.next() {
        if c != '<' {
            continue;
        }
        s.ctx.work(1)?;
        let mut quote = None;
        let mut end = start;
        for (n, c) in chars.by_ref() {
            if quote == Some(c) {
                quote = None
            } else if quote.is_none() && matches!(c, '\'' | '"') {
                quote = Some(c)
            } else if quote.is_none() && c == '>' {
                end = n;
                break;
            }
        }
        if end == start {
            break;
        }
        let token = source[start + 1..end].trim();
        if token.starts_with(['!', '?']) {
            continue;
        }
        if token.starts_with('/') {
            depth = depth.saturating_sub(1);
            continue;
        }
        let tag = token
            .split_whitespace()
            .next()
            .unwrap_or("")
            .trim_end_matches('/')
            .to_ascii_lowercase();
        if !token.ends_with('/')
            && !matches!(
                tag.as_str(),
                "area"
                    | "base"
                    | "br"
                    | "col"
                    | "embed"
                    | "hr"
                    | "img"
                    | "input"
                    | "link"
                    | "meta"
                    | "param"
                    | "source"
                    | "track"
                    | "wbr"
            )
        {
            depth += 1;
        }
        if depth > s.limits.max_depth {
            return Err(CoreError::Budget);
        }
    }
    Ok(())
}
fn blocked(name: &str) -> bool {
    matches!(
        name,
        "script" | "style" | "noscript" | "template" | "svg" | "head"
    )
}
fn inline(
    e: ElementRef<'_>,
    out: &mut String,
    s: &mut Session,
    links: &mut Vec<Value>,
    path: &str,
    depth: usize,
) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    if depth > s.limits.max_depth {
        return Err(CoreError::Budget);
    }
    if blocked(e.value().name()) {
        return Ok(());
    }
    if e.value().name() == "a"
        && let Some(target) = e.value().attr("href")
    {
        s.reserve(target.len().saturating_mul(4) + 512)?;
        links.push(json!({"path":path,"target":target,"origin":"source"}));
    }
    let mut counts = BTreeMap::new();
    for node in e.children() {
        if let Node::Text(t) = node.value() {
            s.reserve(t.text.len().saturating_mul(4) + 64)?;
            out.push_str(&t.text);
        } else if let Some(child) = ElementRef::wrap(node) {
            let name = child.value().name();
            let n = counts.entry(name).or_insert(0usize);
            *n += 1;
            let child_path = format!("{path}/{name}[{n}]");
            if name == "br" {
                out.push('\n')
            } else {
                inline(child, out, s, links, &child_path, depth + 1)?;
            }
        }
        if out.len() > s.limits.budget.max_output_chars.saturating_mul(4) {
            return Err(CoreError::Budget);
        }
    }
    Ok(())
}
fn text(
    e: ElementRef<'_>,
    s: &mut Session,
    links: &mut Vec<Value>,
    path: &str,
    pre: bool,
) -> Result<String, CoreError> {
    let mut out = String::new();
    inline(e, &mut out, s, links, path, 1)?;
    if pre {
        return Ok(out.replace("\r\n", "\n").replace('\r', "\n"));
    }
    s.reserve(out.len().saturating_mul(4) + 128)?;
    Ok(out.split_whitespace().collect::<Vec<_>>().join(" "))
}
fn numeric(e: ElementRef<'_>, attr: &str) -> Result<usize, CoreError> {
    let n = e
        .value()
        .attr(attr)
        .unwrap_or("1")
        .parse::<usize>()
        .map_err(|_| CoreError::Parse)?;
    if n == 0 {
        return Err(CoreError::Unsupported);
    }
    Ok(n)
}
fn table(
    e: ElementRef<'_>,
    s: &mut Session,
    links: &mut Vec<Value>,
    path: &str,
) -> Result<Block, CoreError> {
    let mut table = Table::default();
    let mut occupied = BTreeSet::new();
    let mut headers = BTreeMap::new();
    let mut output = String::new();
    let mut rows = vec![];
    let mut section_counts = BTreeMap::new();
    for child in e.child_elements() {
        let name = child.value().name();
        let section_number = section_counts.entry(name).or_insert(0usize);
        *section_number += 1;
        if child.value().name() == "tr" {
            rows.push((child, format!("{path}/tr[{section_number}]")));
        } else if matches!(child.value().name(), "thead" | "tbody" | "tfoot") {
            let name = child.value().name();
            for (n, tr) in child
                .child_elements()
                .filter(|c| c.value().name() == "tr")
                .enumerate()
            {
                rows.push((tr, format!("{path}/{name}[{section_number}]/tr[{}]", n + 1)));
            }
        }
    }
    s.reserve(rows.len().saturating_mul(1024))?;
    for (index, (tr, row_path)) in rows.iter().enumerate() {
        s.ctx.work(1)?;
        let row = index + 1;
        table.rows = table.rows.max(row);
        let mut column = 1usize;
        if row > 1 {
            output.push('\n')
        }
        output.push_str(&format!("Row {row}: "));
        let mut counts = BTreeMap::new();
        for cell in tr
            .child_elements()
            .filter(|c| matches!(c.value().name(), "td" | "th"))
        {
            while occupied.contains(&(row, column)) {
                s.ctx.work(1)?;
                column += 1;
            }
            let rs = numeric(cell, "rowspan")?;
            let cs = numeric(cell, "colspan")?;
            let area = rs.checked_mul(cs).ok_or(CoreError::Budget)?;
            s.ctx.work(area)?;
            s.reserve(area.checked_mul(64).ok_or(CoreError::Budget)? + 2048)?;
            let end_row = row.checked_add(rs - 1).ok_or(CoreError::Budget)?;
            let end_column = column.checked_add(cs - 1).ok_or(CoreError::Budget)?;
            table.rows = table.rows.max(end_row);
            table.columns = table.columns.max(end_column);
            for r in row..=end_row {
                for c in column..=end_column {
                    if !occupied.insert((r, c)) {
                        return Err(CoreError::Parse);
                    }
                }
            }
            let tag = cell.value().name();
            let count = counts.entry(tag).or_insert(0usize);
            *count += 1;
            let cell_path = format!("{row_path}/{tag}[{count}]");
            let value = text(cell, s, links, &cell_path, false)?;
            let role = if tag == "th" {
                if cell.value().attr("scope") == Some("row") {
                    HeaderRole::Row
                } else {
                    HeaderRole::Column
                }
            } else {
                HeaderRole::Unknown
            };
            if row == 1 && role == HeaderRole::Column {
                for c in column..=end_column {
                    headers.insert(c, value.clone());
                }
            }
            let label = headers
                .get(&column)
                .map(|h| format!(" [{h}]"))
                .unwrap_or_default();
            s.reserve(label.len().saturating_mul(4) + value.len().saturating_mul(4) + 128)?;
            if column > 1 {
                output.push_str(" | ")
            }
            output.push_str(&format!("C{column}{label}={value}"));
            if output.len() > s.limits.budget.max_output_chars.saturating_mul(4) {
                return Err(CoreError::Budget);
            }
            table.cells.push(Cell {
                row,
                column,
                row_span: rs,
                column_span: cs,
                text: value,
                header_role: role,
                role_origin: Origin::Source,
                regions: vec![SourceRegion {
                    cell_range: Some(CellRange {
                        row_start: row,
                        row_end: end_row,
                        column_start: column,
                        column_end: end_column,
                    }),
                    ..region(cell_path)
                }],
                ..Cell::default()
            });
            column += cs;
        }
    }
    if table.rows == 0 || table.columns == 0 {
        return Err(CoreError::Parse);
    }
    Ok(Block {
        kind: BlockKind::Table,
        text: output,
        table: Some(table),
        regions: vec![region(path)],
        ..Block::default()
    })
}
fn nested_tables(
    e: ElementRef<'_>,
    doc: &mut Document,
    s: &mut Session,
    w: &mut Walk,
    path: &str,
    depth: usize,
) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    if depth > s.limits.max_depth {
        return Err(CoreError::Budget);
    }
    let mut counts = BTreeMap::new();
    for child in e.child_elements() {
        let name = child.value().name();
        let n = counts.entry(name).or_insert(0usize);
        *n += 1;
        let child_path = format!("{path}/{name}[{n}]");
        if name == "table" {
            let b = table(child, s, &mut w.links, &child_path)?;
            s.push(doc, b)?;
        }
        nested_tables(child, doc, s, w, &child_path, depth + 1)?;
    }
    Ok(())
}
struct Walk {
    headings: Vec<(usize, String, String)>,
    links: Vec<Value>,
    removed: Vec<String>,
    scope: u8,
}
fn walk(
    e: ElementRef<'_>,
    doc: &mut Document,
    s: &mut Session,
    w: &mut Walk,
    path: &str,
    inside: bool,
    depth: usize,
) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    if depth > s.limits.max_depth {
        return Err(CoreError::Budget);
    }
    let name = e.value().name();
    if blocked(name) {
        return Ok(());
    }
    let inside = inside || w.scope == 1 && name == "main" || w.scope == 2 && name == "article";
    let classes = format!(
        "{} {}",
        e.value().attr("class").unwrap_or(""),
        e.value().attr("id").unwrap_or("")
    );
    let footnote = e.value().attr("epub:type").or(e.value().attr("type")) == Some("footnote")
        || e.value().attr("role") == Some("doc-footnote");
    let boilerplate = !footnote && matches!(name, "nav" | "footer" | "aside")
        || e.value().attr("role") == Some("navigation")
        || classes.contains("cookie-banner")
        || classes.contains("cookie-consent");
    let protects_table =
        boilerplate && (name == "table" || e.select(&selector("table")?).next().is_some());
    if boilerplate && !protects_table {
        w.removed.push(path.into());
        return Ok(());
    }
    if inside {
        let kind = match name {
            "h1" | "h2" | "h3" | "h4" | "h5" | "h6" => Some(BlockKind::Heading),
            _ if footnote => Some(BlockKind::Footnote),
            "p" => Some(BlockKind::Paragraph),
            "li" => Some(BlockKind::List),
            "pre" => Some(BlockKind::Code),
            "table" => Some(BlockKind::Table),
            _ => None,
        };
        if let Some(kind) = kind {
            let mut b = if kind == BlockKind::Table {
                table(e, s, &mut w.links, path)?
            } else {
                Block {
                    kind,
                    text: text(e, s, &mut w.links, path, kind == BlockKind::Code)?,
                    regions: vec![region(path)],
                    ..Block::default()
                }
            };
            if kind == BlockKind::Heading {
                let level = name[1..].parse().map_err(|_| CoreError::Internal)?;
                while w.headings.last().is_some_and(|(l, _, _)| *l >= level) {
                    w.headings.pop();
                }
                b.heading_level = Some(level);
                b.parent_id = w.headings.last().map(|(_, _, id)| id.clone());
                w.headings
                    .push((level, b.text.clone(), format!("b{}", doc.blocks.len() + 1)));
            } else {
                b.parent_id = w.headings.last().map(|(_, _, id)| id.clone());
            }
            b.heading_path = w.headings.iter().map(|(_, t, _)| t.clone()).collect();
            s.push(doc, b)?;
            // Nested tables have their own structure/provenance, not only parent-cell text.
            if kind == BlockKind::Table {
                nested_tables(e, doc, s, w, path, depth + 1)?;
            }
            return Ok(());
        }
    }
    let mut counts = BTreeMap::new();
    for node in e.children() {
        if let Some(child) = ElementRef::wrap(node) {
            let tag = child.value().name();
            let n = counts.entry(tag).or_insert(0usize);
            *n += 1;
            walk(
                child,
                doc,
                s,
                w,
                &format!("{path}/{tag}[{n}]"),
                inside,
                depth + 1,
            )?;
        } else if inside
            && let Node::Text(t) = node.value()
            && !t.text.trim().is_empty()
        {
            s.push(
                doc,
                Block {
                    text: t.text.trim().into(),
                    regions: vec![region(format!("{path}/text()"))],
                    heading_path: w.headings.iter().map(|(_, t, _)| t.clone()).collect(),
                    ..Block::default()
                },
            )?;
        }
    }
    Ok(())
}
/// Reused by EPUB; the caller retains its package-wide session and provenance.
pub fn append_html(
    bytes: &[u8],
    hint: Option<&str>,
    doc: &mut Document,
    s: &mut Session,
    prefix: &str,
) -> Result<Value, CoreError> {
    s.reserve(bytes.len().saturating_mul(8) + 4096)?;
    let decoded = charset(bytes, hint, s)?;
    preflight(&decoded.text, s)?;
    let dom = Html::parse_document(&decoded.text, s)?;
    let title = dom
        .select(&selector("title")?)
        .next()
        .map(|e| e.text().collect::<String>());
    let dates: Vec<String> = dom
        .select(&selector("meta")?)
        .filter(|e| {
            e.value()
                .attr("name")
                .or(e.value().attr("property"))
                .is_some_and(|n| {
                    matches!(
                        n,
                        "date" | "article:published_time" | "article:modified_time"
                    )
                })
        })
        .filter_map(|e| e.value().attr("content").map(str::to_owned))
        .collect();
    let scope = if dom.select(&selector("main")?).next().is_some() {
        1
    } else if dom.select(&selector("article")?).next().is_some() {
        2
    } else {
        0
    };
    let mut w = Walk {
        headings: vec![],
        links: vec![],
        removed: vec![],
        scope,
    };
    walk(
        dom.root_element(),
        doc,
        s,
        &mut w,
        &format!("{prefix}/html[1]"),
        scope == 0,
        1,
    )?;
    Ok(
        json!({"encoding":decoded.encoding,"decoding_errors":decoded.errors,"title":title,"dates":dates,"links":w.links,"removed_boilerplate":w.removed,"main_content":"heuristic"}),
    )
}
fn mime_root<'a>(
    mail: &'a ParsedMail<'a>,
    start: Option<&str>,
    depth: usize,
    s: &mut Session,
) -> Result<Option<&'a ParsedMail<'a>>, CoreError> {
    s.ctx.work(1)?;
    if depth > s.limits.max_depth {
        return Err(CoreError::Budget);
    }
    if matches!(
        mail.ctype.mimetype.as_str(),
        "text/html" | "application/xhtml+xml"
    ) && start.is_none_or(|id| {
        mail.headers
            .get_first_value("Content-ID")
            .is_some_and(|v| v.trim_matches(['<', '>']) == id.trim_matches(['<', '>']))
    }) {
        return Ok(Some(mail));
    }
    for child in &mail.subparts {
        if let Some(part) = mime_root(child, start, depth + 1, s)? {
            return Ok(Some(part));
        }
    }
    Ok(None)
}
pub fn parse(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    let mut s = Session::new(bytes, limits)?;
    let mut doc = Document {
        generation: generation(
            bytes,
            "rust-html",
            &format!("{}{}", include_str!("html.rs"), include_str!("html_dom.rs")),
        ),
        ..Document::default()
    };
    let ascii = String::from_utf8_lossy(&bytes[..bytes.len().min(8192)]).to_ascii_lowercase();
    let diagnostics = if ascii.contains("content-type: multipart/related") {
        let header_count = bytes
            .windows(13)
            .filter(|w| w.eq_ignore_ascii_case(b"content-type:"))
            .count();
        if header_count > limits.max_depth {
            return Err(CoreError::Budget);
        }
        s.reserve(header_count.saturating_mul(8192) + bytes.len().saturating_mul(8))?;
        let mail = mailparse::parse_mail(bytes).map_err(|_| CoreError::Parse)?;
        let start = mail.ctype.params.get("start").map(String::as_str);
        let root = mime_root(&mail, start, 1, &mut s)?.ok_or(CoreError::Unsupported)?;
        let body = root.get_body_raw().map_err(|_| CoreError::Parse)?;
        append_html(
            &body,
            root.ctype.params.get("charset").map(String::as_str),
            &mut doc,
            &mut s,
            "mhtml:root",
        )?
    } else {
        // Honor a simple supplied meta charset before statistical decoding.
        let hint = ascii.find("charset=").and_then(|n| {
            ascii[n + 8..]
                .trim_start_matches(['\'', '"', ' '])
                .split(['\'', '"', ' ', '>', ';'])
                .next()
        });
        append_html(bytes, hint, &mut doc, &mut s, "")?
    };
    doc.generation.dependency_versions.extend([
        ("html5ever".into(), "0.35.0".into()),
        ("markup5ever_rcdom".into(), "0.35.0+unofficial".into()),
        ("mailparse".into(), "0.16.1".into()),
    ]);
    let partial = diagnostics["decoding_errors"].as_u64().unwrap_or(0) > 0;
    finish(&mut doc, &mut s, diagnostics, partial)?;
    Ok(doc)
}
