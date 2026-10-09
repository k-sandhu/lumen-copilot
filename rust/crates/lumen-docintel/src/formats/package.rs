//! Bounded in-memory ZIP/XML machinery shared by Office candidates.
use crate::{
    CoreError,
    canonical::{Document, Generation},
    runtime::{Budget, Cancellation, Context, Reservation},
};
use quick_xml::{NsReader, events::Event, name::ResolveResult};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Cursor, Read},
};

#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub budget: Budget,
    pub max_entries: usize,
    pub max_expanded_bytes: usize,
    pub max_part_bytes: usize,
    pub max_ratio: usize,
    pub max_xml_depth: usize,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            budget: Budget::default(),
            max_entries: 2048,
            max_expanded_bytes: 64 * 1024 * 1024,
            max_part_bytes: 16 * 1024 * 1024,
            max_ratio: 200,
            max_xml_depth: 128,
        }
    }
}
impl From<Budget> for Limits {
    fn from(budget: Budget) -> Self {
        Self {
            budget,
            ..Self::default()
        }
    }
}

pub struct Session {
    pub ctx: Context,
    pub limits: Limits,
    reservations: Vec<Reservation>,
}
impl Session {
    pub fn new(limits: Limits) -> Result<Self, CoreError> {
        Self::with_context(
            limits,
            Context::new(limits.budget, Cancellation::default())?,
        )
    }
    pub fn with_context(limits: Limits, ctx: Context) -> Result<Self, CoreError> {
        if limits.max_entries == 0
            || limits.max_part_bytes == 0
            || limits.max_ratio == 0
            || limits.max_xml_depth == 0
            || limits.max_xml_depth > 128
        {
            return Err(CoreError::InvalidInput);
        }
        Ok(Self {
            ctx,
            limits,
            reservations: Vec::new(),
        })
    }
    pub fn reserve(&mut self, bytes: usize) -> Result<(), CoreError> {
        // Include reservation/vector bookkeeping before retaining the allocation.
        self.reservations.push(
            self.ctx
                .reserve(bytes.checked_add(128).ok_or(CoreError::Budget)?)?,
        );
        Ok(())
    }
    pub fn emit(&mut self, output: &mut String, text: &str) -> Result<(), CoreError> {
        self.ctx.work(1)?;
        self.ctx.output(text.chars().count())?;
        self.reserve(text.len().checked_mul(4).ok_or(CoreError::Budget)?)?;
        output.push_str(text);
        Ok(())
    }
}

