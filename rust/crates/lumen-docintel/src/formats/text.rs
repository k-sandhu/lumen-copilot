//! Text decoding, source lines and supplied Markdown roles; no execution.
use super::common::{Limits, Session, decoded, finish, generation, region};
use crate::{
    CoreError,
    canonical::{Block, BlockKind, Document},
};
use pulldown_cmark::{CodeBlockKind, Event, Parser, Tag};
use serde_json::{Value, json};

#[derive(Debug, Clone, Copy)]
pub enum Mode<'a> {
    Text,
    Markdown,
    Code(&'a str),
}
fn line(starts: &[usize], offset: usize) -> usize {
    starts.partition_point(|&n| n <= offset)
}

/// Used by notebooks after their JSON boundary has bounded each source string.
pub fn append_markdown(
    source: &str,
    doc: &mut Document,
    s: &mut Session,
    prefix: &str,
) -> Result<(), CoreError> {
    let starts: Vec<usize> = std::iter::once(0)
        .chain(source.match_indices('\n').map(|(n, _)| n + 1))
        .collect();
    s.reserve(starts.len().saturating_mul(16) + source.len().saturating_mul(4))?;
    let mut active: Option<(Block, usize, std::ops::Range<usize>)> = None;
    let mut depth = 0;
    let mut headings: Vec<(usize, String, String)> = vec![];
    for (event, range) in Parser::new(source).into_offset_iter() {
        s.ctx.work(1)?;
        match event {
            Event::Start(tag) => {
                depth += 1;
                if depth > s.limits.max_depth {
                    return Err(CoreError::Budget);
                }
                if active.is_none() {
                    let kind = match &tag {
                        Tag::Heading { .. } => Some(BlockKind::Heading),
                        Tag::Paragraph => Some(BlockKind::Paragraph),
                        Tag::Item => Some(BlockKind::List),
                        Tag::CodeBlock(_) => Some(BlockKind::Code),
                        _ => None,
                    };
                    if let Some(kind) = kind {
                        let mut b = Block {
                            kind,
                            heading_path: headings
                                .iter()
                                .map(|(_, text, _)| text.clone())
                                .collect(),
                            parent_id: headings.last().map(|(_, _, id)| id.clone()),
                            ..Block::default()
                        };
                        if let Tag::Heading { level, .. } = &tag {
                            b.heading_level = Some(*level as usize);
                        }
                        if let Tag::CodeBlock(CodeBlockKind::Fenced(info)) = &tag {
                            b.regions.push(region(format!(
                                "{prefix}language:{}",
                                info.split_whitespace().next().unwrap_or("unknown")
                            )));
                        }
                        active = Some((b, depth, range.clone()));
                    }
                }
            }
            Event::End(_) => {
                if active.as_ref().is_some_and(|(_, d, _)| *d == depth) {
                    let (mut b, _, source_range) = active.take().ok_or(CoreError::Internal)?;
                    b.regions.insert(
                        0,
                        region(format!(
                            "{prefix}lines:{}-{}",
                            line(&starts, source_range.start),
                            line(&starts, source_range.end.saturating_sub(1))
                        )),
                    );
                    if let Some(level) = b.heading_level {
                        while headings.last().is_some_and(|(l, _, _)| *l >= level) {
                            headings.pop();
                        }
                        b.parent_id = headings.last().map(|(_, _, id)| id.clone());
                        b.heading_path = headings
                            .iter()
                            .map(|(_, text, _)| text.clone())
                            .chain(std::iter::once(b.text.clone()))
                            .collect();
                        headings.push((
                            level,
                            b.text.clone(),
                            format!("b{}", doc.blocks.len() + 1),
                        ));
                    }
                    s.push(doc, b)?;
                }
                depth = depth.checked_sub(1).ok_or(CoreError::Parse)?;
            }
            Event::Text(t) | Event::Code(t) | Event::Html(t) | Event::InlineHtml(t) => {
                if let Some((b, _, _)) = active.as_mut() {
                    s.reserve(t.len().saturating_mul(4) + 64)?;
                    b.text.push_str(&t);
                    if b.text.len() > s.limits.budget.max_output_chars.saturating_mul(4) {
                        return Err(CoreError::Budget);
                    }
                } else if !t.trim().is_empty() {
                    s.push(
                        doc,
                        Block {
                            text: t.into_string(),
                            regions: vec![region(format!(
                                "{prefix}lines:{}-{}",
                                line(&starts, range.start),
                                line(&starts, range.end.saturating_sub(1))
                            ))],
                            ..Block::default()
                        },
                    )?;
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                if let Some((b, _, _)) = active.as_mut() {
                    b.text.push('\n');
                }
            }
            Event::Rule => s.push(
                doc,
                Block {
                    text: "---".into(),
                    regions: vec![region(format!(
                        "{prefix}lines:{}-{}",
                        line(&starts, range.start),
                        line(&starts, range.start)
                    ))],
                    ..Block::default()
                },
            )?,
            _ => {}
        }
    }
    Ok(())
}
pub fn parse(bytes: &[u8], limits: Limits, mode: Mode<'_>) -> Result<Document, CoreError> {
    let mut s = Session::new(bytes, limits)?;
    let decoded = decoded(bytes, &mut s)?;
    s.reserve(decoded.text.len().saturating_mul(4))?;
    let source = decoded.text.replace("\r\n", "\n").replace('\r', "\n");
    let mut doc = Document {
        generation: generation(bytes, "rust-text", include_str!("text.rs")),
        ..Document::default()
    };
    let mut language: Value = Value::Null;
    match mode {
        Mode::Markdown => append_markdown(&source, &mut doc, &mut s, "")?,
        Mode::Code(lang) => {
            language = json!(lang);
            s.push(
                &mut doc,
                Block {
                    kind: BlockKind::Code,
                    text: source.clone(),
                    regions: vec![
                        region(format!("lines:1-{}", source.lines().count().max(1))),
                        region(format!("language:{lang}")),
                    ],
                    ..Block::default()
                },
            )?;
        }
        Mode::Text => {
            let mut start = 1;
            let mut buffer = String::new();
            let mut end = 0;
            for (index, line) in source.split('\n').enumerate() {
                s.ctx.work(1)?;
                if line.trim().is_empty() {
                    if !buffer.is_empty() {
                        s.push(
                            &mut doc,
                            Block {
                                text: std::mem::take(&mut buffer),
                                regions: vec![region(format!("lines:{start}-{end}"))],
                                ..Block::default()
                            },
                        )?;
                    }
                } else {
                    s.reserve(line.len().saturating_mul(4) + 128)?;
                    if buffer.is_empty() {
                        start = index + 1;
                    } else {
                        buffer.push('\n');
                    }
                    buffer.push_str(line);
                    end = index + 1;
                    if buffer.len() > limits.budget.max_output_chars.saturating_mul(4) {
                        return Err(CoreError::Budget);
                    }
                }
            }
            if !buffer.is_empty() {
                s.push(
                    &mut doc,
                    Block {
                        text: buffer,
                        regions: vec![region(format!("lines:{start}-{end}"))],
                        ..Block::default()
                    },
                )?;
            }
        }
    }
    doc.generation
        .dependency_versions
        .insert("pulldown-cmark".into(), "0.13.4".into());
    finish(
        &mut doc,
        &mut s,
        json!({"encoding":decoded.encoding,"encoding_evidence":decoded.evidence,"decoding_errors":decoded.errors,"language":language,"line_endings":"LF"}),
        decoded.errors > 0,
    )?;
    Ok(doc)
}
