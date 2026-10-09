//! Strict classic-xref PDF reader. No repair heuristics or unbounded decoding.
use super::Memory;
use crate::CoreError;
use std::collections::BTreeMap;

pub type Key = (u32, u16);
#[derive(Clone, Debug)]
pub enum Value {
    Number(f64),
    Name(String),
    Bytes(Vec<u8>),
    Array(Vec<Value>),
    Dict(BTreeMap<String, Value>),
    Ref(Key),
    Keyword(String),
    Null,
}
impl Value {
    pub fn number(&self) -> Result<f64, CoreError> {
        if let Self::Number(n) = self {
            Ok(*n)
        } else {
            Err(CoreError::Parse)
        }
    }
    pub fn integer(&self) -> Result<usize, CoreError> {
        let n = self.number()?;
        if n >= 0.0 && n.fract() == 0.0 && n <= u32::MAX as f64 {
            Ok(n as usize)
        } else {
            Err(CoreError::Parse)
        }
    }
    pub fn name(&self) -> Option<&str> {
        if let Self::Name(n) = self {
            Some(n)
        } else {
            None
        }
    }
    pub fn dict(&self) -> Result<&BTreeMap<String, Value>, CoreError> {
        if let Self::Dict(n) = self {
            Ok(n)
        } else {
            Err(CoreError::Parse)
        }
    }
    pub fn array(&self) -> Result<&[Value], CoreError> {
        if let Self::Array(n) = self {
            Ok(n)
        } else {
            Err(CoreError::Parse)
        }
    }
    pub fn key(&self) -> Result<Key, CoreError> {
        if let Self::Ref(n) = self {
            Ok(*n)
        } else {
            Err(CoreError::Parse)
        }
    }
}
fn space(c: u8) -> bool {
    matches!(c, 0 | 9 | 10 | 12 | 13 | 32)
}
fn delimiter(c: u8) -> bool {
    space(c)
        || matches!(
            c,
            b'(' | b')' | b'<' | b'>' | b'[' | b']' | b'{' | b'}' | b'/' | b'%'
        )
}
pub struct Reader<'a, 'm> {
    pub data: &'a [u8],
    pub at: usize,
    pub memory: &'m mut Memory,
}
impl Reader<'_, '_> {
    pub fn skip(&mut self) -> Result<(), CoreError> {
        while self.at < self.data.len() {
            if self.at.is_multiple_of(1024) {
                self.memory.ctx.work(1)?;
            }
            if space(self.data[self.at]) {
                self.at += 1;
            } else if self.data[self.at] == b'%' {
                while self.at < self.data.len() && !matches!(self.data[self.at], b'\r' | b'\n') {
                    self.at += 1;
                    if self.at.is_multiple_of(1024) {
                        self.memory.ctx.work(1)?;
                    }
                }
            } else {
                break;
            }
        }
        Ok(())
    }
    pub fn word(&mut self) -> Result<&[u8], CoreError> {
        self.skip()?;
        let start = self.at;
        while self.at < self.data.len() && !delimiter(self.data[self.at]) {
            self.at += 1;
            if self.at.is_multiple_of(1024) {
                self.memory.ctx.work(1)?;
            }
        }
        if self.at == start || self.at - start > 1024 {
            return Err(CoreError::Parse);
        }
        Ok(&self.data[start..self.at])
    }
    pub fn expect(&mut self, word: &[u8]) -> Result<(), CoreError> {
        if self.word()? == word {
            Ok(())
        } else {
            Err(CoreError::Parse)
        }
    }
    pub fn uint(&mut self) -> Result<usize, CoreError> {
        let word = self.word()?;
        let s = std::str::from_utf8(word).map_err(|_| CoreError::Parse)?;
        s.parse().map_err(|_| CoreError::Parse)
    }
    fn byte(&mut self) -> Result<u8, CoreError> {
        let c = *self.data.get(self.at).ok_or(CoreError::Parse)?;
        self.at += 1;
        Ok(c)
    }
    fn push(&mut self, out: &mut Vec<u8>, c: u8) -> Result<(), CoreError> {
        if out.len() >= 1024 * 1024 {
            return Err(CoreError::Budget);
        }
        if out.len() == out.capacity() {
            self.memory.reserve(out.capacity().max(16))?;
        }
        out.push(c);
        Ok(())
    }
    pub fn value(&mut self, depth: usize, refs: bool) -> Result<Value, CoreError> {
        self.memory.ctx.work(1)?;
        self.memory.reserve(256)?;
        if depth > 32 {
            return Err(CoreError::Budget);
        }
        self.skip()?;
        match self.byte()? {
            b'/' => {
                let start = self.at;
                while self.at < self.data.len() && !delimiter(self.data[self.at]) {
                    self.at += 1;
                    if self.at - start > 1024 {
                        return Err(CoreError::Budget);
                    }
                }
                let raw = &self.data[start..self.at];
                let mut bytes = Vec::new();
                let mut i = 0;
                while i < raw.len() {
                    let c = if raw[i] == b'#' {
                        if i + 2 >= raw.len() {
                            return Err(CoreError::Parse);
                        }
                        i += 2;
                        hex(raw[i - 1])? * 16 + hex(raw[i])?
                    } else {
                        raw[i]
                    };
                    self.push(&mut bytes, c)?;
                    i += 1;
                }
                Ok(Value::Name(
                    String::from_utf8(bytes).map_err(|_| CoreError::Unsupported)?,
                ))
            }
            b'(' => {
                let mut out = vec![];
                let mut nesting = 1;
                while nesting > 0 {
                    if self.at.is_multiple_of(1024) {
                        self.memory.ctx.work(1)?;
                    }
                    let c = self.byte()?;
                    match c {
                        b'(' => {
                            nesting += 1;
                            if nesting > 32 {
                                return Err(CoreError::Budget);
                            }
                            self.push(&mut out, c)?;
                        }
                        b')' => {
                            nesting -= 1;
                            if nesting > 0 {
                                self.push(&mut out, c)?;
                            }
                        }
                        b'\\' => {
                            let c = self.byte()?;
                            match c {
                                b'\n' => {}
                                b'\r' => {
                                    if self.data.get(self.at) == Some(&b'\n') {
                                        self.at += 1;
                                    }
                                }
                                b'n' => self.push(&mut out, b'\n')?,
                                b'r' => self.push(&mut out, b'\r')?,
                                b't' => self.push(&mut out, b'\t')?,
                                b'b' => self.push(&mut out, 8)?,
                                b'f' => self.push(&mut out, 12)?,
                                b'0'..=b'7' => {
                                    let mut n = u16::from(c - b'0');
                                    for _ in 0..2 {
                                        if let Some(d @ b'0'..=b'7') = self.data.get(self.at) {
                                            n = n * 8 + u16::from(*d - b'0');
                                            self.at += 1;
                                        } else {
                                            break;
                                        }
                                    }
                                    self.push(&mut out, n as u8)?;
                                }
                                _ => self.push(&mut out, c)?,
                            }
                        }
                        b'\r' => {
                            if self.data.get(self.at) == Some(&b'\n') {
                                self.at += 1;
                            }
                            self.push(&mut out, b'\n')?;
                        }
                        _ => self.push(&mut out, c)?,
                    }
                }
                Ok(Value::Bytes(out))
            }
            b'<' if self.data.get(self.at) == Some(&b'<') => {
                self.at += 1;
                let mut dict = BTreeMap::new();
                loop {
                    self.skip()?;
                    if self.data.get(self.at..self.at + 2) == Some(b">>") {
                        self.at += 2;
                        break;
                    }
                    let key = self.value(depth + 1, refs)?;
                    let Value::Name(key) = key else {
                        return Err(CoreError::Parse);
                    };
                    let v = self.value(depth + 1, refs)?;
                    self.memory.reserve(128)?;
                    if dict.insert(key, v).is_some() {
                        return Err(CoreError::Parse);
                    }
                }
                Ok(Value::Dict(dict))
            }
            b'<' => {
                let mut out = vec![];
                let mut high = None;
                loop {
                    if self.at.is_multiple_of(1024) {
                        self.memory.ctx.work(1)?;
                    }
                    let c = self.byte()?;
                    if c == b'>' {
                        break;
                    }
                    if space(c) {
                        continue;
                    }
                    let n = hex(c)?;
                    if let Some(h) = high.take() {
                        self.push(&mut out, h * 16 + n)?;
                    } else {
                        high = Some(n);
                    }
                }
                if let Some(h) = high {
                    self.push(&mut out, h * 16)?;
                }
                Ok(Value::Bytes(out))
            }
            b'[' => {
                let mut out = vec![];
                loop {
                    self.skip()?;
                    if self.data.get(self.at) == Some(&b']') {
                        self.at += 1;
                        break;
                    }
                    self.memory.reserve(128)?;
                    out.push(self.value(depth + 1, refs)?);
                }
                Ok(Value::Array(out))
            }
            b'+' | b'-' | b'.' | b'0'..=b'9' => {
                self.at -= 1;
                let raw = self.word()?;
                let text = std::str::from_utf8(raw).map_err(|_| CoreError::Parse)?;
                let n: f64 = text.parse().map_err(|_| CoreError::Parse)?;
                if !n.is_finite() || n.abs() > 1e12 {
                    return Err(CoreError::Parse);
                }
                if refs && n >= 0.0 && n.fract() == 0.0 && n <= u32::MAX as f64 {
                    let saved = self.at;
                    if let Ok(g) = self.uint()
                        && g <= u16::MAX as usize
                        && self.word().is_ok_and(|w| w == b"R")
                    {
                        return Ok(Value::Ref((n as u32, g as u16)));
                    }
                    self.at = saved;
                }
                Ok(Value::Number(n))
            }
            _ => {
                self.at -= 1;
                let word = self.word()?;
                if word == b"null" || word == b"true" || word == b"false" {
                    Ok(Value::Null)
                } else {
                    let word = String::from_utf8(word.to_vec()).map_err(|_| CoreError::Parse)?;
                    Ok(Value::Keyword(word))
                }
            }
        }
    }
}
fn hex(c: u8) -> Result<u8, CoreError> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        _ => Err(CoreError::Parse),
    }
}
pub struct Object {
    pub value: Value,
    pub stream: Option<(usize, usize)>,
}
pub struct File<'a> {
    pub bytes: &'a [u8],
    pub objects: BTreeMap<Key, Object>,
    pub trailer: Value,
}
impl<'a> File<'a> {
    pub fn open(bytes: &'a [u8], memory: &mut Memory) -> Result<Self, CoreError> {
        memory.ctx.input(bytes.len())?;
        if !bytes.starts_with(b"%PDF-1.") {
            return Err(CoreError::Parse);
        }
        let tail = &bytes[bytes.len().saturating_sub(1024)..];
        let start = tail
            .windows(9)
            .rposition(|w| w == b"startxref")
            .ok_or(CoreError::Parse)?;
        let mut r = Reader {
            data: tail,
            at: start + 9,
            memory,
        };
        let xref = r.uint()?;
        while tail.get(r.at).is_some_and(|c| space(*c)) {
            r.at += 1;
        }
        if !tail[r.at..].starts_with(b"%%EOF") || tail[r.at + 5..].iter().any(|c| !space(*c)) {
            return Err(CoreError::Parse);
        }
        if xref >= bytes.len() {
            return Err(CoreError::Parse);
        }
        let mut r = Reader {
            data: bytes,
            at: xref,
            memory,
        };
        if r.word()? != b"xref" {
            return Err(CoreError::Unsupported);
        }
        let mut offsets = BTreeMap::new();
        loop {
            r.skip()?;
            if bytes[r.at..].starts_with(b"trailer") {
                r.expect(b"trailer")?;
                break;
            }
            let first = r.uint()?;
            let count = r.uint()?;
            if first.checked_add(count).is_none_or(|n| n > 100_000) {
                return Err(CoreError::Budget);
            }
            r.memory.ctx.work(count)?;
            r.memory.reserve(count * 96)?;
            for i in 0..count {
                let offset = r.uint()?;
                let gen_id = r.uint()?;
                let status = r.word()?;
                if status == b"n" {
                    if offset >= xref
                        || gen_id > u16::MAX as usize
                        || offsets
                            .insert(((first + i) as u32, gen_id as u16), offset)
                            .is_some()
                    {
                        return Err(CoreError::Parse);
                    }
                } else if status != b"f" {
                    return Err(CoreError::Parse);
                }
            }
        }
        let trailer = r.value(0, true)?;
        let td = trailer.dict()?;
        if td.contains_key("Encrypt") {
            return Err(CoreError::Unsupported);
        }
        if td.contains_key("Prev") || td.contains_key("XRefStm") {
            return Err(CoreError::Unsupported);
        }
        let mut objects = BTreeMap::new();
        for (&key, &offset) in &offsets {
            let mut r = Reader {
                data: bytes,
                at: offset,
                memory,
            };
            if r.uint()? != key.0 as usize || r.uint()? != key.1 as usize {
                return Err(CoreError::Parse);
            }
            r.expect(b"obj")?;
            let value = r.value(0, true)?;
            r.skip()?;
            let stream = if bytes[r.at..].starts_with(b"stream") {
                r.expect(b"stream")?;
                let length = value.dict()?.get("Length").ok_or(CoreError::Parse)?;
                let length = if let Value::Ref(length_key) = length {
                    let offset = *offsets.get(length_key).ok_or(CoreError::Parse)?;
                    let mut lr = Reader {
                        data: bytes,
                        at: offset,
                        memory: r.memory,
                    };
                    if lr.uint()? != length_key.0 as usize || lr.uint()? != length_key.1 as usize {
                        return Err(CoreError::Parse);
                    }
                    lr.expect(b"obj")?;
                    let length = lr.value(0, true)?.integer()?;
                    lr.expect(b"endobj")?;
                    length
                } else {
                    length.integer()?
                };
                if bytes.get(r.at) == Some(&b'\r') {
                    r.at += 1;
                }
                if bytes.get(r.at) == Some(&b'\n') {
                    r.at += 1;
                } else if bytes.get(r.at.wrapping_sub(1)) != Some(&b'\r') {
                    return Err(CoreError::Parse);
                }
                let start = r.at;
                let end = start
                    .checked_add(length)
                    .filter(|e| *e < bytes.len())
                    .ok_or(CoreError::Parse)?;
                r.at = end;
                r.expect(b"endstream")?;
                Some((start, end))
            } else {
                None
            };
            r.expect(b"endobj")?;
            if r.at > xref {
                return Err(CoreError::Parse);
            }
            if value
                .dict()
                .ok()
                .and_then(|d| d.get("Type"))
                .and_then(Value::name)
                .is_some_and(|n| matches!(n, "ObjStm" | "XRef"))
            {
                return Err(CoreError::Unsupported);
            }
            memory.reserve(128)?;
            objects.insert(key, Object { value, stream });
        }
        Ok(Self {
            bytes,
            objects,
            trailer,
        })
    }
    pub fn resolve<'b>(&'b self, v: &'b Value) -> Result<&'b Value, CoreError> {
        let mut v = v;
        for _ in 0..32 {
            if let Value::Ref(k) = v {
                v = &self.objects.get(k).ok_or(CoreError::Parse)?.value;
            } else {
                return Ok(v);
            }
        }
        Err(CoreError::Parse)
    }
    pub fn dictionary<'b>(
        &'b self,
        v: &'b Value,
    ) -> Result<&'b BTreeMap<String, Value>, CoreError> {
        self.resolve(v)?.dict()
    }
    pub fn object(&self, v: &Value) -> Result<&Object, CoreError> {
        self.objects.get(&v.key()?).ok_or(CoreError::Parse)
    }
}