pub struct Package<'a> {
    archive: zip::ZipArchive<Cursor<&'a [u8]>>,
    pub names: BTreeSet<String>,
}
fn safe_name(name: &str) -> bool {
    !name.is_empty()
        && !name.starts_with('/')
        && !name.contains(['\\', ':', '\0', '%'])
        && name
            .trim_end_matches('/')
            .split('/')
            .all(|p| !p.is_empty() && p != "." && p != "..")
}
impl<'a> Package<'a> {
    pub fn open(bytes: &'a [u8], s: &mut Session) -> Result<Self, CoreError> {
        s.ctx.input(bytes.len())?;
        if bytes.len() > s.limits.budget.max_input_bytes {
            return Err(CoreError::Budget);
        }
        // Inspect classic EOCD BEFORE zip allocates its central-directory index.
        // ZIP64 is outside this bounded candidate profile.
        let eocd = bytes.len().saturating_sub(65557)..bytes.len().saturating_sub(21);
        let end = eocd
            .rev()
            .find(|&n| {
                bytes.get(n..n + 4) == Some(b"PK\x05\x06")
                    && n + 22 + usize::from(u16::from_le_bytes([bytes[n + 20], bytes[n + 21]]))
                        == bytes.len()
            })
            .ok_or(CoreError::Parse)?;
        let entries = usize::from(u16::from_le_bytes([bytes[end + 10], bytes[end + 11]]));
        if entries == 65535 {
            return Err(CoreError::Unsupported);
        }
        if entries > s.limits.max_entries {
            return Err(CoreError::Budget);
        }
        if bytes[end + 4..end + 8] != [0, 0, 0, 0] {
            return Err(CoreError::Unsupported);
        }
        s.reserve(
            bytes
                .len()
                .checked_mul(4)
                .and_then(|n| n.checked_add(entries * 2048))
                .ok_or(CoreError::Budget)?,
        )?;
        let mut archive = zip::ZipArchive::new(Cursor::new(bytes)).map_err(|_| CoreError::Parse)?;
        if archive.len() != entries {
            return Err(CoreError::Parse);
        }
        let mut names = BTreeSet::new();
        let mut expanded = 0usize;
        for i in 0..archive.len() {
            s.ctx.work(1)?;
            let mut file = archive.by_index(i).map_err(|_| CoreError::Parse)?;
            if !safe_name(file.name())
                || !names.insert(file.name().to_owned())
                || file.unix_mode().is_some_and(|m| m & 0o170000 == 0o120000)
            {
                return Err(CoreError::InvalidInput);
            }
            if file.encrypted() {
                return Err(CoreError::Unsupported);
            }
            let size = usize::try_from(file.size()).map_err(|_| CoreError::Budget)?;
            let compressed =
                usize::try_from(file.compressed_size()).map_err(|_| CoreError::Budget)?;
            expanded = expanded.checked_add(size).ok_or(CoreError::Budget)?;
            if size > s.limits.max_part_bytes
                || expanded > s.limits.max_expanded_bytes
                || size > compressed.max(1).saturating_mul(s.limits.max_ratio)
            {
                return Err(CoreError::Budget);
            }
            // Validate CRC/declared lengths of all parts, without retaining media.
            let mut read = 0usize;
            let mut buffer = [0u8; 8192];
            loop {
                s.ctx.work(1)?;
                let n = file.read(&mut buffer).map_err(|_| CoreError::Parse)?;
                if n == 0 {
                    break;
                }
                read = read.checked_add(n).ok_or(CoreError::Budget)?;
                if read > size {
                    return Err(CoreError::Budget);
                }
            }
            if read != size {
                return Err(CoreError::Parse);
            }
        }
        Ok(Self { archive, names })
    }
    pub fn bytes(&mut self, name: &str, s: &mut Session) -> Result<Vec<u8>, CoreError> {
        let mut file = self.archive.by_name(name).map_err(|_| CoreError::Parse)?;
        let size = usize::try_from(file.size()).map_err(|_| CoreError::Budget)?;
        s.reserve(size.checked_mul(2).ok_or(CoreError::Budget)?)?;
        let mut bytes = Vec::with_capacity(size);
        let mut buffer = [0u8; 8192];
        loop {
            s.ctx.work(1)?;
            let n = file.read(&mut buffer).map_err(|_| CoreError::Parse)?;
            if n == 0 {
                break;
            }
            if bytes.len() + n > size {
                return Err(CoreError::Budget);
            }
            bytes.extend_from_slice(&buffer[..n]);
        }
        Ok(bytes)
    }
    pub fn xml(&mut self, name: &str, s: &mut Session) -> Result<Node, CoreError> {
        parse_xml(&self.bytes(name, s)?, s)
    }
    pub fn relationships(
        &mut self,
        part: &str,
        s: &mut Session,
    ) -> Result<BTreeMap<String, Relationship>, CoreError> {
        let (dir, file) = part.rsplit_once('/').unwrap_or(("", part));
        let path = if dir.is_empty() {
            format!("_rels/{file}.rels")
        } else {
            format!("{dir}/_rels/{file}.rels")
        };
        let mut result = BTreeMap::new();
        if !self.names.contains(&path) {
            return Ok(result);
        }
        let xml = self.xml(&path, s)?;
        for n in xml.children_named("Relationship") {
            let id = n.attr("Id").ok_or(CoreError::Parse)?;
            let target = n.attr("Target").ok_or(CoreError::Parse)?;
            let external = n.attr("TargetMode") == Some("External");
            let target = if external {
                target.to_owned()
            } else {
                resolve_part(part, target)?
            };
            if !external && !self.names.contains(&target) {
                return Err(CoreError::Parse);
            }
            if result
                .insert(
                    id.to_owned(),
                    Relationship {
                        target,
                        external,
                        kind: n.attr("Type").unwrap_or("").to_owned(),
                    },
                )
                .is_some()
            {
                return Err(CoreError::InvalidInput);
            }
        }
        Ok(result)
    }
}
pub struct Relationship {
    pub target: String,
    pub external: bool,
    pub kind: String,
}
pub fn resolve_part(source: &str, target: &str) -> Result<String, CoreError> {
    if target.contains(['\\', ':', '\0', '%', '?', '#']) {
        return Err(CoreError::InvalidInput);
    }
    let mut parts: Vec<&str> = if target.starts_with('/') {
        vec![]
    } else {
        source.split('/').collect()
    };
    if !target.starts_with('/') {
        parts.pop();
    }
    for p in target.trim_start_matches('/').split('/') {
        match p {
            ".." => {
                if parts.pop().is_none() {
                    return Err(CoreError::InvalidInput);
                }
            }
            "." => {}
            "" => return Err(CoreError::InvalidInput),
            _ => parts.push(p),
        }
    }
    let path = parts.join("/");
    if !safe_name(&path) {
        return Err(CoreError::InvalidInput);
    }
    Ok(path)
}

