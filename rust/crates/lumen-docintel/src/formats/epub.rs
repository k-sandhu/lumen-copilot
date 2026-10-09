//! EPUB 2/3 spine and TOC, sharing archive/XML/HTML budgets.
use super::{
    common::{Limits, Session, finish, region},
    html::append_html,
    package::{self, Node, Package, resolve_part},
};
use crate::{
    CoreError,
    canonical::{Block, BlockKind, Document, Origin},
};
use serde_json::json;
use std::collections::BTreeMap;
struct Item {
    path: String,
    mime: String,
    properties: String,
}
fn titles(
    node: &Node,
    part: &str,
    out: &mut BTreeMap<String, String>,
    s: &mut Session,
) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    if node.name == "a"
        && let Some(href) = node.attr("href")
    {
        let path = resolve_part(part, href.split('#').next().unwrap_or(""))?;
        s.reserve(node.full_text().len().saturating_mul(4) + path.len().saturating_mul(4) + 512)?;
        out.entry(path)
            .or_insert_with(|| node.full_text().trim().into());
    }
    if node.name == "navPoint"
        && let Some(content) = node.child("content")
        && let Some(src) = content.attr("src")
    {
        let path = resolve_part(part, src.split('#').next().unwrap_or(""))?;
        let title = node
            .child("navLabel")
            .map(Node::full_text)
            .unwrap_or_default();
        s.reserve(title.len().saturating_mul(4) + path.len().saturating_mul(4) + 512)?;
        out.entry(path).or_insert_with(|| title.trim().into());
    }
    for child in &node.children {
        titles(child, part, out, s)?;
    }
    Ok(())
}
pub fn parse(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    // Package::open charges input; both sessions keep the same counters/deadline.
    let mut s = Session::new(&[], limits)?;
    let mut ps = package::Session::with_context(
        package::Limits {
            budget: limits.budget,
            max_xml_depth: limits.max_depth,
            ..package::Limits::default()
        },
        s.ctx.clone(),
    )?;
    let mut zip = Package::open(bytes, &mut ps)?;
    if zip.names.contains("META-INF/encryption.xml") || zip.names.contains("META-INF/rights.xml") {
        return Err(CoreError::Unsupported);
    }
    if zip.bytes("mimetype", &mut ps)? != b"application/epub+zip" {
        return Err(CoreError::Parse);
    }
    let container = zip.xml("META-INF/container.xml", &mut ps)?;
    let root = container.find("rootfile").ok_or(CoreError::Parse)?;
    let opf_path = resolve_part("", root.attr("full-path").ok_or(CoreError::Parse)?)?;
    let opf = zip.xml(&opf_path, &mut ps)?;
    if opf.name != "package" {
        return Err(CoreError::Parse);
    }
    let version = opf.attr("version").ok_or(CoreError::Parse)?;
    if !version.starts_with(['2', '3']) {
        return Err(CoreError::Unsupported);
    }
    let manifest = opf.child("manifest").ok_or(CoreError::Parse)?;
    let spine = opf.child("spine").ok_or(CoreError::Parse)?;
    let mut items = BTreeMap::new();
    for item in manifest.children_named("item") {
        s.ctx.work(1)?;
        let id = item.attr("id").ok_or(CoreError::Parse)?.to_owned();
        let path = resolve_part(&opf_path, item.attr("href").ok_or(CoreError::Parse)?)?;
        if !zip.names.contains(&path) {
            return Err(CoreError::Parse);
        }
        s.reserve(id.len().saturating_mul(4) + path.len().saturating_mul(4) + 1024)?;
        if items
            .insert(
                id,
                Item {
                    path,
                    mime: item.attr("media-type").unwrap_or("").into(),
                    properties: item.attr("properties").unwrap_or("").into(),
                },
            )
            .is_some()
        {
            return Err(CoreError::InvalidInput);
        }
    }
    let toc = if version.starts_with('3') {
        items
            .values()
            .find(|i| i.properties.split_whitespace().any(|p| p == "nav"))
    } else {
        spine.attr("toc").and_then(|id| items.get(id))
    };
    let mut toc_titles = BTreeMap::new();
    if let Some(toc) = toc {
        let nav = zip.xml(&toc.path, &mut ps)?;
        titles(&nav, &toc.path, &mut toc_titles, &mut s)?;
    }
    let build = format!(
        "{}{}{}{}",
        include_str!("epub.rs"),
        include_str!("html.rs"),
        include_str!("html_dom.rs"),
        include_str!("common.rs")
    );
    let mut doc = Document {
        generation: package::generation(bytes, "rust-epub", &build),
        ..Document::default()
    };
    let mut chapters = vec![];
    let mut partial = false;
    for (index, reference) in spine.children_named("itemref").enumerate() {
        s.ctx.work(1)?;
        let number = index + 1;
        let item = items
            .get(reference.attr("idref").ok_or(CoreError::Parse)?)
            .ok_or(CoreError::Parse)?;
        if !matches!(item.mime.as_str(), "application/xhtml+xml" | "text/html") {
            return Err(CoreError::Unsupported);
        }
        let prefix = format!("chapter:{number};{}", item.path);
        let title = toc_titles.get(&item.path).cloned();
        if let Some(title) = &title {
            s.push(
                &mut doc,
                Block {
                    kind: BlockKind::Heading,
                    heading_level: Some(1),
                    text: title.clone(),
                    origin: Origin::Heuristic,
                    regions: vec![region(format!(
                        "{prefix};toc:{}",
                        toc.map(|t| t.path.as_str()).unwrap_or("")
                    ))],
                    ..Block::default()
                },
            )?;
        }
        let chapter = zip.bytes(&item.path, &mut ps)?;
        let start = doc.blocks.len();
        let html = append_html(&chapter, None, &mut doc, &mut s, &prefix)?;
        partial |= html["decoding_errors"].as_u64().unwrap_or(0) > 0;
        let title = title.or_else(|| {
            doc.blocks[start..]
                .iter()
                .find(|b| b.kind == BlockKind::Heading)
                .map(|b| b.text.clone())
        });
        s.reserve(2048)?;
        chapters.push(json!({"index":number,"path":item.path,"title":title,"title_origin":if toc_titles.contains_key(&item.path){"toc"}else{"source_heading_or_unknown"},"source_sha256":package::sha256(&chapter),"html":html}));
    }
    if chapters.is_empty() {
        return Err(CoreError::Parse);
    }
    let metadata: BTreeMap<String, Vec<String>> = opf
        .child("metadata")
        .map(|n| {
            let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
            for child in &n.children {
                out.entry(child.name.clone())
                    .or_default()
                    .push(child.full_text());
            }
            out
        })
        .unwrap_or_default();
    finish(
        &mut doc,
        &mut s,
        json!({"version":version,"package":opf_path,"metadata":metadata,"chapters":chapters}),
        partial,
    )?;
    Ok(doc)
}
