//! Inert workbook values and formula caches; no formula evaluation or I/O.
use super::package::{Limits, Node, Package, Session, finish, generation};
use crate::{
    CoreError,
    canonical::{
        Block, BlockKind, Cell, CellRange, Document, HeaderRole, Origin, PartKind, SourcePart,
        SourceRegion, Table,
    },
    runtime::Context,
};
use calamine::{Data, Reader, Xls, Xlsx};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{Cursor, Read},
};

#[derive(Clone, Copy, Debug)]
pub struct WorkbookLimits {
    pub package: Limits,
    pub max_rows: usize,
    pub max_cells: usize,
}
impl Default for WorkbookLimits {
    fn default() -> Self {
        Self {
            package: Limits::default(),
            max_rows: 100_000,
            max_cells: 100_000,
        }
    }
}
impl From<Limits> for WorkbookLimits {
    fn from(package: Limits) -> Self {
        Self {
            package,
            ..Self::default()
        }
    }
}
#[derive(Default)]
struct Sheet {
    name: String,
    number: usize,
    rows: usize,
    columns: usize,
    cells: BTreeMap<(usize, usize), Cell>,
    merges: Vec<CellRange>,
    hidden: bool,
    hidden_rows: Vec<usize>,
    partial: bool,
}
pub fn coordinate(row: usize, column: usize) -> String {
    let mut n = column;
    let mut letters = Vec::new();
    while n > 0 {
        n -= 1;
        letters.push((b'A' + (n % 26) as u8) as char);
        n /= 26;
    }
    letters.reverse();
    format!("{}{row}", letters.into_iter().collect::<String>())
}
fn position(text: &str) -> Result<(usize, usize), CoreError> {
    let mut column = 0usize;
    let mut split = 0;
    for c in text.bytes() {
        if c.is_ascii_alphabetic() {
            column = column
                .checked_mul(26)
                .and_then(|v| v.checked_add(usize::from(c.to_ascii_uppercase() - b'A' + 1)))
                .ok_or(CoreError::Parse)?;
            split += 1;
        } else {
            break;
        }
    }
    let row = text
        .get(split..)
        .ok_or(CoreError::Parse)?
        .parse::<usize>()
        .map_err(|_| CoreError::Parse)?;
    if row == 0 || column == 0 || row > 1_048_576 || column > 16_384 {
        return Err(CoreError::Parse);
    }
    Ok((row, column))
}
fn range(text: &str) -> Result<CellRange, CoreError> {
    let (first, last) = text.split_once(':').unwrap_or((text, text));
    let (r, c) = position(first)?;
    let (re, ce) = position(last)?;
    if re < r || ce < c {
        return Err(CoreError::Parse);
    }
    Ok(CellRange {
        row_start: r,
        row_end: re,
        column_start: c,
        column_end: ce,
    })
}
fn covered(sheet: &Sheet, row: usize, column: usize) -> bool {
    sheet.merges.iter().any(|m| {
        row >= m.row_start
            && row <= m.row_end
            && column >= m.column_start
            && column <= m.column_end
            && (row, column) != (m.row_start, m.column_start)
    })
}
fn region(
    sheet: &Sheet,
    row: usize,
    column: usize,
    row_span: usize,
    column_span: usize,
) -> SourceRegion {
    SourceRegion {
        kind: Some(PartKind::Sheet),
        number: Some(sheet.number),
        name: Some(sheet.name.clone()),
        cell_range: Some(CellRange {
            row_start: row,
            row_end: row + row_span - 1,
            column_start: column,
            column_end: column + column_span - 1,
        }),
        ..SourceRegion::default()
    }
}
fn value(data: Data) -> Result<(String, Option<Value>), CoreError> {
    Ok(match data {
        Data::Empty => (String::new(), None),
        Data::String(s) | Data::DateTimeIso(s) | Data::DurationIso(s) => {
            (s.clone(), Some(json!(s)))
        }
        Data::Bool(b) => (if b { "True" } else { "False" }.to_owned(), Some(json!(b))),
        Data::Int(n) => (n.to_string(), Some(json!(n))),
        Data::Float(n) => {
            if !n.is_finite() {
                return Err(CoreError::Parse);
            }
            (n.to_string(), Some(json!(n)))
        }
        Data::Error(e) => (e.to_string(), Some(json!(e.to_string()))),
        Data::DateTime(d) => {
            let date = d.as_datetime().ok_or(CoreError::Parse)?;
            let text = date.to_string();
            (text.clone(), Some(json!(text)))
        }
    })
}
fn formula(n: &Node) -> Option<String> {
    n.child("f").map(|f| match f.attr("t") {
        Some("dataTable") => {
            let mut out = "dataTable".to_owned();
            for key in ["ref", "dt2D", "dtr", "r1", "r2", "del1", "del2", "ca"] {
                if let Some(v) = f.attr(key) {
                    out.push_str(&format!("; {key}={v}"));
                }
            }
            out
        }
        Some("array") => format!("={}; array range={}", f.text, f.attr("ref").unwrap_or("")),
        _ => format!("={}", f.text),
    })
}
fn formats(xml: Option<&Node>) -> Result<Vec<String>, CoreError> {
    let Some(xml) = xml else {
        return Ok(vec!["General".to_owned()]);
    };
    let custom: BTreeMap<usize, String> = xml
        .child("numFmts")
        .map(|n| {
            n.children_named("numFmt")
                .map(|n| {
                    Ok((
                        n.attr("numFmtId")
                            .ok_or(CoreError::Parse)?
                            .parse()
                            .map_err(|_| CoreError::Parse)?,
                        n.attr("formatCode").ok_or(CoreError::Parse)?.to_owned(),
                    ))
                })
                .collect()
        })
        .transpose()?
        .unwrap_or_default();
    let builtin = |id| match id {
        0 => "General",
        1 => "0",
        2 => "0.00",
        3 => "#,##0",
        4 => "#,##0.00",
        9 => "0%",
        10 => "0.00%",
        11 => "0.00E+00",
        12 => "# ?/?",
        13 => "# ??/??",
        14 => "mm-dd-yy",
        15 => "d-mmm-yy",
        16 => "d-mmm",
        17 => "mmm-yy",
        18 => "h:mm AM/PM",
        19 => "h:mm:ss AM/PM",
        20 => "h:mm",
        21 => "h:mm:ss",
        22 => "m/d/yy h:mm",
        49 => "@",
        _ => "",
    };
    xml.child("cellXfs")
        .map(|n| {
            n.children_named("xf")
                .map(|n| {
                    let id = n
                        .attr("numFmtId")
                        .unwrap_or("0")
                        .parse::<usize>()
                        .map_err(|_| CoreError::Parse)?;
                    Ok(custom
                        .get(&id)
                        .cloned()
                        .unwrap_or_else(|| builtin(id).to_owned()))
                })
                .collect()
        })
        .unwrap_or(Ok(vec!["General".to_owned()]))
}
fn insert(
    sheet: &mut Sheet,
    mut cell: Cell,
    s: &mut Session,
    limits: WorkbookLimits,
    total: &mut usize,
) -> Result<(), CoreError> {
    s.ctx.work(1)?;
    if cell.row_span==0 || cell.column_span==0 {return Err(CoreError::Parse);}
    let end_row=cell.row.checked_add(cell.row_span-1).ok_or(CoreError::Budget)?;
    let end_column=cell.column.checked_add(cell.column_span-1).ok_or(CoreError::Budget)?;
    if end_row>limits.max_rows || end_column>limits.max_cells {sheet.partial=true;return Ok(());}
    if cell.row > limits.max_rows || *total >= limits.max_cells || cell.column > limits.max_cells {
        sheet.partial = true;
        return Ok(());
    }
    if covered(sheet, cell.row, cell.column) {
        return Ok(());
    }
    if let Some(m) = sheet
        .merges
        .iter()
        .find(|m| (m.row_start, m.column_start) == (cell.row, cell.column))
    {
        cell.row_span = m.row_end - m.row_start + 1;
        cell.column_span = m.column_end - m.column_start + 1;
    }
    cell.regions = vec![region(
        sheet,
        cell.row,
        cell.column,
        cell.row_span,
        cell.column_span,
    )];
    s.reserve(1024 + cell.text.len() * 8 + cell.formula.as_ref().map_or(0, |f| f.len() * 4))?;
    sheet.rows = sheet.rows.max(cell.row + cell.row_span - 1);
    sheet.columns = sheet.columns.max(cell.column + cell.column_span - 1);
    if sheet.cells.insert((cell.row, cell.column), cell).is_some() {
        return Err(CoreError::Parse);
    }
    *total += 1;
    Ok(())
}
fn xlsx(
    bytes: &[u8],
    mut package: Package<'_>,
    s: &mut Session,
    limits: WorkbookLimits,
) -> Result<Vec<Sheet>, CoreError> {
    let workbook = package.xml("xl/workbook.xml", s)?;
    let rels = package.relationships("xl/workbook.xml", s)?;
    let styles = if package.names.contains("xl/styles.xml") {
        Some(package.xml("xl/styles.xml", s)?)
    } else {
        None
    };
    let formats = formats(styles.as_ref())?;
    // Bound dependency-owned workbook metadata and shared strings BEFORE calamine.
    let shared_size = if package.names.contains("xl/sharedStrings.xml") {
        let raw = package.bytes("xl/sharedStrings.xml", s)?;
        super::package::parse_xml(&raw, s)?;
        raw.len()
    } else {
        0
    };
    s.reserve(shared_size.saturating_mul(16) + bytes.len().saturating_mul(2) + 65536)?;
    let mut reader = Xlsx::new(Cursor::new(bytes)).map_err(|_| CoreError::Parse)?;
    let mut sheets = Vec::new();
    let mut total = 0;
    for (i, n) in workbook
        .child("sheets")
        .ok_or(CoreError::Parse)?
        .children_named("sheet")
        .enumerate()
    {
        s.ctx.work(1)?;
        let name = n.attr("name").ok_or(CoreError::Parse)?.to_owned();
        let rel = rels
            .get(n.attr("id").ok_or(CoreError::Parse)?)
            .ok_or(CoreError::Parse)?;
        if rel.external {
            return Err(CoreError::Unsupported);
        }
        let xml = package.xml(&rel.target, s)?;
        let mut sheet = Sheet {
            name: name.clone(),
            number: i + 1,
            hidden: n.attr("state").is_some_and(|v| v != "visible"),
            ..Sheet::default()
        };
        if let Some(merges) = xml.child("mergeCells") {
            for m in merges.children_named("mergeCell") {
                let r = range(m.attr("ref").ok_or(CoreError::Parse)?)?;
                s.ctx.work(sheet.merges.len() + 1)?;
                if sheet.merges.iter().any(|p| {
                    r.row_start <= p.row_end
                        && p.row_start <= r.row_end
                        && r.column_start <= p.column_end
                        && p.column_start <= r.column_end
                }) {
                    return Err(CoreError::Parse);
                }
                sheet.merges.push(r);
            }
        }
        let mut raw = BTreeMap::new();
        let mut last_row = 0;
        let mut last_col;
        if let Some(data) = xml.child("sheetData") {
            for row in data.children_named("row") {
                last_row = row
                    .attr("r")
                    .map(|v| v.parse().map_err(|_| CoreError::Parse))
                    .unwrap_or(Ok(last_row + 1))?;
                last_col = 0;
                if row.attr("hidden") == Some("1") {
                    sheet.hidden_rows.push(last_row);
                }
                for c in row.children_named("c") {
                    s.ctx.work(1)?;
                    let (r, col) = if let Some(ref_name) = c.attr("r") {
                        position(ref_name)?
                    } else {
                        (last_row, last_col + 1)
                    };
                    last_col = col;
                    sheet.rows = sheet.rows.max(r);
                    sheet.columns = sheet.columns.max(col);
                    if raw.insert((r, col), c).is_some() {
                        return Err(CoreError::Parse);
                    }
                }
            }
        }
        if let Some(d) = xml.child("dimension").and_then(|n| n.attr("ref")) {
            let dimension = range(d)?;
            sheet.columns = sheet.columns.max(dimension.column_end);
        }
        let mut cells = reader
            .worksheet_cells_reader(&name)
            .map_err(|_| CoreError::Parse)?;
        while let Some(record) = cells.next_cell().map_err(|_| CoreError::Parse)? {
            s.ctx.work(1)?;
            let (r, c) = record.get_position();
            let (r, c) = (r as usize + 1, c as usize + 1);
            let source = raw.get(&(r, c)).ok_or(CoreError::Parse)?;
            if covered(&sheet, r, c) {
                continue;
            }
            let (text, typed) = value(Data::from(record.get_value().clone()))?;
            let expression = formula(source);
            let cache = source
                .child("v")
                .is_some_and(|v| !v.text.is_empty() || source.attr("t") == Some("str"));
            let style = source
                .attr("s")
                .unwrap_or("0")
                .parse::<usize>()
                .map_err(|_| CoreError::Parse)?;
            let format = formats.get(style).ok_or(CoreError::Parse)?.clone();
            insert(
                &mut sheet,
                Cell {
                    row: r,
                    column: c,
                    row_span: 1,
                    column_span: 1,
                    text,
                    formula: expression.clone(),
                    cached_value: if expression.is_some() && !cache {
                        None
                    } else {
                        typed.or_else(|| {
                            (cache && source.attr("t") == Some("str")).then(|| json!(""))
                        })
                    },
                    cache_freshness: expression
                        .map(|_| if cache { "unknown" } else { "unavailable" }.to_owned()),
                    format: (format != "General").then_some(format),
                    ..Cell::default()
                },
                s,
                limits,
                &mut total,
            )?;
            if total >= limits.max_cells {
                sheet.partial = raw.len() > sheet.cells.len();
                break;
            }
        }
        // calamine omits some formula-only/empty-cache cells from its value stream.
        for (&(r, c), source) in &raw {
            if !sheet.cells.contains_key(&(r, c))
                && !covered(&sheet, r, c)
                && let Some(expression) = formula(source)
            {
                let cache = source
                    .child("v")
                    .is_some_and(|v| !v.text.is_empty() || source.attr("t") == Some("str"));
                let style = source
                    .attr("s")
                    .unwrap_or("0")
                    .parse::<usize>()
                    .map_err(|_| CoreError::Parse)?;
                insert(
                    &mut sheet,
                    Cell {
                        row: r,
                        column: c,
                        row_span: 1,
                        column_span: 1,
                        formula: Some(expression),
                        cached_value: cache.then(|| {
                            json!(source.child("v").map(|v| v.text.as_str()).unwrap_or(""))
                        }),
                        cache_freshness: Some(
                            if cache { "unknown" } else { "unavailable" }.to_owned(),
                        ),
                        format: formats
                            .get(style)
                            .filter(|v| v.as_str() != "General")
                            .cloned(),
                        ..Cell::default()
                    },
                    s,
                    limits,
                    &mut total,
                )?;
            }
        }
        if sheet.columns > limits.max_cells {
            sheet.partial = true;
            sheet.columns = limits.max_cells;
        }
        sheets.push(sheet);
    }
    Ok(sheets)
}
fn odf_text(n: &Node) -> String {
    let paragraphs:Vec<String>=n.children_named("p").map(Node::full_text).collect();
    if paragraphs.is_empty() {n.full_text()} else {paragraphs.join("\n")}
}
fn ods(
    mut package: Package<'_>,
    s: &mut Session,
    limits: WorkbookLimits,
) -> Result<Vec<Sheet>, CoreError> {
    let xml = package.xml("content.xml", s)?;
    let root = xml.find("spreadsheet").ok_or(CoreError::Parse)?;
    let mut sheets = Vec::new();
    let mut total = 0;
    for (i, n) in root.children_named("table").enumerate() {
        let mut sheet = Sheet {
            name: n.attr("name").ok_or(CoreError::Parse)?.to_owned(),
            number: i + 1,
            ..Sheet::default()
        };
        let mut row = 1usize;
        let mut rows = Vec::new();
        n.descendants("table-row", &mut rows);
        for source_row in rows {
            let repeat = source_row
                .attr("number-rows-repeated")
                .unwrap_or("1")
                .parse::<usize>()
                .map_err(|_| CoreError::Parse)?;
            if repeat == 0 {
                return Err(CoreError::Parse);
            }
            if row > limits.max_rows {
                sheet.partial = true;
                break;
            }
            let allowed = repeat.min(limits.max_rows - row + 1);
            if allowed < repeat {
                sheet.partial = true;
            }
            for r in row..row + allowed {
                s.ctx.work(1)?;
                let mut column = 1;
                for source in source_row
                    .children
                    .iter()
                    .filter(|n| matches!(n.name.as_str(), "table-cell" | "covered-table-cell"))
                {
                    let repeat = source
                        .attr("number-columns-repeated")
                        .unwrap_or("1")
                        .parse::<usize>()
                        .map_err(|_| CoreError::Parse)?;
                    if repeat == 0 {
                        return Err(CoreError::Parse);
                    }
                    if column > limits.max_cells {
                        sheet.partial = true;
                        break;
                    }
                    let allowed = repeat.min(limits.max_cells - column + 1);
                    if allowed < repeat {
                        sheet.partial = true;
                    }
                    for c in column..column + allowed {
                        s.ctx.work(1)?;
                        if source.name == "covered-table-cell" {
                            continue;
                        }
                        let row_span = source
                            .attr("number-rows-spanned")
                            .unwrap_or("1")
                            .parse::<usize>()
                            .map_err(|_| CoreError::Parse)?;
                        let column_span = source
                            .attr("number-columns-spanned")
                            .unwrap_or("1")
                            .parse::<usize>()
                            .map_err(|_| CoreError::Parse)?;
                        if row_span == 0 || column_span == 0 {
                            return Err(CoreError::Parse);
                        }
                        if row_span > 1 || column_span > 1 {
                            sheet.merges.push(CellRange {
                                row_start: r,
                                row_end: r.checked_add(row_span - 1).ok_or(CoreError::Budget)?,
                                column_start: c,
                                column_end: c
                                    .checked_add(column_span - 1)
                                    .ok_or(CoreError::Budget)?,
                            });
                        }
                        let source_value = match source.attr("value-type") {
                            Some("float" | "percentage" | "currency") => source
                                .attr("value")
                                .map(|v| v.parse::<f64>().map(|n| json!(n)))
                                .transpose()
                                .map_err(|_| CoreError::Parse)?,
                            Some("boolean") => {
                                Some(json!(source.attr("boolean-value") == Some("true")))
                            }
                            Some("date") => source.attr("date-value").map(|v| json!(v)),
                            Some("time") => source.attr("time-value").map(|v| json!(v)),
                            _ => {
                                let text = odf_text(source);
                                (!text.is_empty()).then(|| json!(text))
                            }
                        };
                        let text = if source.attr("value-type") == Some("boolean") {
                            if source_value == Some(json!(true)) {
                                "True"
                            } else {
                                "False"
                            }
                            .to_owned()
                        } else {
                            let supplied = odf_text(source);
                            if !supplied.is_empty() {
                                supplied
                            } else {
                                source_value
                                    .as_ref()
                                    .map(|v| {
                                        v.as_str()
                                            .map(str::to_owned)
                                            .unwrap_or_else(|| v.to_string())
                                    })
                                    .unwrap_or_default()
                            }
                        };
                        let expression = source.attr("formula").map(str::to_owned);
                        let cell = Cell {
                            row: r,
                            column: c,
                            row_span,
                            column_span,
                            text,
                            cached_value: source_value.clone(),
                            formula: expression.clone(),
                            cache_freshness: expression.map(|_| {
                                if source_value.is_some() {
                                    "unknown"
                                } else {
                                    "unavailable"
                                }
                                .to_owned()
                            }),
                            unit: source.attr("currency").map(str::to_owned),
                            ..Cell::default()
                        };
                        insert(&mut sheet, cell, s, limits, &mut total)?;
                        if total >= limits.max_cells {
                            sheet.partial = true;
                            break;
                        }
                    }
                    column = column.checked_add(repeat).ok_or(CoreError::Budget)?;
                    if total >= limits.max_cells {
                        break;
                    }
                }
                if total >= limits.max_cells {
                    break;
                }
            }
            row = row.checked_add(repeat).ok_or(CoreError::Budget)?;
            if total >= limits.max_cells {
                break;
            }
        }
        sheets.push(sheet);
    }
    Ok(sheets)
}
fn xls(bytes: &[u8], s: &mut Session, limits: WorkbookLimits) -> Result<Vec<Sheet>, CoreError> {
    s.ctx.input(bytes.len())?;
    s.reserve(bytes.len().saturating_mul(32))?;
    let mut compound = cfb::CompoundFile::open(Cursor::new(bytes)).map_err(|_| CoreError::Parse)?;
    let name = if compound.exists("/Workbook") {
        "/Workbook"
    } else {
        "/Book"
    };
    let stream = compound.open_stream(name).map_err(|_| CoreError::Parse)?;
    let mut raw = Vec::new();
    stream
        .take(s.limits.max_part_bytes as u64 + 1)
        .read_to_end(&mut raw)
        .map_err(|_| CoreError::Parse)?;
    if raw.len() > s.limits.max_part_bytes {
        return Err(CoreError::Budget);
    }
    let mut offset = 0;
    let mut max_cells = 0usize;
    while offset + 4 <= raw.len() {
        s.ctx.work(1)?;
        let kind = u16::from_le_bytes([raw[offset], raw[offset + 1]]);
        let len = usize::from(u16::from_le_bytes([raw[offset + 2], raw[offset + 3]]));
        let data = raw
            .get(offset + 4..offset + 4 + len)
            .ok_or(CoreError::Parse)?;
        if kind == 0x002f {
            return Err(CoreError::Unsupported);
        }
        if kind==0x00fc {
            if data.len()<8 {return Err(CoreError::Parse);}
            let unique=u32::from_le_bytes(data[4..8].try_into().map_err(|_|CoreError::Parse)?) as usize;
            if unique>limits.max_cells || unique>raw.len() {return Err(CoreError::Budget);}
        }
        if matches!(kind,0x0203|0x0204|0x0205|0x00fd|0x027e|0x0006|0x00bd) {
            if data.len()<6 {return Err(CoreError::Parse);}
            let row=usize::from(u16::from_le_bytes([data[0],data[1]]))+1;
            let column=usize::from(u16::from_le_bytes([data[2],data[3]]))+1;
            let rectangle=row.checked_mul(column).ok_or(CoreError::Budget)?;
            max_cells=max_cells.max(rectangle);
            if row>limits.max_rows || rectangle>limits.max_cells {return Ok(vec![Sheet {name:"Workbook".to_owned(),number:1,partial:true,..Sheet::default()}]);}
        }
        if kind == 0x0200 && data.len() >= 12 {
            let re =
                u32::from_le_bytes(data[4..8].try_into().map_err(|_| CoreError::Parse)?) as usize;
            let ce = usize::from(u16::from_le_bytes([data[10], data[11]]));
            max_cells = max_cells
                .checked_add(re.checked_mul(ce).ok_or(CoreError::Budget)?)
                .ok_or(CoreError::Budget)?;
            if re > limits.max_rows || max_cells > limits.max_cells {
                return Ok(vec![Sheet {
                    name: "Workbook".to_owned(),
                    number: 1,
                    partial: true,
                    ..Sheet::default()
                }]);
            }
        }
        offset += 4 + len;
    }
    s.reserve(max_cells.checked_mul(512).ok_or(CoreError::Budget)?)?;
    let mut reader = Xls::new(Cursor::new(bytes)).map_err(|_| CoreError::Parse)?;
    let mut sheets = Vec::new();
    let mut total = 0;
    for (i, name) in reader.sheet_names().iter().enumerate() {
        s.ctx.work(1)?;
        let data = reader.worksheet_range(name).map_err(|_| CoreError::Parse)?;
        let formula = reader
            .worksheet_formula(name)
            .map_err(|_| CoreError::Parse)?;
        let mut sheet = Sheet {
            name: name.clone(),
            number: i + 1,
            ..Sheet::default()
        };
        for m in reader
            .merge_cells_by_sheet_name(name)
            .map_err(|_| CoreError::Parse)?
        {
            sheet.merges.push(CellRange {
                row_start: m.start.0 as usize + 1,
                row_end: m.end.0 as usize + 1,
                column_start: m.start.1 as usize + 1,
                column_end: m.end.1 as usize + 1,
            });
        }
        for (r, c, v) in data.used_cells() {
            let start = data.start().unwrap_or((0, 0));
            let row = r + start.0 as usize + 1;
            let column = c + start.1 as usize + 1;
            let (text, typed) = value(v.clone())?;
            let expression = formula
                .get_value(((row - 1) as u32, (column - 1) as u32))
                .filter(|v| !v.is_empty())
                .map(|v| format!("={v}"));
            insert(
                &mut sheet,
                Cell {
                    row,
                    column,
                    row_span: 1,
                    column_span: 1,
                    text,
                    cached_value: typed,
                    formula: expression.clone(),
                    cache_freshness: expression.map(|_| "unknown".to_owned()),
                    ..Cell::default()
                },
                s,
                limits,
                &mut total,
            )?;
        }
        sheets.push(sheet);
    }
    Ok(sheets)
}
fn render_sheets(sheets: Vec<Sheet>, bytes: &[u8], s: &mut Session) -> Result<Document, CoreError> {
    let mut doc = Document {
        generation: generation(bytes, "rust-workbook", include_str!("spreadsheets.rs")),
        ..Document::default()
    };
    doc.generation
        .dependency_versions
        .insert("calamine".to_owned(), "0.36.1".to_owned());
    let mut offset = 0;
    let mut annotations = Vec::new();
    let mut partial = false;
    for mut sheet in sheets {
        partial |= sheet.partial;
        let id = format!("sheet/{}", sheet.number);
        let mut text = String::new();
        let mut headers = Vec::new();
        let mut first_row = None;
        let mut chars=0usize;
        let mut last_byte=0usize;
        let nonblank: Vec<usize> = sheet
            .cells
            .iter()
            .filter(|(_, c)| c.cached_value.is_some() || c.formula.is_some())
            .map(|((r, _), _)| *r)
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect();
        if !nonblank.is_empty() {
            s.emit(&mut text, &format!("Sheet: {}", sheet.name))?;
            if !sheet.merges.is_empty() {
                s.emit(&mut text, "\nMerged cells: ")?;
                for (i, m) in sheet.merges.iter().enumerate() {
                    if i > 0 {
                        s.emit(&mut text, ", ")?;
                    }
                    s.emit(
                        &mut text,
                        &format!(
                            "{}:{}",
                            coordinate(m.row_start, m.column_start),
                            coordinate(m.row_end, m.column_end)
                        ),
                    )?;
                }
            }
            for row in nonblank {
                let first = first_row.is_none();
                if first {
                    first_row = Some(row);
                    headers = (1..=sheet.columns)
                        .map(|c| {
                            sheet
                                .cells
                                .get(&(row, c))
                                .map(|c| c.text.clone())
                                .unwrap_or_default()
                        })
                        .collect();
                }
                s.emit(&mut text, &format!("\nRow {row} (Sheet {}): ", sheet.name))?;
                for column in 1..=sheet.columns {
                    s.ctx.work(sheet.merges.len() + 1)?;
                    if column > 1 {
                        s.emit(&mut text, " | ")?;
                    }
                    let label = if !first {
                        headers
                            .get(column - 1)
                            .filter(|h| !h.is_empty())
                            .map(|h| format!(" [{h}]"))
                            .unwrap_or_default()
                    } else {
                        String::new()
                    };
                    s.emit(&mut text, &format!("{}{label}=", coordinate(row, column)))?;
                    if let Some(cell) = sheet.cells.get(&(row, column)) {
                        chars+=text[last_byte..].chars().count();last_byte=text.len();
                        let start = chars;
                        s.emit(&mut text, &cell.text)?;
                        chars+=text[last_byte..].chars().count();last_byte=text.len();
                        let end = chars;
                        if cell.cached_value.is_some()
                            && let Some(format) = &cell.format
                        {
                            s.emit(&mut text, &format!(" [format={format}]"))?;
                        }
                        if let Some(expression) = &cell.formula {
                            let cache = if cell.cache_freshness.as_deref() == Some("unavailable") {
                                "cached value unavailable"
                            } else {
                                "cached value supplied; freshness unknown"
                            };
                            s.emit(&mut text, &format!(" [formula={expression}; {cache}]"))?;
                        }
                        annotations.push(json!({"kind":"cell_span","block":id,"sheet":sheet.name,"row":row,"column":column,"char_start":start,"char_end":end,"typed_value":cell.cached_value}));
                    }
                }
            }
            if !doc.blocks.is_empty() {
                offset += 2;
                s.ctx.output(2)?;
            }
            let start = offset;
            offset += text.chars().count();
            for c in sheet.cells.values_mut() {
                if Some(c.row) == first_row {
                    c.header_role = HeaderRole::Column;
                    c.role_origin = Origin::Heuristic;
                }
            }
            doc.blocks.push(Block {
                id: id.clone(),
                kind: BlockKind::Table,
                text,
                table: Some(Table {
                    rows: sheet.rows.max(1),
                    columns: sheet.columns.max(1),
                    cells: sheet.cells.into_values().collect(),
                    caption: None,
                }),
                regions: vec![SourceRegion {
                    kind: Some(PartKind::Sheet),
                    number: Some(sheet.number),
                    name: Some(sheet.name.clone()),
                    ..SourceRegion::default()
                }],
                ..Block::default()
            });
            doc.source_parts.push(SourcePart {
                kind: PartKind::Sheet,
                name: sheet.name.clone(),
                number: sheet.number,
                char_start: start,
                char_end: offset,
            });
        } else {
            doc.source_parts.push(SourcePart {
                kind: PartKind::Sheet,
                name: sheet.name.clone(),
                number: sheet.number,
                char_start: offset,
                char_end: offset,
            });
        }
        annotations.push(json!({"kind":"sheet_policy","sheet":sheet.name,"hidden":sheet.hidden,"hidden_rows":sheet.hidden_rows,"included_hidden":true,"incomplete":sheet.partial,"header_origin":"heuristic","merged_header_associations":sheet.merges}));
    }
    let stats = s.ctx.stats();
    finish(
        &mut doc,
        s,
        json!({"annotations":annotations,"stats":stats}),
    )?;
    if partial {
        doc.generation.outcome = Some("partial".to_owned());
    }
    Ok(doc)
}
pub fn extract(bytes: &[u8], limits: impl Into<WorkbookLimits>) -> Result<Document, CoreError> {
    let limits = limits.into();
    extract_session(bytes, limits, Session::new(limits.package)?)
}
pub fn extract_with_context(
    bytes: &[u8],
    limits: WorkbookLimits,
    ctx: &Context,
) -> Result<Document, CoreError> {
    extract_session(
        bytes,
        limits,
        Session::with_context(limits.package, ctx.clone())?,
    )
}
fn extract_session(
    bytes: &[u8],
    limits: WorkbookLimits,
    mut s: Session,
) -> Result<Document, CoreError> {
    if limits.max_rows == 0 || limits.max_cells == 0 {
        return Err(CoreError::InvalidInput);
    }
    let sheets = if bytes.starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") {
        xls(bytes, &mut s, limits)?
    } else {
        let mut package = Package::open(bytes, &mut s)?;
        if package.names.contains("xl/workbook.xml") {
            xlsx(bytes, package, &mut s, limits)?
        } else if package.names.contains("mimetype")
            && package.bytes("mimetype", &mut s)?
                == b"application/vnd.oasis.opendocument.spreadsheet"
        {
            ods(package, &mut s, limits)?
        } else {
            return Err(CoreError::Unsupported);
        }
    };
    render_sheets(sheets, bytes, &mut s)
}
