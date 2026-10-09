//! Streaming delimited records; source values are always inert strings.
use super::common::{Limits, Session, decoded, finish, generation, region};
use crate::{
    CoreError,
    canonical::{Block, BlockKind, Cell, CellRange, Document, HeaderRole, Origin, Table},
};
use serde_json::json;

fn dialect(text: &str) -> u8 {
    b",;\t|"
        .iter()
        .copied()
        .max_by_key(|&d| {
            let mut r = csv::ReaderBuilder::new()
                .delimiter(d)
                .has_headers(false)
                .flexible(true)
                .from_reader(text.as_bytes());
            let lengths: Vec<usize> = r
                .records()
                .take(20)
                .filter_map(Result::ok)
                .map(|r| r.len())
                .collect();
            let first = lengths.first().copied().unwrap_or(0);
            (
                if first > 1 {
                    lengths.iter().filter(|&&n| n == first).count()
                } else {
                    0
                },
                first,
            )
        })
        .unwrap_or(b',')
}
fn kind(value: &str) -> &'static str {
    if value.is_empty() {
        "empty"
    } else if value.parse::<i64>().is_ok() {
        "integer"
    } else if value.parse::<f64>().is_ok_and(f64::is_finite) {
        "number"
    } else if matches!(value.to_ascii_lowercase().as_str(), "true" | "false") {
        "boolean"
    } else {
        "text"
    }
}
pub fn parse(bytes: &[u8], limits: Limits) -> Result<Document, CoreError> {
    let mut s = Session::new(bytes, limits)?;
    let decoded = decoded(bytes, &mut s)?;
    let delimiter = dialect(&decoded.text);
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(delimiter)
        .has_headers(false)
        .flexible(true)
        .from_reader(decoded.text.as_bytes());
    let mut record = csv::StringRecord::new();
    let mut table = Table::default();
    let mut text = String::new();
    let mut headers: Vec<String> = vec![];
    let mut types: Vec<&str> = vec![];
    let mut ragged = vec![];
    let mut has_header = false;
    let mut chars = 0;
    while {
        s.ctx.work(1)?;
        // A record cannot exceed the whole bounded source; reserve before csv grows its buffer.
        if table.rows == 0 {
            s.reserve(decoded.text.len().saturating_mul(4) + 8192)?;
        }
        reader
            .read_record(&mut record)
            .map_err(|_| CoreError::Parse)?
    } {
        if record.as_slice().len() > limits.max_record_bytes {
            return Err(CoreError::Budget);
        }
        table.rows += 1;
        let row = table.rows;
        if row == 1 {
            table.columns = record.len();
            has_header = record.iter().all(|v| !v.is_empty() && kind(v) == "text")
                && record
                    .iter()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
                    == record.len();
            headers = record
                .iter()
                .enumerate()
                .map(|(n, v)| {
                    if has_header {
                        v.to_owned()
                    } else {
                        format!("C{}", n + 1)
                    }
                })
                .collect();
            types = vec!["empty"; record.len()];
        } else if record.len() != headers.len() {
            ragged.push(row);
        }
        table.columns = table.columns.max(record.len());
        s.reserve(record.as_slice().len().saturating_mul(8) + record.len().saturating_mul(1536))?;
        let line = record.position().map(|p| p.line()).unwrap_or(1);
        let start = text.len();
        if row > 1 {
            text.push('\n');
        }
        text.push_str(&format!("Row {row}: "));
        for (column, value) in record.iter().enumerate() {
            s.ctx.work(1)?;
            if column > 0 {
                text.push_str(" | ");
            }
            let label = headers
                .get(column)
                .cloned()
                .unwrap_or_else(|| format!("C{}", column + 1));
            s.reserve(label.len().saturating_mul(8) + 128)?;
            text.push_str(&format!("C{} [{label}]={value}", column + 1));
            if !has_header || row > 1 {
                if types.len() <= column {
                    types.push("empty");
                }
                let k = kind(value);
                types[column] = match (types[column], k) {
                    ("empty", k) => k,
                    (a, "empty") => a,
                    (a, b) if a == b => a,
                    ("integer", "number") | ("number", "integer") => "number",
                    _ => "text",
                };
            }
            table.cells.push(Cell {
                row,
                column: column + 1,
                row_span: 1,
                column_span: 1,
                text: value.into(),
                header_role: if has_header && row == 1 {
                    HeaderRole::Column
                } else {
                    HeaderRole::Unknown
                },
                role_origin: Origin::Heuristic,
                regions: vec![crate::canonical::SourceRegion {
                    cell_range: Some(CellRange {
                        row_start: row,
                        row_end: row,
                        column_start: column + 1,
                        column_end: column + 1,
                    }),
                    ..region(format!("record:{row};line:{line}"))
                }],
                ..Cell::default()
            });
        }
        // Charge while accumulating, not only when the completed table is emitted.
        chars += text[start..].chars().count();
        if chars > limits.budget.max_output_chars {
            return Err(CoreError::Budget);
        }
    }
    let partial = !ragged.is_empty() || decoded.errors > 0;
    let mut doc = Document {
        generation: generation(bytes, "rust-csv", include_str!("csv.rs")),
        ..Document::default()
    };
    doc.generation
        .dependency_versions
        .insert("csv".into(), "1.4.0".into());
    if table.rows > 0 {
        s.push(
            &mut doc,
            Block {
                kind: BlockKind::Table,
                text,
                table: Some(table),
                regions: vec![region("delimited-records")],
                ..Block::default()
            },
        )?;
    }
    finish(
        &mut doc,
        &mut s,
        json!({"delimiter":(delimiter as char).to_string(),"header_origin":"heuristic","has_header":has_header,"column_types":types,"ragged_rows":ragged,"encoding":decoded.encoding,"decoding_errors":decoded.errors}),
        partial,
    )?;
    Ok(doc)
}
