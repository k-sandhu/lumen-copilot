//! Canonical schema v1, with Python-compatible Unicode code-point offsets.
use crate::CoreError;
use serde::de::{DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Serialize};
use std::cell::Cell as ScalarCounter;
use std::collections::{BTreeMap, HashMap};

pub const MAX_JSON_BYTES: usize = 32 * 1024 * 1024;
const MAX_BLOCKS: usize = 100_000;
const MAX_CHARS: usize = 2_000_000;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BlockKind {
    Heading,
    #[default]
    Paragraph,
    List,
    Table,
    Figure,
    Caption,
    Footnote,
    Code,
    Furniture,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Origin {
    #[default]
    Source,
    Heuristic,
    Derived,
    Ocr,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PartKind {
    Page,
    Slide,
    Sheet,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CoordinateUnit {
    Point,
    Pixel,
    Normalized,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CoordinateOrigin {
    TopLeft,
    BottomLeft,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BoundingBox {
    pub x0: f64,
    pub y0: f64,
    pub x1: f64,
    pub y1: f64,
    pub unit: CoordinateUnit,
    pub origin: CoordinateOrigin,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CellRange {
    pub row_start: usize,
    pub row_end: usize,
    pub column_start: usize,
    pub column_end: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct SourceRegion {
    pub kind: Option<PartKind>,
    pub number: Option<usize>,
    pub name: Option<String>,
    pub cell_range: Option<CellRange>,
    pub bbox: Option<BoundingBox>,
    pub origin: Origin,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourcePart {
    pub kind: PartKind,
    pub name: String,
    pub number: usize,
    pub char_start: usize,
    pub char_end: usize,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HeaderRole {
    #[default]
    Unknown,
    Row,
    Column,
    Both,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Cell {
    pub row: usize,
    pub column: usize,
    pub row_span: usize,
    pub column_span: usize,
    pub text: String,
    pub header_role: HeaderRole,
    pub role_origin: Origin,
    pub formula: Option<String>,
    pub cached_value: Option<serde_json::Value>,
    pub cache_freshness: Option<String>,
    pub format: Option<String>,
    pub unit: Option<String>,
    pub regions: Vec<SourceRegion>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Table {
    pub rows: usize,
    pub columns: usize,
    pub cells: Vec<Cell>,
    pub caption: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Block {
    pub id: String,
    pub kind: BlockKind,
    pub text: String,
    pub parent_id: Option<String>,
    pub heading_level: Option<usize>,
    pub heading_path: Vec<String>,
    pub origin: Origin,
    pub regions: Vec<SourceRegion>,
    pub table: Option<Table>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Generation {
    pub source_sha256: Option<String>,
    pub extraction_id: Option<String>,
    pub parser_id: Option<String>,
    pub parser_version: Option<String>,
    pub build_id: Option<String>,
    pub dependency_versions: BTreeMap<String, String>,
    pub fingerprint: Option<serde_json::Value>,
    pub diagnostics: Option<serde_json::Value>,
    pub outcome: Option<String>,
    pub tokenizer_id: Option<String>,
    pub chunker_settings: Option<serde_json::Value>,
    pub embedding_model: Option<String>,
    pub embedding_dimension: Option<usize>,
    pub ocr_identity: Option<serde_json::Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Document {
    pub schema_version: usize,
    pub renderer_version: usize,
    pub blocks: Vec<Block>,
    pub source_parts: Vec<SourcePart>,
    pub generation: Generation,
}

impl Default for Document {
    fn default() -> Self {
        Self {
            schema_version: 1,
            renderer_version: 1,
            blocks: vec![],
            source_parts: vec![],
            generation: Generation::default(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BlockSpan {
    pub block_id: String,
    pub char_start: usize,
    pub char_end: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RenderedDocument {
    pub document: Document,
    pub rendered_text: String,
    pub spans: Vec<BlockSpan>,
}

fn valid_region(region: &SourceRegion) -> bool {
    if region.kind.is_some() != region.number.is_some() || region.number == Some(0) {
        return false;
    }
    if let Some(r) = &region.cell_range
        && (r.row_start == 0
            || r.column_start == 0
            || r.row_end < r.row_start
            || r.column_end < r.column_start)
    {
        return false;
    }
    if let Some(b) = &region.bbox {
        let values = [b.x0, b.y0, b.x1, b.y1];
        if values.iter().any(|v| !v.is_finite() || *v < 0.0) || b.x1 < b.x0 || b.y1 < b.y0 {
            return false;
        }
        if b.unit == CoordinateUnit::Normalized && values.iter().any(|v| *v > 1.0) {
            return false;
        }
    }
    true
}

fn validate_table(table: &Table, origin_cells: &mut usize) -> Result<(), CoreError> {
    if table.rows == 0 || table.columns == 0 {
        return Err(CoreError::InvalidInput);
    }
    // Bound event/active-set allocation before constructing either collection.
    *origin_cells = origin_cells
        .checked_add(table.cells.len())
        .ok_or(CoreError::Budget)?;
    if *origin_cells > 100_000 {
        return Err(CoreError::Budget);
    }
    let mut events = Vec::with_capacity(table.cells.len() * 2);
    for cell in &table.cells {
        let end_row = cell
            .row
            .checked_add(cell.row_span)
            .ok_or(CoreError::InvalidInput)?;
        let end_column = cell
            .column
            .checked_add(cell.column_span)
            .ok_or(CoreError::InvalidInput)?;
        if cell.row == 0
            || cell.column == 0
            || cell.row_span == 0
            || cell.column_span == 0
            || end_row - 1 > table.rows
            || end_column - 1 > table.columns
            || cell.regions.iter().any(|r| !valid_region(r))
        {
            return Err(CoreError::InvalidInput);
        }
        // End events sort before starts at a shared row: touching is legal.
        events.push((cell.row, 1u8, cell.column, end_column));
        events.push((end_row, 0u8, cell.column, end_column));
    }
    events.sort_unstable();
    let mut active: BTreeMap<usize, usize> = BTreeMap::new();
    for (_, kind, column, end_column) in events {
        if kind == 0 {
            active.remove(&column);
            continue;
        }
        if active
            .range(..=column)
            .next_back()
            .is_some_and(|(_, end)| *end > column)
            || active
                .range(column..)
                .next()
                .is_some_and(|(start, _)| *start < end_column)
        {
            return Err(CoreError::InvalidInput);
        }
        active.insert(column, end_column);
    }
    Ok(())
}

pub fn render(document: Document) -> Result<RenderedDocument, CoreError> {
    if document.schema_version != 1 || document.renderer_version != 1 {
        return Err(CoreError::InvalidInput);
    }
    if document.blocks.len() > MAX_BLOCKS {
        return Err(CoreError::Budget);
    }
    let mut ids = HashMap::with_capacity(document.blocks.len());
    let mut chars: usize = document.blocks.len().saturating_sub(1).saturating_mul(2);
    let mut bytes = chars;
    let mut origin_cells = 0;
    for (n, block) in document.blocks.iter().enumerate() {
        if block.id.is_empty()
            || ids.insert(block.id.as_str(), n).is_some()
            || block.regions.iter().any(|r| !valid_region(r))
            || (block.kind == BlockKind::Heading) != block.heading_level.is_some()
            || block
                .heading_level
                .is_some_and(|level| !(1..=6).contains(&level))
            || (block.kind == BlockKind::Table) != block.table.is_some()
        {
            return Err(CoreError::InvalidInput);
        }
        chars = chars
            .checked_add(block.text.chars().count())
            .ok_or(CoreError::Budget)?;
        bytes = bytes
            .checked_add(block.text.len())
            .ok_or(CoreError::Budget)?;
        if chars > MAX_CHARS || bytes > MAX_JSON_BYTES {
            return Err(CoreError::Budget);
        }
        if let Some(table) = &block.table {
            validate_table(table, &mut origin_cells)?;
        }
    }
    // Iterative color walk: O(blocks), no recursive stack for hostile hierarchy.
    let mut colors = vec![0_u8; document.blocks.len()];
    for start in 0..document.blocks.len() {
        if colors[start] == 2 {
            continue;
        }
        let mut chain = vec![];
        let mut cursor = Some(start);
        while let Some(n) = cursor {
            if colors[n] == 2 {
                break;
            }
            if colors[n] == 1 {
                return Err(CoreError::InvalidInput);
            }
            colors[n] = 1;
            chain.push(n);
            cursor = match &document.blocks[n].parent_id {
                None => None,
                Some(id) => Some(*ids.get(id.as_str()).ok_or(CoreError::InvalidInput)?),
            };
        }
        for n in chain {
            colors[n] = 2;
        }
    }
    let mut prior_end = 0;
    for part in &document.source_parts {
        if part.number == 0
            || part.char_start < prior_end
            || part.char_start > part.char_end
            || part.char_end > chars
        {
            return Err(CoreError::InvalidInput);
        }
        prior_end = part.char_end;
    }
    if let Some(hash) = &document.generation.source_sha256
        && (hash.len() != 64 || !hash.bytes().all(|b| b.is_ascii_hexdigit()))
    {
        return Err(CoreError::InvalidInput);
    }
    let mut rendered_text = String::with_capacity(bytes);
    let mut spans = Vec::with_capacity(document.blocks.len());
    let mut offset = 0;
    for block in &document.blocks {
        if !spans.is_empty() {
            rendered_text.push_str("\n\n");
            offset += 2;
        }
        let start = offset;
        rendered_text.push_str(&block.text);
        offset += block.text.chars().count();
        spans.push(BlockSpan {
            block_id: block.id.clone(),
            char_start: start,
            char_end: offset,
        });
    }
    Ok(RenderedDocument {
        document,
        rendered_text,
        spans,
    })
}

/// Check shape without building collections. Compact JSON must not amplify into
/// unbounded serde/model allocations before validation gets a chance to run.
fn check_json_shape(input: &str) -> Result<(), CoreError> {
    let remaining = (128usize * 1024 * 1024)
        .checked_sub(32 * 1024 * 1024 + input.len().saturating_mul(2))
        .ok_or(CoreError::Budget)?;
    let max_nodes = (remaining / 512).min(100_000);
    let count = ScalarCounter::new(0usize);
    let exhausted = ScalarCounter::new(false);
    #[derive(Clone, Copy)]
    struct Shape<'a> {
        count: &'a ScalarCounter<usize>,
        exhausted: &'a ScalarCounter<bool>,
        limit: usize,
    }
    impl<'de> DeserializeSeed<'de> for Shape<'_> {
        type Value = ();
        fn deserialize<D: serde::Deserializer<'de>>(self, d: D) -> Result<(), D::Error> {
            if self.count.get() == self.limit {
                self.exhausted.set(true);
                return Err(serde::de::Error::custom("model shape budget"));
            }
            self.count.set(self.count.get() + 1);
            d.deserialize_any(self)
        }
    }
    impl<'de> Visitor<'de> for Shape<'_> {
        type Value = ();
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("bounded document JSON")
        }
        fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<(), E> {
            Ok(())
        }
        fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<(), E> {
            Ok(())
        }
        fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<(), E> {
            Ok(())
        }
        fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<(), E> {
            Ok(())
        }
        fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<(), E> {
            Ok(())
        }
        fn visit_unit<E: serde::de::Error>(self) -> Result<(), E> {
            Ok(())
        }
        fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
            while seq.next_element_seed(self)?.is_some() {}
            Ok(())
        }
        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
            while map.next_key::<IgnoredAny>()?.is_some() {
                map.next_value_seed(self)?;
            }
            Ok(())
        }
    }
    let mut parser = serde_json::Deserializer::from_str(input);
    Shape {
        count: &count,
        exhausted: &exhausted,
        limit: max_nodes,
    }
    .deserialize(&mut parser)
    .map_err(|_| {
        if exhausted.get() {
            CoreError::Budget
        } else {
            CoreError::InvalidInput
        }
    })?;
    parser.end().map_err(|_| CoreError::InvalidInput)
}

pub fn render_json(input: &str) -> Result<String, CoreError> {
    if input.len() > MAX_JSON_BYTES {
        return Err(CoreError::Budget);
    }
    check_json_shape(input)?;
    let document = serde_json::from_str(input).map_err(|_| CoreError::InvalidInput)?;
    serde_json::to_string(&render(document)?).map_err(|_| CoreError::Internal)
}
