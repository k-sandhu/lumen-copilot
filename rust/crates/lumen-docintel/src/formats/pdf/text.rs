//! PDF text state, font maps and positioned glyphs; no font/image rasterization.
use super::{
    Memory, Page, stream,
    syntax::{File, Reader, Value},
};
use crate::{
    CoreError,
    canonical::{BoundingBox, CoordinateOrigin, CoordinateUnit},
};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone)]
pub(super) struct Glyph {
    pub text: String,
    pub bbox: BoundingBox,
    pub size: f64,
    pub bold: bool,
}
#[derive(Default)]
pub(super) struct PageText {
    pub glyphs: Vec<Glyph>,
    pub has_images: bool,
    pub lines: Vec<[f64; 4]>,
}
#[derive(Clone, Copy)]
struct Matrix([f64; 6]);
impl Matrix {
    fn identity() -> Self {
        Self([1., 0., 0., 1., 0., 0.])
    }
    fn point(self, x: f64, y: f64) -> (f64, f64) {
        let [a, b, c, d, e, f] = self.0;
        (a * x + c * y + e, b * x + d * y + f)
    }
    fn concat(self, other: Self) -> Self {
        let [a, b, c, d, e, f] = self.0;
        let [g, h, i, j, k, l] = other.0;
        Self([
            a * g + c * h,
            b * g + d * h,
            a * i + c * j,
            b * i + d * j,
            a * k + c * l + e,
            b * k + d * l + f,
        ])
    }
    fn shift(&mut self, x: f64, y: f64) {
        let (e, f) = self.point(x, y);
        self.0[4] = e;
        self.0[5] = f;
    }
}
#[derive(Clone)]
struct Font {
    widths: BTreeMap<u16, f64>,
    default: f64,
    cmap: BTreeMap<Vec<u8>, String>,
    two_byte: bool,
    bold: bool,
    winansi: bool,
}
pub(super) fn metadata_text(bytes: &[u8]) -> Result<String, CoreError> {
    if bytes.starts_with(&[0xfe, 0xff]) {
        utf16(&bytes[2..])
    } else {
        if bytes.iter().any(|b| *b >= 128) {
            return Err(CoreError::Unsupported);
        }
        String::from_utf8(bytes.to_vec()).map_err(|_| CoreError::Parse)
    }
}
fn utf16(bytes: &[u8]) -> Result<String, CoreError> {
    if !bytes.len().is_multiple_of(2) {
        return Err(CoreError::Parse);
    }
    String::from_utf16(
        &bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|b| u16::from_be_bytes([b[0], b[1]]))
            .collect::<Vec<_>>(),
    )
    .map_err(|_| CoreError::Parse)
}
fn code(bytes: &[u8]) -> Result<u16, CoreError> {
    match bytes {
        [a] => Ok(u16::from(*a)),
        [a, b] => Ok(u16::from_be_bytes([*a, *b])),
        _ => Err(CoreError::Unsupported),
    }
}
fn cmap(data: &[u8], m: &mut Memory) -> Result<BTreeMap<Vec<u8>, String>, CoreError> {
    let mut r = Reader {
        data,
        at: 0,
        memory: m,
    };
    let mut result = BTreeMap::new();
    let mut pending: Vec<Value> = vec![];
    while r.at < data.len() {
        r.skip()?;
        if r.at == data.len() {
            break;
        }
        let v = r.value(0, false)?;
        if let Value::Keyword(k) = &v {
            match k.as_str() {
                "beginbfchar" | "beginbfrange" => {
                    let count = pending.last().ok_or(CoreError::Parse)?.integer()?;
                    r.memory.ctx.work(count)?;
                    if count > 65536 {
                        return Err(CoreError::Budget);
                    }
                    let range = k == "beginbfrange";
                    pending.clear();
                    for _ in 0..count {
                        let Value::Bytes(first) = r.value(0, false)? else {
                            return Err(CoreError::Parse);
                        };
                        let first_code = code(&first)?;
                        let last = if range {
                            let Value::Bytes(last) = r.value(0, false)? else {
                                return Err(CoreError::Parse);
                            };
                            if last.len() != first.len() {
                                return Err(CoreError::Parse);
                            }
                            code(&last)?
                        } else {
                            first_code
                        };
                        if last < first_code {
                            return Err(CoreError::Parse);
                        }
                        let destination = r.value(0, false)?;
                        let n = usize::from(last - first_code) + 1;
                        r.memory.ctx.work(n)?;
                        r.memory.reserve(n * 192)?;
                        for i in 0..n {
                            let text = match &destination {
                                Value::Bytes(b) => {
                                    let mut b = b.clone();
                                    if range && i > 0 {
                                        let end = b.len();
                                        if end < 2 {
                                            return Err(CoreError::Parse);
                                        }
                                        let old = u16::from_be_bytes([b[end - 2], b[end - 1]]);
                                        let new = old
                                            .checked_add(i as u16)
                                            .ok_or(CoreError::Parse)?
                                            .to_be_bytes();
                                        b[end - 2..].copy_from_slice(&new);
                                    }
                                    utf16(&b)?
                                }
                                Value::Array(a) => {
                                    let Value::Bytes(b) = a.get(i).ok_or(CoreError::Parse)? else {
                                        return Err(CoreError::Parse);
                                    };
                                    utf16(b)?
                                }
                                _ => return Err(CoreError::Parse),
                            };
                            if text.is_empty() {
                                return Err(CoreError::Parse);
                            }
                            let number = first_code + i as u16;
                            let key = if first.len() == 1 {
                                vec![number as u8]
                            } else {
                                number.to_be_bytes().to_vec()
                            };
                            if result.insert(key, text).is_some() {
                                return Err(CoreError::Parse);
                            }
                        }
                    }
                    r.expect(if range { b"endbfrange" } else { b"endbfchar" })?;
                }
                _ => pending.clear(),
            }
        } else {
            r.memory.reserve(128)?;
            pending.push(v);
            if pending.len() > 64 {
                return Err(CoreError::Budget);
            }
        }
    }
    Ok(result)
}
fn font(file: &File, v: &Value, m: &mut Memory) -> Result<Font, CoreError> {
    let d = file.dictionary(v)?;
    let subtype = d
        .get("Subtype")
        .and_then(Value::name)
        .ok_or(CoreError::Parse)?;
    let base = d.get("BaseFont").and_then(Value::name).unwrap_or("");
    let mut f = Font {
        widths: BTreeMap::new(),
        default: 600.,
        cmap: BTreeMap::new(),
        two_byte: false,
        bold: base.contains("Bold"),
        winansi: false,
    };
    if let Some(map) = d.get("ToUnicode") {
        let raw = stream(file, map, m)?;
        f.cmap = cmap(&raw, m)?;
    }
    if subtype == "Type0" {
        if d.get("Encoding").and_then(Value::name) != Some("Identity-H") {
            return Err(CoreError::Unsupported);
        }
        f.two_byte = true;
        if f.cmap.is_empty() {
            return Err(CoreError::Unsupported);
        }
        let descendants = file
            .resolve(d.get("DescendantFonts").ok_or(CoreError::Parse)?)?
            .array()?;
        if descendants.len() != 1 {
            return Err(CoreError::Parse);
        }
        let cid = file.dictionary(&descendants[0])?;
        f.default = cid
            .get("DW")
            .map(Value::number)
            .transpose()?
            .unwrap_or(1000.);
        if let Some(widths) = cid.get("W") {
            let a = file.resolve(widths)?.array()?;
            let mut i = 0;
            while i < a.len() {
                let first = a[i].integer()?;
                let next = a.get(i + 1).ok_or(CoreError::Parse)?;
                i += 2;
                if let Value::Array(w) = next {
                    m.ctx.work(w.len())?;
                    m.reserve(w.len() * 96)?;
                    for (j, w) in w.iter().enumerate() {
                        let c = first
                            .checked_add(j)
                            .filter(|c| *c <= 65535)
                            .ok_or(CoreError::Parse)?;
                        f.widths.insert(c as u16, w.number()?);
                    }
                } else {
                    let last = next.integer()?;
                    if last < first || last > 65535 {
                        return Err(CoreError::Parse);
                    }
                    let width = a.get(i).ok_or(CoreError::Parse)?.number()?;
                    i += 1;
                    m.ctx.work(last - first + 1)?;
                    m.reserve((last - first + 1) * 96)?;
                    for c in first..=last {
                        f.widths.insert(c as u16, width);
                    }
                }
            }
        }
    } else if matches!(subtype, "Type1" | "TrueType") {
        if let Some(enc) = d.get("Encoding") {
            let enc = file.resolve(enc)?;
            if enc.name() == Some("WinAnsiEncoding") {
                f.winansi = true;
            } else if enc.name() != Some("StandardEncoding") {
                return Err(CoreError::Unsupported);
            }
        }
        if let Some(widths) = d.get("Widths") {
            let first = d.get("FirstChar").ok_or(CoreError::Parse)?.integer()?;
            let widths = file.resolve(widths)?.array()?;
            m.ctx.work(widths.len())?;
            m.reserve(widths.len() * 96)?;
            for (i, w) in widths.iter().enumerate() {
                let c = first
                    .checked_add(i)
                    .filter(|c| *c <= 255)
                    .ok_or(CoreError::Parse)?;
                f.widths.insert(c as u16, w.number()?);
            }
        } else if base.starts_with("Courier") {
            f.default = 600.;
        } else if base.starts_with("Helvetica") || base.starts_with("Arial") {
            // Standard advance widths, not claimed ink extents.
            let widths = [
                278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
                556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
                1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
                667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
                333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
                556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
            ];
            m.reserve(widths.len() * 96)?;
            for (i, w) in widths.iter().enumerate() {
                f.widths.insert((i + 32) as u16, f64::from(*w));
            }
        } else {
            return Err(CoreError::Unsupported);
        }
    } else {
        return Err(CoreError::Unsupported);
    }
    if f.default < 0.0 || f.default > 10000.0 || f.widths.values().any(|w| *w < 0.0 || *w > 10000.0)
    {
        return Err(CoreError::Parse);
    }
    Ok(f)
}
#[derive(Clone)]
struct State {
    ctm: Matrix,
    tm: Matrix,
    line: Matrix,
    font: String,
    size: f64,
    leading: f64,
    scale: f64,
    spacing: f64,
    word_spacing: f64,
    rise: f64,
    in_text: bool,
    invisible: bool,
}
impl Default for State {
    fn default() -> Self {
        Self {
            ctm: Matrix::identity(),
            tm: Matrix::identity(),
            line: Matrix::identity(),
            font: String::new(),
            size: 12.,
            leading: 0.,
            scale: 1.,
            spacing: 0.,
            word_spacing: 0.,
            rise: 0.,
            in_text: false,
            invisible: false,
        }
    }
}
fn numbers(args: &[Value], n: usize) -> Result<Vec<f64>, CoreError> {
    if args.len() != n {
        return Err(CoreError::Parse);
    }
    args.iter().map(Value::number).collect()
}
fn matrix(args: &[Value]) -> Result<Matrix, CoreError> {
    let n = numbers(args, 6)?;
    Ok(Matrix(n.try_into().map_err(|_| CoreError::Parse)?))
}
fn show(
    data: &[u8],
    state: &mut State,
    fonts: &BTreeMap<String, Font>,
    out: &mut PageText,
    page: &Page,
    m: &mut Memory,
) -> Result<(), CoreError> {
    if !state.in_text {
        return Err(CoreError::Parse);
    }
    let f = fonts.get(&state.font).ok_or(CoreError::Parse)?;
    let width = if f.two_byte { 2 } else { 1 };
    if !data.len().is_multiple_of(width) {
        return Err(CoreError::Parse);
    }
    for bytes in data.chunks_exact(width) {
        m.ctx.work(1)?;
        let code = code(bytes)?;
        let text = if let Some(t) = f.cmap.get(bytes) {
            t.clone()
        } else if !f.cmap.is_empty() {
            return Err(CoreError::Unsupported);
        } else if bytes[0] < 128 {
            String::from_utf8(bytes.to_vec()).map_err(|_| CoreError::Parse)?
        } else if f.winansi {
            let (t, _, bad) = encoding_rs::WINDOWS_1252.decode(bytes);
            if bad {
                return Err(CoreError::Parse);
            }
            t.into_owned()
        } else {
            return Err(CoreError::Unsupported);
        };
        if text.chars().any(|c| c.is_control() && !c.is_whitespace()) {
            return Err(CoreError::Unsupported);
        }
        let advance = f.widths.get(&code).copied().unwrap_or(f.default) * state.size / 1000.;
        let transform = state.ctm.concat(state.tm);
        let corners = [
            transform.point(0., state.rise - state.size * 0.2),
            transform.point(advance * state.scale, state.rise - state.size * 0.2),
            transform.point(0., state.rise + state.size * 0.8),
            transform.point(advance * state.scale, state.rise + state.size * 0.8),
        ];
        let x0 = corners.iter().map(|p| p.0).fold(f64::INFINITY, f64::min);
        let x1 = corners
            .iter()
            .map(|p| p.0)
            .fold(f64::NEG_INFINITY, f64::max);
        let y0 = corners.iter().map(|p| p.1).fold(f64::INFINITY, f64::min);
        let y1 = corners
            .iter()
            .map(|p| p.1)
            .fold(f64::NEG_INFINITY, f64::max);
        if [x0, x1, y0, y1]
            .iter()
            .any(|n| !n.is_finite() || n.abs() > 1e8)
        {
            return Err(CoreError::Parse);
        }
        if !state.invisible {
            if x0 < 0. || y0 < 0. || x1 > page.width + 1. || y1 > page.height + 1. {
                return Err(CoreError::Unsupported);
            }
            m.reserve(256 + text.len() * 4)?;
            m.ctx.output(text.chars().count())?;
            out.glyphs.push(Glyph {
                text,
                bbox: BoundingBox {
                    x0,
                    y0,
                    x1,
                    y1,
                    unit: CoordinateUnit::Point,
                    origin: CoordinateOrigin::BottomLeft,
                },
                size: (y1 - y0).max(1.),
                bold: f.bold,
            });
        }
        state.tm.shift(
            (advance + state.spacing + if code == 32 { state.word_spacing } else { 0. })
                * state.scale,
            0.,
        );
    }
    Ok(())
}
#[allow(clippy::too_many_arguments)]
fn interpret(
    file: &File,
    data: &[u8],
    resources: &Value,
    state: &mut State,
    out: &mut PageText,
    page: &Page,
    depth: usize,
    active: &mut BTreeSet<super::syntax::Key>,
    m: &mut Memory,
) -> Result<(), CoreError> {
    if depth > 16 {
        return Err(CoreError::Budget);
    }
    let resources = file.dictionary(resources)?;
    let mut fonts = BTreeMap::new();
    if let Some(fs) = resources.get("Font") {
        for (k, v) in file.dictionary(fs)? {
            m.ctx.work(1)?;
            m.reserve(256)?;
            fonts.insert(k.clone(), font(file, v, m)?);
        }
    }
    let xobjects = resources
        .get("XObject")
        .map(|v| file.dictionary(v))
        .transpose()?;
    let mut at = 0;
    let mut args: Vec<Value> = vec![];
    let mut stack = vec![];
    let mut path: Vec<(f64, f64)> = vec![];
    let mut pending_lines: Vec<[f64; 4]> = vec![];
    while at < data.len() {
        m.ctx.work(1)?;
        let mut r = Reader {
            data,
            at,
            memory: m,
        };
        r.skip()?;
        if r.at == data.len() {
            break;
        }
        let value = r.value(0, false)?;
        at = r.at;
        if let Value::Keyword(op) = value {
            match op.as_str() {
                "BT" => {
                    if state.in_text || !args.is_empty() {
                        return Err(CoreError::Parse);
                    }
                    state.in_text = true;
                    state.tm = Matrix::identity();
                    state.line = state.tm;
                }
                "ET" => {
                    if !state.in_text || !args.is_empty() {
                        return Err(CoreError::Parse);
                    }
                    state.in_text = false;
                }
                "Tf" => {
                    if args.len() != 2 {
                        return Err(CoreError::Parse);
                    }
                    state.font = args[0].name().ok_or(CoreError::Parse)?.into();
                    state.size = args[1].number()?;
                    if state.size <= 0. || state.size > 10000. || !fonts.contains_key(&state.font) {
                        return Err(CoreError::Parse);
                    }
                }
                "Tm" => {
                    state.tm = matrix(&args)?;
                    state.line = state.tm;
                }
                "Td" | "TD" => {
                    let n = numbers(&args, 2)?;
                    if op == "TD" {
                        state.leading = -n[1];
                    }
                    state.line.shift(n[0], n[1]);
                    state.tm = state.line;
                }
                "T*" => {
                    numbers(&args, 0)?;
                    state.line.shift(0., -state.leading);
                    state.tm = state.line;
                }
                "TL" => state.leading = numbers(&args, 1)?[0],
                "Tc" => state.spacing = numbers(&args, 1)?[0],
                "Tw" => state.word_spacing = numbers(&args, 1)?[0],
                "Tz" => state.scale = numbers(&args, 1)?[0] / 100.,
                "Ts" => state.rise = numbers(&args, 1)?[0],
                "Tr" => {
                    let mode = numbers(&args, 1)?[0];
                    if !(0.0..=3.0).contains(&mode) || mode.fract() != 0. {
                        return Err(CoreError::Unsupported);
                    }
                    state.invisible = mode == 3.;
                }
                "Tj" | "'" | "\"" => {
                    if op == "'" || op == "\"" {
                        state.line.shift(0., -state.leading);
                        state.tm = state.line;
                    }
                    let index = if op == "\"" {
                        if args.len() != 3 {
                            return Err(CoreError::Parse);
                        }
                        state.word_spacing = args[0].number()?;
                        state.spacing = args[1].number()?;
                        2
                    } else {
                        if args.len() != 1 {
                            return Err(CoreError::Parse);
                        }
                        0
                    };
                    let Value::Bytes(bytes) = &args[index] else {
                        return Err(CoreError::Parse);
                    };
                    show(bytes, state, &fonts, out, page, m)?;
                }
                "TJ" => {
                    if args.len() != 1 {
                        return Err(CoreError::Parse);
                    }
                    for v in args[0].array()? {
                        match v {
                            Value::Bytes(bytes) => show(bytes, state, &fonts, out, page, m)?,
                            Value::Number(n) => {
                                state.tm.shift(-n / 1000. * state.size * state.scale, 0.)
                            }
                            _ => return Err(CoreError::Parse),
                        }
                    }
                }
                "q" => {
                    numbers(&args, 0)?;
                    if stack.len() >= 32 {
                        return Err(CoreError::Budget);
                    }
                    m.reserve(512)?;
                    stack.push(state.clone());
                }
                "Q" => {
                    numbers(&args, 0)?;
                    *state = stack.pop().ok_or(CoreError::Parse)?;
                }
                "cm" => state.ctm = state.ctm.concat(matrix(&args)?),
                "Do" => {
                    if args.len() != 1 {
                        return Err(CoreError::Parse);
                    }
                    let name = args[0].name().ok_or(CoreError::Parse)?;
                    let v = xobjects.and_then(|d| d.get(name)).ok_or(CoreError::Parse)?;
                    let d = file.dictionary(v)?;
                    match d.get("Subtype").and_then(Value::name) {
                        Some("Image") => out.has_images = true,
                        Some("Form") => {
                            let key = v.key()?;
                            if !active.insert(key) {
                                return Err(CoreError::Parse);
                            }
                            let raw = stream(file, v, m)?;
                            let mut child = state.clone();
                            if let Some(mat) = d.get("Matrix") {
                                child.ctm = child.ctm.concat(matrix(file.resolve(mat)?.array()?)?);
                            }
                            let empty = Value::Dict(BTreeMap::new());
                            interpret(
                                file,
                                &raw,
                                d.get("Resources").unwrap_or(&empty),
                                &mut child,
                                out,
                                page,
                                depth + 1,
                                active,
                                m,
                            )?;
                            active.remove(&key);
                        }
                        _ => return Err(CoreError::Unsupported),
                    }
                }
                "m" => {
                    let n = numbers(&args, 2)?;
                    path.clear();
                    m.reserve(64)?;
                    path.push(state.ctm.point(n[0], n[1]));
                }
                "l" => {
                    let n = numbers(&args, 2)?;
                    let end = state.ctm.point(n[0], n[1]);
                    if let Some(start) = path.last() {
                        m.reserve(64)?;
                        pending_lines.push([start.0, start.1, end.0, end.1]);
                    }
                    m.reserve(64)?;
                    path.push(end);
                }
                "re" => {
                    let n = numbers(&args, 4)?;
                    let pts = [
                        state.ctm.point(n[0], n[1]),
                        state.ctm.point(n[0] + n[2], n[1]),
                        state.ctm.point(n[0] + n[2], n[1] + n[3]),
                        state.ctm.point(n[0], n[1] + n[3]),
                    ];
                    m.reserve(256)?;
                    for i in 0..4 {
                        let (a, b) = (pts[i], pts[(i + 1) % 4]);
                        pending_lines.push([a.0, a.1, b.0, b.1]);
                    }
                }
                "h" => {
                    numbers(&args, 0)?;
                    if let Some((first, last)) = path.first().zip(path.last()) {
                        m.reserve(64)?;
                        pending_lines.push([last.0, last.1, first.0, first.1]);
                    }
                }
                "S" | "s" | "n" | "f" | "f*" | "F" | "B" | "B*" | "b" | "b*" => {
                    numbers(&args, 0)?;
                    if matches!(op.as_str(), "s" | "b" | "b*")
                        && let Some((first, last)) = path.first().zip(path.last()) {
                            m.reserve(64)?;
                            pending_lines.push([last.0, last.1, first.0, first.1]);
                    }
                    if matches!(op.as_str(), "S" | "s" | "B" | "B*" | "b" | "b*") {
                        m.reserve(pending_lines.len() * 64)?;
                        out.lines.append(&mut pending_lines);
                    } else {
                        pending_lines.clear();
                    }
                    path.clear();
                }
                "w" | "J" | "j" | "M" | "G" | "g" | "i" => {
                    numbers(&args, 1)?;
                }
                "RG" | "rg" => {
                    numbers(&args, 3)?;
                }
                "K" | "k" => {
                    numbers(&args, 4)?;
                }
                "d" => {
                    if args.len() != 2 {
                        return Err(CoreError::Parse);
                    }
                    args[0].array()?;
                    args[1].number()?;
                }
                "BMC" | "MP" => {
                    if args.len() != 1 || args[0].name().is_none() {
                        return Err(CoreError::Parse);
                    }
                }
                "BDC" | "DP" => {
                    if args.len() != 2 {
                        return Err(CoreError::Parse);
                    }
                    if let Value::Dict(d) = &args[1]
                        && d.contains_key("ActualText")
                    {
                        return Err(CoreError::Unsupported);
                    }
                }
                "EMC" => {
                    numbers(&args, 0)?;
                }
                // Unsupported graphics/encoding can affect evidence or visibility.
                _ => return Err(CoreError::Unsupported),
            }
            args.clear();
        } else {
            if args.len() >= 64 {
                return Err(CoreError::Budget);
            }
            m.reserve(128)?;
            args.push(value);
        }
    }
    if !args.is_empty() || !stack.is_empty() || state.in_text {
        return Err(CoreError::Parse);
    }
    Ok(())
}
pub(super) fn page(file: &File, page: &Page, m: &mut Memory) -> Result<PageText, CoreError> {
    let mut out = PageText::default();
    let mut state = State::default();
    let mut raw = vec![];
    if let Some(contents) = page.contents {
        let contents = file.resolve(contents)?;
        let refs = if let Value::Array(a) = contents {
            a.iter().collect::<Vec<_>>()
        } else {
            vec![page.contents.unwrap()]
        };
        for v in refs {
            let bytes = stream(file, v, m)?;
            m.reserve(bytes.len() + 1)?;
            raw.extend_from_slice(&bytes);
            raw.push(b'\n');
        }
        interpret(
            file,
            &raw,
            page.resources,
            &mut state,
            &mut out,
            page,
            0,
            &mut BTreeSet::new(),
            m,
        )?;
    }
    Ok(out)
}
