//! Bounded byte/control-word RTF state machine, with no external conversion.
use super::package::{Limits, Session, finish, generation};
use crate::{
    CoreError,
    canonical::{Block, BlockKind, Cell, Document, SourceRegion, Table},
    runtime::Context,
};
use encoding_rs::{Encoding, WINDOWS_1252};
use serde_json::json;

#[derive(Clone, Copy)]
struct State {
    encoding: &'static Encoding,
    uc: usize,
    note: Option<usize>,
    skip: bool,
    heading: Option<usize>,
    list: bool,
}
impl Default for State {
    fn default() -> Self {
        Self {
            encoding: WINDOWS_1252,
            uc: 1,
            note: None,
            skip: false,
            heading: None,
            list: false,
        }
    }
}
struct Reader {
    s: Session,
    d: Document,
    paragraph: String,
    kind: BlockKind,
    heading: Option<usize>,
    pending: Option<u16>,
    raw: Vec<u8>,
    notes: Vec<String>,
    row: Option<Vec<String>>,
    note_ranges: Vec<(usize, usize)>,
    ranges: Vec<serde_json::Value>,
    start: usize,
    paragraph_ordinal: usize,
}
impl Reader {
    fn emit(&mut self, text: &str, state: State) -> Result<(), CoreError> {
        if state.skip {
            return Ok(());
        }
        if let Some(n) = state.note {
            return self.s.emit(&mut self.notes[n], text);
        }
        if self.paragraph.is_empty() {
            self.kind = if state.heading.is_some() {
                BlockKind::Heading
            } else if state.list {
                BlockKind::List
            } else {
                BlockKind::Paragraph
            };
            self.heading = state.heading;
        }
        self.s.emit(&mut self.paragraph, text)
    }
    fn flush_bytes(&mut self, state: State) -> Result<(), CoreError> {
        if self.raw.is_empty() {
            return Ok(());
        }
        if self.pending.is_some() {
            return Err(CoreError::Parse);
        }
        self.s.reserve(self.raw.len() * 4 + 64)?;
        let (text, errors) = state.encoding.decode_without_bom_handling(&self.raw);
        if errors {
            return Err(CoreError::Parse);
        }
        let text = text.into_owned();
        self.raw.clear();
        self.emit(&text, state)
    }
    fn push(&mut self, mut b: Block, path: String, end: usize) -> Result<String, CoreError> {
        self.s.reserve(2048 + path.len() * 4)?;
        b.id = format!("b{}", self.d.blocks.len() + 1);
        b.regions.push(SourceRegion {
            name: Some(path.clone()),
            ..SourceRegion::default()
        });
        self.ranges
            .push(json!({"block_id":b.id,"byte_start":self.start,"byte_end":end,"path":path}));
        let id = b.id.clone();
        self.d.blocks.push(b);
        self.start = end;
        Ok(id)
    }
    fn paragraph(&mut self, end: usize) -> Result<(), CoreError> {
        if self.pending.is_some() {
            return Err(CoreError::Parse);
        }
        if self.row.is_some() {
            return Err(CoreError::Unsupported);
        }
        if self.paragraph.is_empty() {
            return Ok(());
        }
        self.paragraph_ordinal += 1;
        let text = std::mem::take(&mut self.paragraph);
        let id = self.push(
            Block {
                kind: self.kind,
                text,
                heading_level: self.heading,
                ..Block::default()
            },
            format!("rtf/paragraph[{}]", self.paragraph_ordinal),
            end,
        )?;
        let note_ranges = std::mem::take(&mut self.note_ranges);
        for (i, text) in std::mem::take(&mut self.notes).into_iter().enumerate() {
            self.push(
                Block {
                    kind: BlockKind::Footnote,
                    text,
                    parent_id: Some(id.clone()),
                    ..Block::default()
                },
                format!(
                    "rtf/paragraph[{}]/footnote[{}]",
                    self.paragraph_ordinal,
                    i + 1
                ),
                end,
            )?;
            let range = self.ranges.last_mut().unwrap();
            range["byte_start"] = json!(note_ranges[i].0);
            range["byte_end"] = json!(note_ranges[i].1);
        }
        Ok(())
    }
    fn unicode(&mut self, unit: u16, state: State) -> Result<(), CoreError> {
        if state.skip {
            return Ok(());
        }
        if (0xd800..=0xdbff).contains(&unit) {
            if self.pending.replace(unit).is_some() {
                return Err(CoreError::Parse);
            }
            return Ok(());
        }
        let scalar = if let Some(high) = self.pending.take() {
            if !(0xdc00..=0xdfff).contains(&unit) {
                return Err(CoreError::Parse);
            }
            0x10000 + ((u32::from(high) - 0xd800) << 10) + (u32::from(unit) - 0xdc00)
        } else {
            u32::from(unit)
        };
        let c = char::from_u32(scalar).ok_or(CoreError::Parse)?;
        self.emit(c.encode_utf8(&mut [0; 4]), state)
    }
}
fn encoding(cp: i32) -> Result<&'static Encoding, CoreError> {
    let label = match cp {
        1250..=1258 => format!("windows-{cp}"),
        65001 => "utf-8".into(),
        932 => "shift_jis".into(),
        936 => "gbk".into(),
        949 => "euc-kr".into(),
        950 => "big5".into(),
        _ => return Err(CoreError::Unsupported),
    };
    Encoding::for_label(label.as_bytes()).ok_or(CoreError::Unsupported)
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
    s.ctx.input(bytes.len())?;
    s.reserve(bytes.len().checked_mul(8).ok_or(CoreError::Budget)?)?;
    if !bytes.starts_with(br"{\rtf1") {
        return Err(CoreError::Unsupported);
    }
    let mut r = Reader {
        s,
        d: Document {
            generation: generation(bytes, "rtf-rust", include_str!("rtf.rs")),
            ..Document::default()
        },
        paragraph: String::new(),
        kind: BlockKind::Paragraph,
        heading: None,
        pending: None,
        raw: Vec::new(),
        notes: Vec::new(),
        row: None,
        note_ranges: Vec::new(),
        ranges: Vec::new(),
        start: 0,
        paragraph_ordinal: 0,
    };
    let mut stack = Vec::new();
    let mut state = State::default();
    let mut i = 0;
    let mut fallback = 0usize;
    let mut closed = false;
    while i < bytes.len() {
        r.s.ctx.work(1)?;
        let c = bytes[i];
        i += 1;
        if closed {
            if !c.is_ascii_whitespace() {
                return Err(CoreError::Parse);
            }
            continue;
        }
        match c {
            b'{' => {
                r.flush_bytes(state)?;
                if fallback > 0 || r.pending.is_some() {
                    return Err(CoreError::Parse);
                }
                if stack.len() >= limits.max_xml_depth {
                    return Err(CoreError::Budget);
                }
                r.s.reserve(128)?;
                stack.push(state);
            }
            b'}' => {
                r.flush_bytes(state)?;
                if fallback > 0 || r.pending.is_some() {
                    return Err(CoreError::Parse);
                }
                let leaving_note = state.note;
                state = stack.pop().ok_or(CoreError::Parse)?;
                if let Some(n) = leaving_note.filter(|n| Some(*n) != state.note) {
                    r.note_ranges[n].1 = i;
                }
                if stack.is_empty() {
                    closed = true;
                }
            }
            b'\r' | b'\n' => {}
            b'\\' => {
                let symbol = *bytes.get(i).ok_or(CoreError::Parse)?;
                i += 1;
                if symbol == b'\'' {
                    let hex = bytes.get(i..i + 2).ok_or(CoreError::Parse)?;
                    let text = std::str::from_utf8(hex).map_err(|_| CoreError::Parse)?;
                    let v = u8::from_str_radix(text, 16).map_err(|_| CoreError::Parse)?;
                    i += 2;
                    if fallback > 0 {
                        fallback -= 1;
                    } else if !state.skip {
                        r.raw.push(v);
                    }
                    continue;
                }
                if matches!(symbol, b'\\' | b'{' | b'}') {
                    if fallback > 0 {
                        fallback -= 1;
                    } else if !state.skip {
                        r.raw.push(symbol);
                    }
                    continue;
                }
                r.flush_bytes(state)?;
                if !symbol.is_ascii_alphabetic() {
                    match symbol {
                        b'*' => return Err(CoreError::Unsupported),
                        b'~' => r.emit("\u{a0}", state)?,
                        b'-' => {}
                        b'_' => r.emit("\u{2011}", state)?,
                        _ => return Err(CoreError::Parse),
                    }
                    continue;
                }
                let start = i - 1;
                while i < bytes.len() && bytes[i].is_ascii_alphabetic() {
                    i += 1;
                    if i - start > 64 {
                        return Err(CoreError::Parse);
                    }
                }
                let word = std::str::from_utf8(&bytes[start..i]).map_err(|_| CoreError::Parse)?;
                let numeric = i;
                if bytes.get(i) == Some(&b'-') {
                    i += 1;
                }
                while i < bytes.len() && bytes[i].is_ascii_digit() {
                    i += 1;
                    if i - numeric > 11 {
                        return Err(CoreError::Parse);
                    }
                }
                let number = if i > numeric {
                    Some(
                        std::str::from_utf8(&bytes[numeric..i])
                            .map_err(|_| CoreError::Parse)?
                            .parse::<i32>()
                            .map_err(|_| CoreError::Parse)?,
                    )
                } else {
                    None
                };
                if bytes.get(i) == Some(&b' ') {
                    i += 1;
                }
                if fallback > 0 {
                    return Err(CoreError::Parse);
                }
                if state.skip && !matches!(word, "object" | "objdata" | "pict" | "bin" | "fcharset")
                {
                    continue;
                }
                match word {
                    "rtf" => {
                        if number != Some(1) {
                            return Err(CoreError::Unsupported);
                        }
                    }
                    "ansi" => {}
                    "ansicpg" => state.encoding = encoding(number.ok_or(CoreError::Parse)?)?,
                    "mac" | "pc" | "pca" | "fcharset" | "object" | "objdata" | "pict" | "bin" => {
                        return Err(CoreError::Unsupported);
                    }
                    "fonttbl" | "colortbl" | "stylesheet" | "info" | "pntext" | "listtext" => {
                        state.skip = true
                    }
                    "uc" => {
                        let n = number.ok_or(CoreError::Parse)?;
                        if !(0..=16).contains(&n) {
                            return Err(CoreError::Unsupported);
                        }
                        state.uc = n as usize;
                    }
                    "u" => {
                        let n = number.ok_or(CoreError::Parse)?;
                        let unit = i16::try_from(n).map_err(|_| CoreError::Parse)? as u16;
                        r.unicode(unit, state)?;
                        fallback = state.uc;
                    }
                    "par" => {
                        if let Some(n) = state.note {
                            r.s.emit(&mut r.notes[n], "\n")?;
                        } else {
                            r.paragraph(i)?;
                        }
                    }
                    "line" => r.emit("\n", state)?,
                    "tab" => r.emit("\t", state)?,
                    "outlinelevel" => {
                        state.heading = match number {
                            Some(0..=5) => Some(number.unwrap() as usize + 1),
                            Some(9) => None,
                            _ => return Err(CoreError::Unsupported),
                        }
                    }
                    "ls" => state.list = number.ok_or(CoreError::Parse)? != 0,
                    "pard" => {
                        state.heading = None;
                        state.list = false;
                    }
                    "footnote" => {
                        if state.note.is_some() || r.row.is_some() {
                            return Err(CoreError::Unsupported);
                        }
                        r.s.reserve(256)?;
                        r.notes.push(String::new());
                        r.note_ranges.push((start - 1, i));
                        state.note = Some(r.notes.len() - 1);
                    }
                    "trowd" => {
                        r.paragraph(i)?;
                        r.row = Some(Vec::new());
                    }
                    "cell" => {
                        r.s.reserve(512)?;
                        r.row
                            .as_mut()
                            .ok_or(CoreError::Parse)?
                            .push(std::mem::take(&mut r.paragraph));
                    }
                    "row" => {
                        if !r.paragraph.is_empty() {
                            return Err(CoreError::Parse);
                        }
                        let values = r.row.take().ok_or(CoreError::Parse)?;
                        if values.is_empty() {
                            return Err(CoreError::Parse);
                        }
                        r.s.reserve(
                            values.iter().map(String::len).sum::<usize>() * 8 + values.len() * 1024,
                        )?;
                        let columns = values.len();
                        let text = values.join("\t");
                        let next_table_path = format!("rtf/table[{}]", r.d.blocks.len() + 1);
                        let previous = r.d.blocks.last_mut().filter(|b| b.kind == BlockKind::Table);
                        let row_number = previous
                            .as_ref()
                            .map_or(1, |b| b.table.as_ref().unwrap().rows + 1);
                        let table_path = previous
                            .as_ref()
                            .map_or(next_table_path, |b| b.regions[0].name.clone().unwrap());
                        let cells = values
                            .into_iter()
                            .enumerate()
                            .map(|(column, text)| Cell {
                                row: row_number,
                                column: column + 1,
                                row_span: 1,
                                column_span: 1,
                                text,
                                regions: vec![SourceRegion {
                                    name: Some(format!(
                                        "{table_path}/row[{row_number}]/cell[{}]",
                                        column + 1
                                    )),
                                    ..SourceRegion::default()
                                }],
                                ..Cell::default()
                            })
                            .collect::<Vec<_>>();
                        if let Some(block) = previous {
                            let table = block.table.as_mut().unwrap();
                            if table.columns != columns {
                                return Err(CoreError::Unsupported);
                            }
                            table.rows += 1;
                            table.cells.extend(cells);
                            block.text.push('\n');
                            block.text.push_str(&text);
                            r.ranges.last_mut().unwrap()["byte_end"] = json!(i);
                        } else {
                            r.push(
                                Block {
                                    kind: BlockKind::Table,
                                    text,
                                    table: Some(Table {
                                        rows: 1,
                                        columns,
                                        cells,
                                        caption: None,
                                    }),
                                    ..Block::default()
                                },
                                table_path,
                                i,
                            )?;
                        }
                    }
                    "clmgf" | "clmrg" | "clvmgf" | "clvmrg" | "nesttableprops" => {
                        return Err(CoreError::Unsupported);
                    }
                    "b" | "i" | "ul" | "ulnone" | "fs" | "f" | "deff" | "lang" | "langfe"
                    | "red" | "green" | "blue" | "viewkind" | "widowctrl" | "plain" | "ql"
                    | "qr" | "qc" | "qj" | "li" | "ri" | "fi" | "sa" | "sb" | "sl" | "slmult"
                    | "intbl" | "cellx" | "trgaph" | "trleft" => {}
                    _ => return Err(CoreError::Unsupported),
                }
            }
            _ => {
                if fallback > 0 {
                    fallback -= 1;
                } else if !state.skip {
                    r.raw.push(c);
                }
            }
        }
    }
    if !closed || !stack.is_empty() || fallback > 0 {
        return Err(CoreError::Parse);
    }
    r.paragraph(i)?;
    finish(
        &mut r.d,
        &mut r.s,
        json!({"format":"rtf","candidate":true,"byte_ranges":r.ranges}),
    )?;
    let rendered = crate::canonical::render(r.d.clone())?;
    if rendered.rendered_text.chars().count() > limits.budget.max_output_chars {
        return Err(CoreError::Budget);
    }
    Ok(r.d)
}
