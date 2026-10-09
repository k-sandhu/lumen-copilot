//! Conservative ruling/alignment detection. All roles and boxes are heuristic.
use super::{
    Memory, Page,
    layout::{self, Line},
    text::PageText,
};
use crate::{
    CoreError,
    canonical::{Block, BlockKind, BoundingBox, Cell, HeaderRole, Origin, Table},
    runtime::Context,
};
use std::collections::{BTreeMap, BTreeSet};

pub(super) struct Candidate {
    pub block: Block,
    pub bbox: BoundingBox,
    pub edges: Vec<f64>,
}
fn axis(mut values: Vec<f64>) -> Vec<f64> {
    values.sort_by(f64::total_cmp);
    values.dedup_by(|a, b| (*a - *b).abs() < 1.);
    values
}
fn overlaps(a: [f64; 4], b: [f64; 4]) -> bool {
    a[0].min(a[2]) <= b[0].max(b[2]) + 1.
        && b[0].min(b[2]) <= a[0].max(a[2]) + 1.
        && a[1].min(a[3]) <= b[1].max(b[3]) + 1.
        && b[1].min(b[3]) <= a[1].max(a[3]) + 1.
}
fn bbox(page: &Page, x0: f64, y0: f64, x1: f64, y1: f64) -> BoundingBox {
    let _ = page;
    BoundingBox {
        x0,
        y0,
        x1,
        y1,
        unit: crate::canonical::CoordinateUnit::Point,
        origin: crate::canonical::CoordinateOrigin::BottomLeft,
    }
}
fn inside(b: &BoundingBox, x: f64, y: f64) -> bool {
    b.x0 <= x && x <= b.x1 && b.y0 <= y && y <= b.y1
}
fn cell_text(text: &PageText, b: &BoundingBox, ctx: &Context) -> Result<String, CoreError> {
    let subset = PageText {
        glyphs: text
            .glyphs
            .iter()
            .filter(|g| {
                inside(
                    b,
                    (g.bbox.x0 + g.bbox.x1) / 2.,
                    (g.bbox.y0 + g.bbox.y1) / 2.,
                )
            })
            .cloned()
            .collect(),
        ..PageText::default()
    };
    ctx.work(text.glyphs.len() / 32 + 1)?;
    Ok(layout::lines(&subset, ctx)?
        .iter()
        .map(|l| l.text.trim())
        .collect::<Vec<_>>()
        .join("\n"))
}
type PageSegment = (usize,usize,usize);
fn render_table(
    table: &Table,
    ctx: &Context,
) -> Result<(String, Vec<PageSegment>), CoreError> {
    ctx.work(
        table
            .rows
            .checked_mul(table.columns)
            .and_then(|n| n.checked_mul(table.cells.len()))
            .ok_or(CoreError::Budget)?,
    )?;
    let mut text = String::from("[Table]");
    ctx.output(7)?;
    let mut headers = BTreeMap::new();
    for c in &table.cells {
        if c.header_role == HeaderRole::Column {
            for column in c.column..c.column + c.column_span {
                headers.entry(column).or_insert(c.text.as_str());
            }
        }
    }
    let mut spans = vec![];
    let mut offset = 7;
    for row in 1..=table.rows {
        ctx.work(1)?;
        let page = table
            .cells
            .iter()
            .find(|c| c.row == row)
            .and_then(|c| c.regions.first())
            .and_then(|r| r.number)
            .ok_or(CoreError::Parse)?;
        let begin = offset;
        let prefix = format!("\nRow {row}: ");
        ctx.output(prefix.chars().count())?;
        offset += prefix.chars().count();
        text.push_str(&prefix);
        for column in 1..=table.columns {
            ctx.work(1)?;
            if column > 1 {
                ctx.output(3)?;
                offset += 3;
                text.push_str(" | ");
            }
            let cell = table
                .cells
                .iter()
                .find(|c| c.row == row && c.column == column);
            let label = if row > 1 {
                headers
                    .get(&column)
                    .filter(|h| !h.is_empty())
                    .map(|h| format!(" [{h}]"))
                    .unwrap_or_default()
            } else {
                String::new()
            };
            let prefix = format!("C{column}{label}=");
            ctx.output(prefix.chars().count())?;
            offset += prefix.chars().count();
            text.push_str(&prefix);
            if let Some(cell) = cell {
                ctx.output(cell.text.chars().count())?;
                offset += cell.text.chars().count();
                text.push_str(&cell.text);
            }
        }
        spans.push((page, begin, offset));
    }
    ctx.output(9)?;
    text.push_str("\n[/Table]");
    if let Some(s) = spans.first_mut() {
        s.1 = 0;
    }
    if let Some(s) = spans.last_mut() {
        s.2 += 9;
    }
    Ok((text, spans))
}
fn finish(
    page: &Page,
    cells: Vec<Cell>,
    rows: usize,
    columns: usize,
    bbox: BoundingBox,
    edges: Vec<f64>,
    m: &mut Memory,
) -> Result<Candidate, CoreError> {
    let table = Table {
        rows,
        columns,
        cells,
        caption: None,
    };
    m.reserve(
        table
            .cells
            .iter()
            .map(|c| c.text.len() * 16 + 1024)
            .sum::<usize>()
            + rows * columns * 256,
    )?;
    let (text, _) = render_table(&table, &m.ctx)?;
    Ok(Candidate {
        block: Block {
            id: String::new(),
            kind: BlockKind::Table,
            text,
            origin: Origin::Heuristic,
            regions: vec![layout::region(page.number, bbox.clone())],
            table: Some(table),
            ..Block::default()
        },
        bbox,
        edges,
    })
}
fn ruled(page: &Page, text: &PageText, m: &mut Memory) -> Result<Vec<Candidate>, CoreError> {
    let lines: Vec<_> = text
        .lines
        .iter()
        .copied()
        .filter(|l| {
            l.iter().all(|v| v.is_finite() && *v >= 0.)
                && ((l[0] - l[2]).abs() < 0.5 || (l[1] - l[3]).abs() < 0.5)
                && ((l[0] - l[2]).abs() > 2. || (l[1] - l[3]).abs() > 2.)
        })
        .collect();
    if lines.len() > 512 {
        return Err(CoreError::Budget);
    }
    m.ctx.work(lines.len() * lines.len())?;
    m.reserve(lines.len() * 256)?;
    let mut groups: Vec<Vec<[f64; 4]>> = vec![];
    let mut used = BTreeSet::new();
    for (i, line) in lines.iter().enumerate() {
        if used.contains(&i) {
            continue;
        }
        let mut group = vec![*line];
        used.insert(i);
        let mut cursor = 0;
        while cursor < group.len() {
            for (j, line) in lines.iter().enumerate() {
                if !used.contains(&j) && overlaps(group[cursor], *line) {
                    used.insert(j);
                    group.push(*line);
                }
            }
            cursor += 1;
        }
        groups.push(group);
    }
    let mut out = vec![];
    for group in groups {
        let xs = axis(
            group
                .iter()
                .filter(|l| (l[0] - l[2]).abs() < 0.5)
                .map(|l| l[0])
                .collect(),
        );
        let mut ys = axis(
            group
                .iter()
                .filter(|l| (l[1] - l[3]).abs() < 0.5)
                .map(|l| l[1])
                .collect(),
        );
        ys.reverse();
        if xs.len() < 2 || ys.len() < 3 {
            continue;
        }
        let rows = ys.len() - 1;
        let columns = xs.len() - 1;
        let count = rows.checked_mul(columns).ok_or(CoreError::Budget)?;
        m.ctx.work(count)?;
        m.reserve(count * 2048 + text.glyphs.len() * 512)?;
        if count > 10000 {
            return Err(CoreError::Budget);
        }
        let outer = bbox(page, xs[0], ys[rows], xs[columns], ys[0]);
        let vertical = |x: f64, top: f64, bottom: f64| {
            group.iter().any(|l| {
                (l[0] - l[2]).abs() < 0.5
                    && (l[0] - x).abs() < 1.
                    && l[1].max(l[3]) >= top - 1.
                    && l[1].min(l[3]) <= bottom + 1.
            })
        };
        let horizontal = |y: f64, left: f64, right: f64| {
            group.iter().any(|l| {
                (l[1] - l[3]).abs() < 0.5
                    && (l[1] - y).abs() < 1.
                    && l[0].max(l[2]) >= right - 1.
                    && l[0].min(l[2]) <= left + 1.
            })
        };
        if !vertical(xs[0], ys[0], ys[rows])
            || !vertical(xs[columns], ys[0], ys[rows])
            || !horizontal(ys[0], xs[0], xs[columns])
            || !horizontal(ys[rows], xs[0], xs[columns])
        {
            continue;
        }
        m.ctx
            .work(count.checked_mul(count).ok_or(CoreError::Budget)?)?;
        let mut parents: Vec<usize> = (0..count).collect();
        fn root(parents: &[usize], mut i: usize) -> usize {
            while parents[i] != i {
                i = parents[i];
            }
            i
        }
        for r in 0..rows {
            for c in 0..columns {
                m.ctx.work(1)?;
                let here = r * columns + c;
                if c + 1 < columns && !vertical(xs[c + 1], ys[r], ys[r + 1]) {
                    let a = root(&parents, here);
                    let b = root(&parents, here + 1);
                    parents[b] = a;
                }
                if r + 1 < rows && !horizontal(ys[r + 1], xs[c], xs[c + 1]) {
                    let a = root(&parents, here);
                    let b = root(&parents, here + columns);
                    parents[b] = a;
                }
            }
        }
        let mut members: BTreeMap<usize, Vec<(usize, usize)>> = BTreeMap::new();
        for r in 0..rows {
            for c in 0..columns {
                members
                    .entry(root(&parents, r * columns + c))
                    .or_default()
                    .push((r, c));
            }
        }
        let mut cells = vec![];
        let mut irregular = false;
        for members in members.values() {
            let r0 = members.iter().map(|p| p.0).min().unwrap();
            let r1 = members.iter().map(|p| p.0).max().unwrap() + 1;
            let c0 = members.iter().map(|p| p.1).min().unwrap();
            let c1 = members.iter().map(|p| p.1).max().unwrap() + 1;
            if members.len() != (r1 - r0) * (c1 - c0) {
                irregular = true;
                break;
            }
            let b = bbox(page, xs[c0], ys[r1], xs[c1], ys[r0]);
            let value = cell_text(text, &b, &m.ctx)?;
            cells.push(Cell {
                row: r0 + 1,
                column: c0 + 1,
                row_span: r1 - r0,
                column_span: c1 - c0,
                text: value,
                header_role: if r0 == 0 {
                    HeaderRole::Column
                } else {
                    HeaderRole::Unknown
                },
                role_origin: Origin::Heuristic,
                regions: vec![layout::region(page.number, b)],
                ..Cell::default()
            });
        }
        if irregular || cells.iter().all(|c| c.text.is_empty()) {
            continue;
        }
        cells.sort_by_key(|c| (c.row, c.column));
        out.push(finish(page, cells, rows, columns, outer, xs, m)?);
    }
    Ok(out)
}
fn numeric(text: &str) -> bool {
    text.chars().any(|c| c.is_ascii_digit())
        && text.chars().filter(|c| c.is_alphabetic()).count() <= 8
}
fn aligned(
    page: &Page,
    text: &PageText,
    excluded: &[Candidate],
    m: &mut Memory,
) -> Result<Vec<Candidate>, CoreError> {
    m.reserve(text.glyphs.len() * 512 + 1024)?;
    let lines = layout::lines(text, &m.ctx)?;
    let mut rows: Vec<Vec<Line>> = vec![];
    for line in lines {
        m.ctx.work(1)?;
        if excluded.iter().any(|c| {
            inside(
                &c.bbox,
                (line.bbox.x0 + line.bbox.x1) / 2.,
                (line.bbox.y0 + line.bbox.y1) / 2.,
            )
        }) {
            continue;
        }
        if let Some(row) = rows.last_mut()
            && (row[0].bbox.y0 - line.bbox.y0).abs() < line.size * 0.4
        {
            row.push(line);
        } else {
            rows.push(vec![line]);
        }
    }
    for row in &mut rows {
        row.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
    }
    let mut out = vec![];
    let mut i = 0;
    while i < rows.len() {
        let width = rows[i].len();
        if width < 2 {
            i += 1;
            continue;
        }
        let mut end = i + 1;
        while end < rows.len()
            && rows[end].len() == width
            && rows[end]
                .iter()
                .zip(&rows[i])
                .all(|(a, b)| (a.bbox.x0 - b.bbox.x0).abs() < 3.)
            && rows[end - 1][0].bbox.y0 - rows[end][0].bbox.y0 < rows[i][0].size * 4.
        {
            end += 1;
            m.ctx.work(width)?;
        }
        let candidates = &rows[i..end];
        let count = candidates.len();
        let header = candidates[0].iter().all(|l| {
            !numeric(&l.text)
                && l.text.split_whitespace().count() <= 4
                && l.text.chars().count() <= 40
        });
        let data_column = (0..width).any(|column| {
            candidates[1..]
                .iter()
                .filter(|row| numeric(&row[column].text))
                .count()
                * 2
                >= count - 1
        });
        if count >= 3 && header && data_column {
            m.reserve(count * width * 2048)?;
            m.ctx.work(count * width)?;
            let mut b = candidates[0][0].bbox.clone();
            let mut cells = vec![];
            for (r, row) in candidates.iter().enumerate() {
                for (c, line) in row.iter().enumerate() {
                    b = layout::union(&b, &line.bbox);
                    cells.push(Cell {
                        row: r + 1,
                        column: c + 1,
                        row_span: 1,
                        column_span: 1,
                        text: line.text.trim().to_owned(),
                        header_role: if r == 0 {
                            HeaderRole::Column
                        } else {
                            HeaderRole::Unknown
                        },
                        role_origin: Origin::Heuristic,
                        regions: vec![layout::region(page.number, line.bbox.clone())],
                        ..Cell::default()
                    });
                }
            }
            let mut edges: Vec<_> = candidates[0].iter().map(|l| l.bbox.x0).collect();
            edges.push(b.x1);
            out.push(finish(page, cells, count, width, b, edges, m)?);
        }
        i = end;
    }
    Ok(out)
}
pub(super) fn detect(
    page: &Page,
    text: &PageText,
    m: &mut Memory,
) -> Result<Vec<Candidate>, CoreError> {
    let mut out = ruled(page, text, m)?;
    let more = aligned(page, text, &out, m)?;
    out.extend(more);
    Ok(out)
}
pub(super) fn remaining(
    text: &PageText,
    candidates: &[Candidate],
    ctx: &Context,
) -> Result<PageText, CoreError> {
    ctx.work(text.glyphs.len() * candidates.len().max(1) / 32 + 1)?;
    Ok(PageText {
        glyphs: text
            .glyphs
            .iter()
            .filter(|g| {
                !candidates.iter().any(|c| {
                    inside(
                        &c.bbox,
                        (g.bbox.x0 + g.bbox.x1) / 2.,
                        (g.bbox.y0 + g.bbox.y1) / 2.,
                    )
                })
            })
            .cloned()
            .collect(),
        ..PageText::default()
    })
}