#[derive(Debug, Default)]
pub struct Node {
    pub name: String,
    pub ns: String,
    pub attrs: BTreeMap<String, String>,
    pub text: String,
    pub children: Vec<Node>,
    pub content: Vec<Content>,
}
#[derive(Debug)]
pub enum Content {
    Text(String),
    Child(usize),
}
impl Node {
    pub fn attr(&self, name: &str) -> Option<&str> {
        self.attrs
            .get(name)
            .or_else(|| {
                self.attrs
                    .iter()
                    .find(|(k, _)| k.ends_with(&format!("}}{name}")))
                    .map(|(_, v)| v)
            })
            .map(String::as_str)
    }
    pub fn relationship_attr(&self, name: &str) -> Option<&str> {
        for ns in [
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "http://purl.oclc.org/ooxml/officeDocument/relationships",
        ] {
            if let Some(value) = self.attrs.get(&format!("{{{ns}}}{name}")) {
                return Some(value);
            }
        }
        None
    }
    pub fn child(&self, name: &str) -> Option<&Node> {
        self.children.iter().find(|n| n.name == name)
    }
    pub fn children_named<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a Node> {
        self.children.iter().filter(move |n| n.name == name)
    }
    pub fn descendants<'a>(&'a self, name: &str, out: &mut Vec<&'a Node>) {
        for n in &self.children {
            if n.name == name {
                out.push(n);
            }
            n.descendants(name, out);
        }
    }
    pub fn find(&self, name: &str) -> Option<&Node> {
        for n in &self.children {
            if n.name == name {
                return Some(n);
            }
            if let Some(found) = n.find(name) {
                return Some(found);
            }
        }
        None
    }
    pub fn val(&self, name: &str) -> Option<&str> {
        self.find(name).and_then(|n| n.attr("val"))
    }
    pub fn full_text(&self) -> String {
        let mut text = String::new();
        for content in &self.content {
            match content {
                Content::Text(s) => text.push_str(s),
                Content::Child(i) => text.push_str(&self.children[*i].full_text()),
            }
        }
        text
    }
}
pub fn parse_xml(bytes: &[u8], s: &mut Session) -> Result<Node, CoreError> {
    let utf8 = std::str::from_utf8(bytes).map_err(|_| CoreError::Unsupported)?;
    let mut reader = NsReader::from_str(utf8);
    reader.config_mut().check_end_names = true;
    let mut stack: Vec<Node> = vec![];
    let mut root = None;
    loop {
        s.ctx.work(1)?;
        let (ns, event) = reader.read_resolved_event().map_err(|_| CoreError::Parse)?;
        match event {
            Event::Start(ref e) | Event::Empty(ref e) => {
                if stack.len() >= s.limits.max_xml_depth {
                    return Err(CoreError::Budget);
                }
                s.reserve(512 + e.len() * 4)?;
                let namespace = match ns {
                    ResolveResult::Bound(n) => n.as_ref().to_owned(),
                    ResolveResult::Unbound => String::new(),
                    _ => return Err(CoreError::Parse),
                };
                let mut node = Node {
                    name: e.local_name().as_ref().to_owned(),
                    ns: namespace,
                    ..Node::default()
                };
                for a in e.attributes() {
                    let a = a.map_err(|_| CoreError::Parse)?;
                    if a.key.as_ref() == "xmlns" || a.key.as_ref().starts_with("xmlns:") {
                        continue;
                    }
                    let (namespace, local) = reader.resolver().resolve_attribute(a.key);
                    let key = match namespace {
                        ResolveResult::Bound(ns) => {
                            format!("{{{}}}{}", ns.as_ref(), local.as_ref())
                        }
                        ResolveResult::Unbound => local.as_ref().to_owned(),
                        _ => return Err(CoreError::Parse),
                    };
                    let value = a
                        .normalized_value(quick_xml::XmlVersion::Implicit1_0)
                        .map_err(|_| CoreError::Parse)?
                        .into_owned();
                    // xmlns declarations are not data attributes.
                    if a.key.as_ref().starts_with("xmlns") {
                        continue;
                    }
                    if node.attrs.insert(key, value).is_some() {
                        return Err(CoreError::InvalidInput);
                    }
                }
                if matches!(event, Event::Start(_)) {
                    stack.push(node);
                } else if let Some(parent) = stack.last_mut() {
                    parent.content.push(Content::Child(parent.children.len()));
                    parent.children.push(node);
                } else if root.replace(node).is_some() {
                    return Err(CoreError::Parse);
                }
            }
            Event::End(_) => {
                let node = stack.pop().ok_or(CoreError::Parse)?;
                if let Some(parent) = stack.last_mut() {
                    parent.content.push(Content::Child(parent.children.len()));
                    parent.children.push(node);
                } else if root.replace(node).is_some() {
                    return Err(CoreError::Parse);
                }
            }
            Event::Text(e) => {
                let text = e.xml10_content();
                if let Some(parent) = stack.last_mut() {
                    s.reserve(text.len() * 8 + 128)?;
                    parent.text.push_str(&text);
                    parent.content.push(Content::Text(text.into_owned()));
                } else if !text.trim().is_empty() {
                    return Err(CoreError::Parse);
                }
            }
            Event::CData(e) => {
                let text = e.xml10_content();
                let parent = stack.last_mut().ok_or(CoreError::Parse)?;
                s.reserve(text.len() * 8 + 128)?;
                parent.text.push_str(&text);
                parent.content.push(Content::Text(text.into_owned()));
            }
            Event::GeneralRef(e) => {
                let reference = e.xml10_content();
                let escaped = format!("&{reference};");
                let text = quick_xml::escape::unescape(&escaped).map_err(|_| CoreError::Parse)?;
                s.reserve(text.len() * 8 + 128)?;
                let parent = stack.last_mut().ok_or(CoreError::Parse)?;
                parent.text.push_str(&text);
                parent.content.push(Content::Text(text.into_owned()));
            }
            Event::DocType(_) => return Err(CoreError::Unsupported),
            Event::Decl(e) => {
                if let Some(encoding) = e.encoding() {
                    let encoding = encoding.map_err(|_| CoreError::Parse)?;
                    if !encoding.eq_ignore_ascii_case("utf-8")
                        && !encoding.eq_ignore_ascii_case("us-ascii")
                    {
                        return Err(CoreError::Unsupported);
                    }
                }
            }
            Event::Eof => break,
            _ => {}
        }
    }
    if !stack.is_empty() {
        return Err(CoreError::Parse);
    }
    root.ok_or(CoreError::Parse)
}

