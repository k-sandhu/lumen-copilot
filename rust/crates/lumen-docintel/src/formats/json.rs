//! JSON Pointer rendering and typed record access; no schema inference.
use super::{
    common::{Limits, Session, finish, generation, region},
    json_value,
};
use crate::{
    CoreError,
    canonical::{Block, Document},
};
use serde_json::{Value, json};

fn leaves(
    value: &Value,
    path: &str,
    text: &mut String,
    fields: &mut Vec<Value>,
    chars: &mut usize,
    s: &mut Session,
) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    match value {
        Value::Object(map) if !map.is_empty() => {
            for (key, v) in map {
                s.reserve(path.len() + key.len().saturating_mul(4) + 128)?;
                let key = key.replace('~', "~0").replace('/', "~1");
                leaves(v, &format!("{path}/{key}"), text, fields, chars, s)?;
            }
        }
        Value::Array(values) if !values.is_empty() => {
            for (i, v) in values.iter().enumerate() {
                leaves(v, &format!("{path}/{i}"), text, fields, chars, s)?;
            }
        }
        _ => {
            let value_text = match value {
                Value::String(v) => v.clone(),
                v => v.to_string(),
            };
            let separator = if fields.is_empty() { "" } else { " ; " };
            let added =
                separator.chars().count() + path.chars().count() + 3 + value_text.chars().count();
            *chars = chars.checked_add(added).ok_or(CoreError::Budget)?;
            if *chars
                > s.limits
                    .budget
                    .max_output_chars
                    .saturating_sub(s.ctx.stats().output_chars)
            {
                return Err(CoreError::Budget);
            }
            s.reserve((path.len() + value_text.len()).saturating_mul(12) + 1024)?;
            text.push_str(separator);
            text.push_str(path);
            text.push_str(" = ");
            let start = *chars - value_text.chars().count();
            text.push_str(&value_text);
            fields.push(json!({"path":path,"value":value,"value_start":start,"value_end":*chars}));
        }
    }
    Ok(())
}
pub(crate) fn record(
    bytes: &[u8],
    record: usize,
    line: usize,
    doc: &mut Document,
    s: &mut Session,
) -> Result<Value, CoreError> {
    if bytes.len() > s.limits.max_record_bytes {
        return Err(CoreError::Budget);
    }
    let value = json_value::read(bytes, s)?;
    let mut text = String::new();
    let mut fields = vec![];
    let mut chars = 0;
    leaves(&value, "", &mut text, &mut fields, &mut chars, s)?;
    let block_id = format!("b{}", doc.blocks.len() + 1);
    s.push(
        doc,
        Block {
            text,
            regions: vec![region(format!("record:{record};line:{line}"))],
            ..Block::default()
        },
    )?;
    Ok(json!({"record":record,"line":line,"block_id":block_id,"fields":fields}))
}
pub(crate) fn parse_mode(bytes: &[u8], limits: Limits, lines: bool) -> Result<Document, CoreError> {
    let mut s = Session::new(bytes, limits)?;
    let mut doc = Document {
        generation: generation(
            bytes,
            if lines { "rust-jsonl" } else { "rust-json" },
            concat!(
                include_str!("json.rs"),
                include_str!("jsonl.rs"),
                include_str!("json_value.rs")
            ),
        ),
        ..Document::default()
    };
    let mut records = vec![];
    if lines {
        for (line, bytes) in bytes.split(|b| *b == b'\n').enumerate() {
            s.ctx.work(1)?;
            if bytes.iter().all(u8::is_ascii_whitespace) {
                continue;
            }
            records.push(record(
                bytes,
                records.len() + 1,
                line + 1,
                &mut doc,
                &mut s,
            )?);
        }
    } else {
        records.push(record(bytes, 1, 1, &mut doc, &mut s)?);
    }
    doc.generation
        .dependency_versions
        .insert("serde_json".into(), "1.0.151".into());
    finish(
        &mut doc,
        &mut s,
        json!({"records":records,"path_encoding":"JSON Pointer","value_offsets":"block-local Unicode code points"}),
        false,
    )?;
    Ok(doc)
}
pub fn parse(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    parse_mode(bytes, limits, false)
}