/// Exact local code-point segments for blocks spanning native pages.
pub(super) type Segments = BTreeMap<String, Vec<(usize, usize, usize)>>;
pub(super) fn join(
    document: &mut crate::canonical::Document,
    pages: &[Page],
    m: &mut Memory,
) -> Result<Segments, CoreError> {
    let mut segments = BTreeMap::new();
    let mut i = 0;
    while i + 1 < document.blocks.len() {
        m.ctx.work(1)?;
        let (a, b) = (&document.blocks[i], &document.blocks[i + 1]);
        let Some((left, right)) = a.table.as_ref().zip(b.table.as_ref()) else {
            i += 1;
            continue;
        };
        let last_page = a.regions.last().and_then(|r| r.number).unwrap();
        let next_page = b.regions[0].number.unwrap();
        let lb = a.regions.last().unwrap().bbox.as_ref().unwrap();
        let rb = b.regions[0].bbox.as_ref().unwrap();
        let headers = |t: &Table| {
            t.cells
                .iter()
                .filter(|c| c.row == 1)
                .map(|c| (c.column, c.column_span, c.text.clone()))
                .collect::<Vec<_>>()
        };
        let left_edges: Vec<_> = left
            .cells
            .iter()
            .filter(|c| c.row == 1)
            .filter_map(|c| c.regions[0].bbox.as_ref().map(|b| b.x0))
            .collect();
        let right_edges: Vec<_> = right
            .cells
            .iter()
            .filter(|c| c.row == 1)
            .filter_map(|c| c.regions[0].bbox.as_ref().map(|b| b.x0))
            .collect();
        if next_page != last_page + 1
            || left.columns != right.columns
            || headers(left) != headers(right)
            || lb.y0 > pages[last_page - 1].height * 0.3
            || rb.y1 < pages[next_page - 1].height * 0.7
            || left_edges.len() != right_edges.len()
            || left_edges
                .iter()
                .zip(&right_edges)
                .any(|(a, b)| (a - b).abs() > 3.)
        {
            i += 1;
            continue;
        }
        m.reserve(
            right
                .cells
                .iter()
                .map(|c| c.text.len() * 16 + 2048)
                .sum::<usize>()
                + left.cells.len() * 2048,
        )?;
        let mut next = document.blocks.remove(i + 1);
        let block = &mut document.blocks[i];
        let table = block.table.as_mut().unwrap();
        let row_offset = table.rows;
        let right = next.table.take().unwrap();
        for mut cell in right.cells {
            cell.row += row_offset;
            table.cells.push(cell);
        }
        table.rows += right.rows;
        block.regions.extend(next.regions);
        let (text, local) = render_table(table, &m.ctx)?;
        block.text = text;
        segments.insert(block.id.clone(), local);
    }
    Ok(segments)
}
pub(super) fn table_segments(
    document: &crate::canonical::Document,
    ctx: &Context,
) -> Result<Segments, CoreError> {
    let mut out = BTreeMap::new();
    for b in &document.blocks {
        if let Some(t) = &b.table {
            ctx.work(1)?;
            let (_, local) = render_table(t, ctx)?;
            out.insert(b.id.clone(), local);
        }
    }
    Ok(out)
}