pub fn generation(bytes: &[u8], parser: &str, source: &str) -> Generation {
    // Include every shared computation layer and the pinned dependency graph.
    // A package-reader or renderer repair must invalidate previous generations.
    let mut build = Sha256::new();
    for component in [
        source,
        include_str!("package.rs"),
        include_str!("../canonical.rs"),
        include_str!("../runtime.rs"),
        include_str!("../../../../Cargo.lock"),
    ] {
        build.update((component.len() as u64).to_le_bytes());
        build.update(component.as_bytes());
    }
    Generation {
        source_sha256: Some(sha256(bytes)),
        parser_id: Some(parser.to_owned()),
        parser_version: Some("1".to_owned()),
        build_id: Some(
            build
                .finalize()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect(),
        ),
        dependency_versions: [
            ("zip".to_owned(), "8.6.0".to_owned()),
            ("quick-xml".to_owned(), "0.42.0".to_owned()),
            ("sha2".to_owned(), "0.10.9".to_owned()),
        ]
        .into(),
        ..Generation::default()
    }
}
pub fn finish(
    doc: &mut Document,
    s: &mut Session,
    diagnostics: serde_json::Value,
) -> Result<(), CoreError> {
    s.ctx.checkpoint()?;
    doc.generation.outcome = Some(
        if doc.blocks.iter().all(|b| b.text.trim().is_empty()) {
            "empty"
        } else {
            "success"
        }
        .to_owned(),
    );
    doc.generation.diagnostics = Some(diagnostics);
    Ok(())
}
pub fn sha256(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
