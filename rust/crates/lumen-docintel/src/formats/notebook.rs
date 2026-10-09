//! Notebook sources and stored outputs, never execution.
use super::{
    common::{Limits, Session, finish, generation, region},
    json_value,
    text::append_markdown,
};
use crate::{
    CoreError,
    canonical::{Block, BlockKind, Document, Origin},
};
use serde_json::{Value, json};

fn source(value: Option<&Value>) -> Result<String, CoreError> {
    match value {
        None => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(Value::Array(parts)) => parts
            .iter()
            .map(|p| p.as_str().ok_or(CoreError::Parse))
            .collect::<Result<Vec<_>, _>>()
            .map(|p| p.concat()),
        _ => Err(CoreError::Parse),
    }
    .map(|s| s.replace("\r\n", "\n").replace('\r', "\n"))
}
fn output(
    doc: &mut Document,
    s: &mut Session,
    value: &Value,
    cell: usize,
    index: usize,
    diagnostics: &mut Vec<Value>,
) -> Result<(), CoreError> {
    let prefix = format!("cell:{cell};output:{index}");
    let mut emit = |text: String, label: &str, derived: bool| -> Result<(), CoreError> {
        let original = text.len();
        let mut end = original.min(s.limits.max_record_bytes);
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        if end < original {
            diagnostics.push(json!({"cell":cell,"output":index,"field":label,"original_bytes":original,"retained_bytes":end}));
        }
        s.push(
            doc,
            Block {
                text: text[..end].into(),
                regions: vec![region(format!("{prefix};{label}"))],
                origin: if derived {
                    Origin::Derived
                } else {
                    Origin::Source
                },
                ..Block::default()
            },
        )
    };
    match value
        .get("output_type")
        .and_then(Value::as_str)
        .ok_or(CoreError::Parse)?
    {
        "stream" => emit(source(value.get("text"))?, "text", false)?,
        "execute_result" | "display_data" => {
            let data = value
                .get("data")
                .and_then(Value::as_object)
                .ok_or(CoreError::Parse)?;
            for (mime, body) in data {
                if mime == "text/plain" {
                    emit(source(Some(body))?, mime, false)?;
                } else {
                    emit(format!("[stored output: {mime}]"), mime, true)?;
                }
            }
        }
        "error" => emit(source(value.get("traceback"))?, "traceback", false)?,
        _ => return Err(CoreError::Unsupported),
    }
    Ok(())
}
pub fn parse(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    let mut s = Session::new(bytes, limits)?;
    let value = json_value::read(bytes, &mut s)?;
    if value.get("nbformat").and_then(Value::as_u64) != Some(4) {
        return Err(CoreError::Unsupported);
    }
    let cells = value
        .get("cells")
        .and_then(Value::as_array)
        .ok_or(CoreError::Parse)?;
    let language = value
        .pointer("/metadata/language_info/name")
        .or_else(|| value.pointer("/metadata/kernelspec/language"))
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let mut doc = Document {
        generation: generation(
            bytes,
            "rust-notebook",
            concat!(
                include_str!("notebook.rs"),
                include_str!("json_value.rs"),
                include_str!("text.rs")
            ),
        ),
        ..Document::default()
    };
    let mut truncated = vec![];
    for (index, cell) in cells.iter().enumerate() {
        s.ctx.work(1)?;
        let text = source(cell.get("source"))?;
        s.reserve(text.len().saturating_mul(4) + 256)?;
        match cell
            .get("cell_type")
            .and_then(Value::as_str)
            .ok_or(CoreError::Parse)?
        {
            "markdown" => {
                append_markdown(&text, &mut doc, &mut s, &format!("cell:{index};source;"))?
            }
            "code" | "raw" => {
                let code = cell["cell_type"] == "code";
                s.push(
                    &mut doc,
                    Block {
                        kind: if code {
                            BlockKind::Code
                        } else {
                            BlockKind::Paragraph
                        },
                        text,
                        regions: vec![region(format!(
                            "cell:{index};source;language:{}",
                            if code { language } else { "raw" }
                        ))],
                        ..Block::default()
                    },
                )?;
                if let Some(outputs) = cell.get("outputs") {
                    for (o, v) in outputs
                        .as_array()
                        .ok_or(CoreError::Parse)?
                        .iter()
                        .enumerate()
                    {
                        output(&mut doc, &mut s, v, index, o, &mut truncated)?;
                    }
                }
            }
            _ => return Err(CoreError::Unsupported),
        }
    }
    doc.generation
        .dependency_versions
        .insert("serde_json".into(), "1.0.151".into());
    let partial = !truncated.is_empty();
    finish(
        &mut doc,
        &mut s,
        json!({"cell_count":cells.len(),"language":language,"truncated_outputs":truncated}),
        partial,
    )?;
    Ok(doc)
}
