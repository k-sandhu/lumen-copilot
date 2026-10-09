//! Namespace-aware streaming XML and tagged-fact capture without entity loading.
use super::common::{Limits, Session, finish, generation, region};
use crate::{
    CoreError,
    canonical::{Block, Document},
};
use quick_xml::{
    NsReader,
    events::Event,
    name::{QName, ResolveResult},
};
use serde_json::{Value, json};
use std::collections::BTreeMap;

const INSTANCE: &str = "http://www.xbrl.org/2003/instance";
const INLINE: &str = "http://www.xbrl.org/2013/inlineXBRL";
const INLINE_OLD: &str = "http://www.xbrl.org/2008/inlineXBRL";
#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Mode {
    Xml,
    Xbrl,
    Inline,
}
#[derive(Default)]
struct Frame {
    name: String,
    ns: String,
    path: String,
    line: usize,
    start: usize,
    attrs: BTreeMap<String, String>,
    children: BTreeMap<String, usize>,
    text: String,
    concept: Option<String>,
    context: bool,
    unit: bool,
    fields: BTreeMap<String, String>,
    skip: bool,
}
fn namespace(result: ResolveResult<'_>) -> Result<String, CoreError> {
    match result {
        ResolveResult::Bound(n) => Ok(n.as_ref().to_owned()),
        ResolveResult::Unbound => Ok(String::new()),
        _ => Err(CoreError::Parse),
    }
}
fn expanded(ns: &str, name: &str) -> String {
    if ns.is_empty() {
        name.into()
    } else {
        format!("{{{ns}}}{name}")
    }
}
fn emit(doc: &mut Document, s: &mut Session, frame: &Frame, text: &str) -> Result<(), CoreError> {
    s.reserve((frame.path.len() + text.len()).saturating_mul(4) + 256)?;
    s.push(
        doc,
        Block {
            text: format!("{} = {text}", frame.path),
            regions: vec![region(format!("{};line:{}", frame.path, frame.line))],
            ..Block::default()
        },
    )
}
fn text(s: &mut Session, stack: &mut [Frame], value: &str) -> Result<(), CoreError> {
    let Some(last) = stack.last() else {
        return if value.trim().is_empty() {
            Ok(())
        } else {
            Err(CoreError::Parse)
        };
    };
    if last.skip {
        return Ok(());
    }
    let name = if last.name == "measure" && last.ns == INSTANCE {
        format!("measure:{}", last.path)
    } else {
        expanded(&last.ns, &last.name)
    };
    let atomic = stack.iter().rposition(|f| f.concept.is_some());
    if let Some(i) = atomic {
        s.reserve(value.len().saturating_mul(4) + 128)?;
        stack[i].text.push_str(value);
        if stack[i].text.len() > s.limits.budget.max_output_chars.saturating_mul(4) {
            return Err(CoreError::Budget);
        }
    } else {
        s.reserve(value.len().saturating_mul(4) + 128)?;
        let last = stack.last_mut().ok_or(CoreError::Parse)?;
        last.text.push_str(value);
        if last.text.len() > s.limits.budget.max_output_chars.saturating_mul(4) {
            return Err(CoreError::Budget);
        }
    }
    if let Some(i) = stack.iter().rposition(|f| f.context || f.unit) {
        s.reserve(name.len() + value.len().saturating_mul(4) + 256)?;
        stack[i].fields.entry(name).or_default().push_str(value);
    }
    Ok(())
}
fn close(
    frame: Frame,
    end: usize,
    doc: &mut Document,
    s: &mut Session,
    facts: &mut Vec<Value>,
    contexts: &mut BTreeMap<String, Value>,
    units: &mut BTreeMap<String, Value>,
) -> Result<(), CoreError> {
    if frame.skip {
        return Ok(());
    }
    if frame.concept.is_none() && !frame.text.trim().is_empty() {
        emit(doc, s, &frame, &frame.text)?;
    }
    if let Some(concept) = &frame.concept {
        let attrs = frame
            .attrs
            .iter()
            .map(|(k, v)| format!("{k}: {v}"))
            .collect::<Vec<_>>()
            .join("; ");
        let prefix = format!("{concept}; {attrs}; value: ");
        let start = prefix.chars().count();
        s.reserve((prefix.len() + frame.text.len()).saturating_mul(8) + 2048)?;
        let id = format!("b{}", doc.blocks.len() + 1);
        s.push(
            doc,
            Block {
                text: format!("{prefix}{}", frame.text),
                regions: vec![region(format!("{};line:{}", frame.path, frame.line))],
                ..Block::default()
            },
        )?;
        facts.push(json!({"concept":concept,"value":frame.text,"attributes":frame.attrs,"context_ref":frame.attrs.get("contextRef"),"unit_ref":frame.attrs.get("unitRef"),"context":null,"unit":null,"period":null,"path":frame.path,"line":frame.line,"byte_start":frame.start,"byte_end":end,"block_id":id,"value_start":start,"value_end":start+frame.text.chars().count()}));
    }
    if frame.context {
        let id = frame.attrs.get("id").ok_or(CoreError::Parse)?;
        let period = json!({"instant":frame.fields.get(&expanded(INSTANCE,"instant")).map(|s|s.trim()),"start":frame.fields.get(&expanded(INSTANCE,"startDate")).map(|s|s.trim()),"end":frame.fields.get(&expanded(INSTANCE,"endDate")).map(|s|s.trim()),"forever":frame.fields.contains_key("forever")});
        let value = json!({"id":id,"fields":frame.fields,"period":period,"path":frame.path,"line":frame.line});
        if contexts.insert(id.clone(), value).is_some() {
            return Err(CoreError::InvalidInput);
        }
    }
    if frame.unit {
        let id = frame.attrs.get("id").ok_or(CoreError::Parse)?;
        let value = json!(
            frame
                .fields
                .iter()
                .filter(|(k, _)| k.starts_with("measure:"))
                .map(|(_, v)| v.trim())
                .collect::<Vec<_>>()
        );
        if units.insert(id.clone(), value).is_some() {
            return Err(CoreError::InvalidInput);
        }
    }
    Ok(())
}
fn value_bytes(value: &Value) -> Result<usize, CoreError> {
    let mut total = 256usize;
    match value {
        Value::String(s) => total = total.checked_add(s.len()).ok_or(CoreError::Budget)?,
        Value::Array(a) => {
            for v in a {
                total = total
                    .checked_add(value_bytes(v)?)
                    .ok_or(CoreError::Budget)?;
            }
        }
        Value::Object(o) => {
            for (k, v) in o {
                total = total
                    .checked_add(k.len())
                    .and_then(|t| t.checked_add(value_bytes(v).ok()?))
                    .ok_or(CoreError::Budget)?;
            }
        }
        _ => {}
    }
    Ok(total)
}
pub(crate) fn parse_mode(bytes: &[u8], limits: Limits, mode: Mode) -> Result<Document, CoreError> {
    let mut s = Session::new(bytes, limits)?;
    let source = std::str::from_utf8(bytes).map_err(|_| CoreError::Unsupported)?;
    let mut reader = NsReader::from_str(source);
    reader.config_mut().check_end_names = true;
    let mut doc = Document {
        generation: generation(
            bytes,
            match mode {
                Mode::Xml => "rust-xml",
                Mode::Xbrl => "rust-xbrl",
                Mode::Inline => "rust-ixbrl",
            },
            concat!(
                include_str!("xml.rs"),
                include_str!("xbrl.rs"),
                include_str!("ixbrl.rs")
            ),
        ),
        ..Document::default()
    };
    let mut stack: Vec<Frame> = vec![];
    let mut root_count = 0;
    let mut root_is_instance = false;
    let mut cursor = 0;
    let mut line = 1;
    let mut facts = vec![];
    let mut contexts = BTreeMap::new();
    let mut units = BTreeMap::new();
    let mut elements = vec![];
    let mut incomplete = vec![];
    loop {
        s.ctx.work(1)?;
        let start = reader.buffer_position() as usize;
        line += bytes[cursor..start].iter().filter(|b| **b == b'\n').count();
        cursor = start;
        let (ns, event) = reader.read_resolved_event().map_err(|_| CoreError::Parse)?;
        let ns = namespace(ns)?;
        let end = reader.buffer_position() as usize;
        match event {
            Event::Start(ref e) | Event::Empty(ref e) => {
                if !stack.iter().any(|f| f.concept.is_some())
                    && let Some(parent) = stack.last_mut()
                {
                    if !parent.skip && !parent.text.trim().is_empty() {
                        let pending = std::mem::take(&mut parent.text);
                        emit(&mut doc, &mut s, parent, &pending)?;
                    } else {
                        parent.text.clear();
                    }
                }
                if stack.len() >= limits.max_depth {
                    return Err(CoreError::Budget);
                }
                s.reserve(e.len().saturating_mul(8) + 2048)?;
                let name = e.local_name().as_ref().to_owned();
                let key = expanded(&ns, &name);
                let (parent, index, skip) = if let Some(p) = stack.last_mut() {
                    let n = p.children.entry(key.clone()).or_default();
                    *n += 1;
                    (p.path.clone(), *n, p.skip)
                } else {
                    root_count += 1;
                    root_is_instance = ns == INSTANCE && name == "xbrl";
                    if mode == Mode::Xbrl && !root_is_instance {
                        return Err(CoreError::Unsupported);
                    }
                    if root_count > 1 {
                        return Err(CoreError::Parse);
                    }
                    (String::new(), 1, false)
                };
                let path = format!("{parent}/{key}[{index}]");
                s.reserve(path.len().saturating_mul(8) + 1024)?;
                let mut attrs = BTreeMap::new();
                for a in e.attributes() {
                    let a = a.map_err(|_| CoreError::Parse)?;
                    if a.key.as_ref() == "xmlns" || a.key.as_ref().starts_with("xmlns:") {
                        continue;
                    }
                    let (ns, local) = reader.resolver().resolve_attribute(a.key);
                    let key = expanded(&namespace(ns)?, local.as_ref());
                    let value = a
                        .normalized_value(quick_xml::XmlVersion::Implicit1_0)
                        .map_err(|_| CoreError::Parse)?
                        .into_owned();
                    s.reserve((key.len() + value.len()).saturating_mul(8) + 256)?;
                    if attrs.insert(key, value).is_some() {
                        return Err(CoreError::InvalidInput);
                    }
                }
                let inline = (ns == INLINE || ns == INLINE_OLD)
                    && matches!(name.as_str(), "nonFraction" | "nonNumeric" | "fraction");
                let concept = if inline {
                    let raw = attrs.get("name").ok_or(CoreError::Parse)?;
                    let (ns, local) = reader.resolver().resolve_element(QName(raw));
                    Some(expanded(&namespace(ns)?, local.as_ref()))
                } else if root_is_instance
                    && mode != Mode::Inline
                    && attrs.contains_key("contextRef")
                {
                    Some(key.clone())
                } else {
                    None
                };
                if inline
                    && (attrs.contains_key("format")
                        || attrs.contains_key("continuedAt")
                        || name == "fraction")
                {
                    incomplete.push(json!({"path":path,"reason":"transformation_or_continuation_requires_separate_resolution"}));
                }
                if ns == INSTANCE
                    && name == "forever"
                    && let Some(c) = stack.iter_mut().rfind(|f| f.context)
                {
                    c.fields.insert("forever".into(), "true".into());
                }
                if concept.is_some() && stack.iter().any(|f| f.concept.is_some()) {
                    return Err(CoreError::Unsupported);
                }
                let skip =
                    skip || (mode == Mode::Inline && matches!(name.as_str(), "script" | "style"));
                elements.push(json!({"path":path,"namespace":ns,"attributes":attrs,"line":line,"byte_start":start}));
                let frame = Frame {
                    name,
                    ns,
                    path,
                    line,
                    start,
                    attrs,
                    children: BTreeMap::new(),
                    text: String::new(),
                    concept,
                    context: false,
                    unit: false,
                    fields: BTreeMap::new(),
                    skip,
                };
                let mut frame = frame;
                frame.context = frame.ns == INSTANCE && frame.name == "context";
                frame.unit = frame.ns == INSTANCE && frame.name == "unit";
                if matches!(event, Event::Empty(_)) {
                    if !frame.skip && frame.concept.is_none() {
                        emit(&mut doc, &mut s, &frame, "")?;
                    }
                    close(
                        frame,
                        end,
                        &mut doc,
                        &mut s,
                        &mut facts,
                        &mut contexts,
                        &mut units,
                    )?;
                } else {
                    stack.push(frame);
                }
            }
            Event::End(_) => close(
                stack.pop().ok_or(CoreError::Parse)?,
                end,
                &mut doc,
                &mut s,
                &mut facts,
                &mut contexts,
                &mut units,
            )?,
            Event::Text(e) => text(&mut s, &mut stack, &e.xml10_content())?,
            Event::CData(e) => text(&mut s, &mut stack, &e.xml10_content())?,
            Event::GeneralRef(e) => {
                let reference = e.xml10_content();
                let escaped = format!("&{reference};");
                let value = quick_xml::escape::unescape(&escaped).map_err(|_| CoreError::Parse)?;
                text(&mut s, &mut stack, &value)?;
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
    if !stack.is_empty() || root_count != 1 {
        return Err(CoreError::Parse);
    }
    if mode != Mode::Xml && facts.is_empty() {
        return Err(CoreError::Unsupported);
    }
    for fact in &mut facts {
        s.ctx.work(1)?;
        if let Some(c) = fact["context_ref"].as_str().and_then(|id| contexts.get(id)) {
            s.reserve(value_bytes(c)?.saturating_mul(16))?;
            fact["context"] = c.clone();
            fact["period"] = c["period"].clone();
        }
        if let Some(u) = fact["unit_ref"].as_str().and_then(|id| units.get(id)) {
            s.reserve(value_bytes(u)?.saturating_mul(16))?;
            fact["unit"] = u.clone();
        }
    }
    doc.generation
        .dependency_versions
        .insert("quick-xml".into(), "0.42.0".into());
    let partial = !incomplete.is_empty();
    finish(
        &mut doc,
        &mut s,
        json!({"facts":facts,"contexts":contexts,"units":units,"elements":elements,"incomplete":incomplete,"source_encoding":"UTF-8","value_offsets":"block-local Unicode code points"}),
        partial,
    )?;
    Ok(doc)
}
pub fn parse(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    parse_mode(bytes, limits, Mode::Xml)
}
