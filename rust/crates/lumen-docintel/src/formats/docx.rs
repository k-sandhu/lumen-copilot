//! DOCX candidate: accepted revisions, table parity and native locations.
use super::package::{Limits, Node, Package, Relationship, Session, finish, generation};
use crate::{
    CoreError,
    canonical::{
        Block, BlockKind, Cell, CellRange, Document, HeaderRole, Origin, SourceRegion, Table,
    },
    runtime::Context,
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

const WORD: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const STRICT_WORD: &str = "http://purl.oclc.org/ooxml/wordprocessingml/main";
fn word(n: &Node) -> bool {
    n.ns == WORD || n.ns == STRICT_WORD
}
fn number(value: Option<&str>, default: usize) -> Result<usize, CoreError> {
    value
        .map(|v| v.parse().map_err(|_| CoreError::Parse))
        .unwrap_or(Ok(default))
}
fn region(path: &str, range: Option<CellRange>) -> SourceRegion {
    SourceRegion {
        name: Some(path.to_owned()),
        cell_range: range,
        ..SourceRegion::default()
    }
}
#[derive(Default)]
struct Annotations {
    entries: Vec<Value>,
    references: Vec<(String, String, String)>,
}
fn inline(
    n: &Node,
    out: &mut String,
    s: &mut Session,
    a: &mut Annotations,
    path: &str,
    rels: &BTreeMap<String, Relationship>,
) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    if word(n) {
        match n.name.as_str() {
            "del" | "moveFrom" | "pPr" | "rPr" | "instrText" => return Ok(()),
            "t" => {
                s.emit(out, &n.text)?;
                return Ok(());
            }
            "tab" => {
                s.emit(out, "\t")?;
                return Ok(());
            }
            "br" | "cr" => {
                s.emit(out, "\n")?;
                return Ok(());
            }
            "footnoteReference" | "endnoteReference" => {
                let kind = if n.name == "footnoteReference" {
                    "footnote"
                } else {
                    "endnote"
                };
                let id = n.attr("id").ok_or(CoreError::Parse)?.to_owned();
                a.references
                    .push((kind.to_owned(), id.clone(), path.to_owned()));
                a.entries.push(
                    json!({"kind":kind,"id":id,"reference":path,"local_char":out.chars().count()}),
                );
                return Ok(());
            }
            "commentReference" | "commentRangeStart" | "commentRangeEnd" => {
                a.entries.push(json!({"kind":n.name,"id":n.attr("id"),"reference":path,"local_char":out.chars().count()}));
                return Ok(());
            }
            "hyperlink" => {
                let target = n
                    .attr("id")
                    .and_then(|id| rels.get(id))
                    .map(|r| r.target.as_str())
                    .or(n.attr("anchor"));
                a.entries
                    .push(json!({"kind":"hyperlink","reference":path,"target":target}));
            }
            "drawing" | "pict" => {
                let mut boxes = Vec::new();
                n.descendants("txbxContent", &mut boxes);
                if !boxes.is_empty() {
                    for b in boxes {
                        for p in b.children_named("p") {
                            inline(p, out, s, a, path, rels)?;
                        }
                    }
                } else {
                    let alt = n
                        .find("docPr")
                        .or_else(|| n.find("shape"))
                        .and_then(|p| p.attr("descr").or(p.attr("title")))
                        .unwrap_or("");
                    let start = out.chars().count();
                    s.emit(out, "[Image")?;
                    if !alt.is_empty() {
                        s.emit(out, ": ")?;
                        s.emit(out, alt)?;
                    }
                    s.emit(out, "]")?;
                    a.entries.push(json!({"kind":"image_placeholder","reference":path,"alt_text":alt,"char_start":start,"char_end":out.chars().count(),"origin":"derived"}));
                }
                return Ok(());
            }
            _ => {}
        }
    }
    for child in &n.children {
        inline(child, out, s, a, path, rels)?;
    }
    Ok(())
}
struct OriginCell<'a> {
    node: &'a Node,
    row: usize,
    column: usize,
    row_span: usize,
    column_span: usize,
    scalar: String,
    nested: bool,
}
struct TableResult {
    text: String,
    table: Table,
}
fn blocks_text(
    n: &Node,
    depth: usize,
    s: &mut Session,
    a: &mut Annotations,
    path: &str,
    rels: &BTreeMap<String, Relationship>,
) -> Result<String, CoreError> {
    let mut text = String::new();
    let mut index = 0;
    for child in n
        .children
        .iter()
        .filter(|n| word(n) && (n.name == "p" || n.name == "tbl"))
    {
        if index > 0 {
            s.emit(&mut text, "\n")?;
        }
        index += 1;
        let path = format!("{path}/{}[{index}]", child.name);
        if child.name == "p" {
            inline(child, &mut text, s, a, &path, rels)?;
        } else {
            let result = table(child, depth + 1, s, a, &path, rels)?;
            s.emit(&mut text, &result.text)?;
        }
    }
    Ok(text)
}
fn table(
    n: &Node,
    depth: usize,
    s: &mut Session,
    a: &mut Annotations,
    path: &str,
    rels: &BTreeMap<String, Relationship>,
) -> Result<TableResult, CoreError> {
    if depth > 32 {
        return Err(CoreError::Budget);
    }
    let declared = n
        .child("tblGrid")
        .map(|n| n.children_named("gridCol").count())
        .unwrap_or(0);
    s.ctx.work(declared)?;
    let rows: Vec<&Node> = n.children_named("tr").collect();
    if rows.is_empty() {
        return Err(CoreError::Parse);
    }
    let mut origins: Vec<OriginCell<'_>> = Vec::new();
    let mut grids: Vec<BTreeMap<usize, usize>> = Vec::new();
    let mut widths = Vec::new();
    let mut columns = declared;
    for (r, row) in rows.iter().enumerate() {
        s.ctx.work(1)?;
        let mut column = number(row.child("trPr").and_then(|n| n.val("gridBefore")), 0)?
            .checked_add(1)
            .ok_or(CoreError::Budget)?;
        s.ctx.work(column)?;
        let mut grid = BTreeMap::new();
        let mut extended = BTreeSet::new();
        for cell in row.children_named("tc") {
            let span = number(cell.child("tcPr").and_then(|n| n.val("gridSpan")), 1)?;
            if span == 0 {
                return Err(CoreError::Parse);
            }
            s.ctx.work(span.checked_add(1).ok_or(CoreError::Budget)?)?;
            s.reserve(span.checked_mul(128).ok_or(CoreError::Budget)?)?;
            let merge = cell.child("tcPr").and_then(|n| n.child("vMerge"));
            let index = if merge.is_some_and(|n| n.attr("val").unwrap_or("continue") == "continue")
            {
                let previous = grids.last().ok_or(CoreError::Parse)?;
                let index = *previous.get(&column).ok_or(CoreError::Parse)?;
                if origins[index].column != column || origins[index].column_span != span {
                    return Err(CoreError::Parse);
                }
                if extended.insert(index) {
                    origins[index].row_span += 1;
                }
                index
            } else {
                let mut scalar = String::new();
                let mut count = 0;
                for p in cell.children_named("p") {
                    if count > 0 {
                        s.emit(&mut scalar, "\n")?;
                    }
                    count += 1;
                    inline(
                        p,
                        &mut scalar,
                        s,
                        a,
                        &format!("{path}/R{}C{column}/p[{count}]", r + 1),
                        rels,
                    )?;
                }
                let nested = cell.child("tbl").is_some();
                if nested {
                    scalar = scalar.trim_matches('\n').to_owned();
                }
                s.reserve(512 + scalar.len() * 2)?;
                origins.push(OriginCell {
                    node: cell,
                    row: r + 1,
                    column,
                    row_span: 1,
                    column_span: span,
                    scalar,
                    nested,
                });
                origins.len() - 1
            };
            let end = column.checked_add(span).ok_or(CoreError::Budget)?;
            for c in column..end {
                grid.insert(c, index);
            }
            column = end;
        }
        let after = number(row.child("trPr").and_then(|n| n.val("gridAfter")), 0)?;
        let width = declared.max(
            column
                .checked_sub(1)
                .and_then(|n| n.checked_add(after))
                .ok_or(CoreError::Budget)?,
        );
        s.ctx.work(width)?;
        columns = columns.max(width);
        widths.push(width);
        grids.push(grid);
    }
    if columns == 0 {
        return Err(CoreError::Parse);
    }
    let mut text = String::new();
    s.emit(&mut text, "[Table]")?;
    let mut headers: Vec<String> = Vec::new();
    let mut char_offset = 0;
    let mut byte_offset = 0;
    for (r, grid) in grids.iter().enumerate() {
        s.emit(&mut text, &format!("\nRow {}: ", r + 1))?;
        for c in 1..=widths[r] {
            s.ctx.work(1)?;
            if c > 1 {
                s.emit(&mut text, " | ")?;
            }
            let origin = grid.get(&c).map(|&i| &origins[i]);
            let nested = origin
                .filter(|o| o.nested)
                .map(|o| format!(" [nested tables at R{}C{}]", o.row, o.column))
                .unwrap_or_default();
            if r == 0 {
                let label = origin
                    .map(|o| o.scalar.replace('\n', " / "))
                    .unwrap_or_default()
                    + &nested;
                s.reserve(label.len() * 4 + 64)?;
                headers.push(label);
            }
            let label = if r > 0 {
                headers
                    .get(c - 1)
                    .filter(|h| !h.is_empty())
                    .map(|h| format!(" [{h}]"))
                    .unwrap_or_default()
            } else {
                String::new()
            };
            s.emit(&mut text, &format!("C{c}{label}="))?;
            char_offset += text[byte_offset..].chars().count();
            byte_offset = text.len();
            let start = char_offset;
            if let Some(o) = origin {
                if o.row == r + 1 && o.column == c {
                    let body =
                        blocks_text(o.node, depth, s, a, &format!("{path}/R{}C{c}", r + 1), rels)?;
                    s.emit(&mut text, &body)?;
                } else {
                    s.emit(&mut text, &o.scalar)?;
                    s.emit(&mut text, &nested)?;
                    s.emit(
                        &mut text,
                        &format!(" [merged from R{}C{}]", o.row, o.column),
                    )?;
                }
                char_offset += text[byte_offset..].chars().count();
                byte_offset = text.len();
                a.entries.push(json!({"kind":"cell_span","block":path,"row":r+1,"column":c,"origin_row":o.row,"origin_column":o.column,"char_start":start,"char_end":char_offset,"repeated":o.row!=r+1 || o.column!=c}));
            }
        }
    }
    s.emit(&mut text, "\n[/Table]")?;
    let cells = origins
        .iter()
        .map(|o| Cell {
            row: o.row,
            column: o.column,
            row_span: o.row_span,
            column_span: o.column_span,
            text: o.scalar.clone(),
            header_role: if o.row == 1 {
                HeaderRole::Column
            } else {
                HeaderRole::Unknown
            },
            role_origin: if o.row == 1 {
                Origin::Heuristic
            } else {
                Origin::Source
            },
            regions: vec![region(
                &format!("{path}/R{}C{}", o.row, o.column),
                Some(CellRange {
                    row_start: o.row,
                    row_end: o.row + o.row_span - 1,
                    column_start: o.column,
                    column_end: o.column + o.column_span - 1,
                }),
            )],
            ..Cell::default()
        })
        .collect();
    Ok(TableResult {
        text,
        table: Table {
            rows: rows.len(),
            columns,
            cells,
            caption: None,
        },
    })
}
fn heading(p: &Node, styles: Option<&Node>, s: &Session) -> Result<Option<usize>, CoreError> {
    let props = p.child("pPr");
    if let Some(outline) = props.and_then(|n| n.val("outlineLvl")) {
        let level = number(Some(outline), 9)?;
        return Ok((level < 6).then_some(level + 1));
    }
    let mut style = props.and_then(|n| n.val("pStyle"));
    let mut seen = BTreeSet::new();
    while let (Some(id), Some(styles)) = (style, styles) {
        s.ctx.work(1)?;
        if !seen.insert(id) {
            return Err(CoreError::Parse);
        }
        let Some(node) = styles
            .children_named("style")
            .find(|n| n.attr("styleId") == Some(id))
        else {
            break;
        };
        if let Some(outline) = node.val("outlineLvl") {
            let level = number(Some(outline), 9)?;
            return Ok((level < 6).then_some(level + 1));
        }
        let name = node.val("name").unwrap_or(id).to_ascii_lowercase();
        if let Some(level) = name
            .strip_prefix("heading ")
            .or_else(|| name.strip_prefix("heading"))
            .and_then(|n| n.parse::<usize>().ok())
            .filter(|n| (1..=6).contains(n))
        {
            return Ok(Some(level));
        }
        style = node.val("basedOn");
    }
    Ok(None)
}
#[allow(clippy::too_many_arguments)]
fn add_blocks(
    container: &Node,
    part: &str,
    furniture: bool,
    doc: &mut Document,
    s: &mut Session,
    a: &mut Annotations,
    rels: &BTreeMap<String, Relationship>,
    styles: Option<&Node>,
    numbering: Option<&Node>,
    headings: &mut Vec<String>,
) -> Result<(), CoreError> {
    for n in &container.children {
        s.ctx.work(1)?;
        if !word(n) || matches!(n.name.as_str(), "del" | "moveFrom") {
            continue;
        }
        if !matches!(n.name.as_str(), "p" | "tbl") {
            if matches!(n.name.as_str(), "ins" | "moveTo" | "sdt" | "sdtContent") {
                add_blocks(
                    n, part, furniture, doc, s, a, rels, styles, numbering, headings,
                )?;
            }
            continue;
        }
        let path = format!("{part}/{}[{}]", n.name, doc.blocks.len() + 1);
        let mut block = Block {
            id: path.clone(),
            regions: vec![region(&path, None)],
            ..Block::default()
        };
        if n.name == "tbl" {
            let t = table(n, 1, s, a, &path, rels)?;
            block.kind = BlockKind::Table;
            block.text = t.text;
            block.table = Some(t.table);
        } else {
            inline(n, &mut block.text, s, a, &path, rels)?;
            if let Some(level) = heading(n, styles, s)? {
                block.kind = BlockKind::Heading;
                block.heading_level = Some(level);
                headings.truncate(level - 1);
                headings.push(block.text.clone());
            } else if let Some(props) = n.child("pPr").and_then(|n| n.child("numPr")) {
                block.kind = BlockKind::List;
                let id = props.val("numId");
                let level = number(props.val("ilvl"), 0)?;
                let definition = numbering.and_then(|numbering| {
                    let num = numbering
                        .children_named("num")
                        .find(|n| n.attr("numId") == id)?;
                    let abstract_id = num.val("abstractNumId")?;
                    numbering
                        .children_named("abstractNum")
                        .find(|n| n.attr("abstractNumId") == Some(abstract_id))?
                        .children_named("lvl")
                        .find(|n| {
                            n.attr("ilvl").and_then(|v| v.parse::<usize>().ok()) == Some(level)
                        })
                });
                a.entries.push(json!({"kind":"list","block":path,"num_id":id,"level":level,"format":definition.and_then(|n|n.val("numFmt")),"label_template":definition.and_then(|n|n.val("lvlText")),"origin":"source"}));
            }
            if furniture {
                block.kind = BlockKind::Furniture;
                block.heading_level = None;
            }
        }
        block.heading_path = headings.clone();
        s.reserve(1024 + block.text.len() * 4)?;
        if !doc.blocks.is_empty() {
            s.ctx.output(2)?;
        }
        doc.blocks.push(block);
    }
    Ok(())
}
pub fn extract(bytes: &[u8], limits: impl Into<Limits>) -> Result<Document, CoreError> {
    extract_session(bytes, Session::new(limits.into())?)
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
    if package
        .names
        .iter()
        .any(|n| n.to_ascii_lowercase().contains("vba"))
    {
        return Err(CoreError::Unsupported);
    }
    if package.names.contains("[Content_Types].xml") {
        let types = package.xml("[Content_Types].xml", &mut s)?;
        if types.children.iter().any(|n| {
            n.attr("ContentType")
                .is_some_and(|v| v.to_ascii_lowercase().contains("macroenabled"))
        }) {
            return Err(CoreError::Unsupported);
        }
    }
    let main = package.xml("word/document.xml", &mut s)?;
    if !word(&main) || main.name != "document" {
        return Err(CoreError::Parse);
    }
    let body = main.child("body").ok_or(CoreError::Parse)?;
    let rels = package.relationships("word/document.xml", &mut s)?;
    let styles = if package.names.contains("word/styles.xml") {
        Some(package.xml("word/styles.xml", &mut s)?)
    } else {
        None
    };
    let numbering = if package.names.contains("word/numbering.xml") {
        Some(package.xml("word/numbering.xml", &mut s)?)
    } else {
        None
    };
    let mut doc = Document {
        generation: generation(bytes, "rust-docx", include_str!("docx.rs")),
        ..Document::default()
    };
    let mut annotations = Annotations::default();
    add_blocks(
        body,
        "word/document.xml",
        false,
        &mut doc,
        &mut s,
        &mut annotations,
        &rels,
        styles.as_ref(),
        numbering.as_ref(),
        &mut vec![],
    )?;
    let references = std::mem::take(&mut annotations.references);
    let mut seen = BTreeSet::new();
    for (kind, id, parent) in references {
        if !seen.insert((kind.clone(), id.clone())) {
            continue;
        }
        let part = format!("word/{kind}s.xml");
        let notes = package.xml(&part, &mut s)?;
        let note = notes
            .children_named(&kind)
            .find(|n| n.attr("id") == Some(id.as_str()))
            .ok_or(CoreError::Parse)?;
        let path = format!("{part}/{kind}[{id}]");
        let text = blocks_text(note, 0, &mut s, &mut annotations, &path, &rels)?;
        s.reserve(text.len() * 4 + 1024)?;
        s.ctx.output(2)?;
        doc.blocks.push(Block {
            id: path.clone(),
            kind: BlockKind::Footnote,
            text,
            parent_id: doc
                .blocks
                .iter()
                .find(|b| parent.starts_with(&b.id))
                .map(|b| b.id.clone()),
            regions: vec![region(&path, None)],
            ..Block::default()
        });
    }
    let mut furniture = BTreeSet::new();
    for tag in ["headerReference", "footerReference"] {
        let mut nodes = Vec::new();
        body.descendants(tag, &mut nodes);
        for reference in nodes {
            let relationship = reference
                .attr("id")
                .and_then(|id| rels.get(id))
                .ok_or(CoreError::Parse)?;
            if relationship.external {
                return Err(CoreError::Unsupported);
            }
            if !furniture.insert(relationship.target.clone()) {
                continue;
            }
            let content = package.xml(&relationship.target, &mut s)?;
            let part_rels = package.relationships(&relationship.target, &mut s)?;
            add_blocks(
                &content,
                &relationship.target,
                true,
                &mut doc,
                &mut s,
                &mut annotations,
                &part_rels,
                styles.as_ref(),
                numbering.as_ref(),
                &mut vec![],
            )?;
        }
    }
    if package.names.contains("word/comments.xml") {
        let comments = package.xml("word/comments.xml", &mut s)?;
        for comment in comments.children_named("comment") {
            let text = blocks_text(
                comment,
                0,
                &mut s,
                &mut annotations,
                "word/comments.xml",
                &rels,
            )?;
            annotations.entries.push(json!({"kind":"comment","id":comment.attr("id"),"author":comment.attr("author"),"date":comment.attr("date"),"text":text,"origin":"source"}));
        }
    }
    let stats = s.ctx.stats();
    finish(
        &mut doc,
        &mut s,
        json!({"policy":"accepted_revisions","annotations":annotations.entries,"stats":stats}),
    )?;
    Ok(doc)
}
