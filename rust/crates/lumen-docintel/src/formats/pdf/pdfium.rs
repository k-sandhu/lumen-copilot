//! Non-cooperative native engine. Call ONLY in an OS-limited worker process.
//! No network or source-path access. The library path is inert build configuration.
use super::{
    Memory, Page, assemble, serialize,
    syntax::Value,
    text::{Glyph, PageText},
};
use crate::{
    CoreError,
    canonical::{BoundingBox, CoordinateOrigin, CoordinateUnit},
    runtime::Context,
};
use pdfium_render::prelude::*;
use serde_json::json;
use std::collections::BTreeMap;

fn error(e: PdfiumError) -> CoreError {
    match e {
        PdfiumError::PdfiumLibraryInternalError(
            PdfiumInternalError::PasswordError | PdfiumInternalError::SecurityError,
        ) => CoreError::Encrypted,
        _ => CoreError::Parse,
    }
}

pub fn extract_json(bytes: &[u8], library: &str, ctx: &Context) -> Result<String, CoreError> {
    ctx.input(bytes.len())?;
    if !bytes.starts_with(b"%PDF-") {
        return Err(CoreError::InvalidInput);
    }
    let mut memory = Memory::new(ctx.clone());
    memory.reserve(bytes.len())?;
    let bindings = Pdfium::bind_to_library(library).map_err(|_| CoreError::Unsupported)?;
    let pdfium = Pdfium::new(bindings);
    let document = pdfium
        .load_pdf_from_byte_slice(bytes, None)
        .map_err(error)?;
    if !matches!(
        document.permissions().security_handler_revision(),
        Ok(PdfSecurityHandlerRevision::Unprotected)
    ) {
        return Err(CoreError::Encrypted);
    }
    let mut values = BTreeMap::new();
    for tag in document.metadata().iter() {
        ctx.work(1)?;
        memory.reserve(tag.value().len() * 8 + 128)?;
        values.insert(format!("{:?}", tag.tag_type()), tag.value().to_string());
    }
    let mut outline = vec![];
    for bookmark in document.bookmarks().iter() {
        ctx.work(1)?;
        let title = bookmark.title().unwrap_or_default();
        let mut depth = 1;
        let mut parent = bookmark.parent();
        while let Some(node) = parent {
            ctx.work(1)?;
            depth += 1;
            if depth > 32 {
                return Err(CoreError::Budget);
            }
            parent = node.parent();
        }
        memory.reserve(title.len() * 8 + 256)?;
        outline.push(json!({"title":title,"depth":depth,"destination_page":bookmark.destination().and_then(|d| d.page_index().ok()).map(|i|i as usize+1)}));
    }
    let null = Value::Null;
    let mut pages = vec![];
    let mut texts = vec![];
    for (index, page) in document.pages().iter().enumerate() {
        ctx.work(1)?;
        memory.reserve(1024)?;
        let rotation = match page.rotation().map_err(error)? {
            PdfPageRenderRotation::None => 0,
            PdfPageRenderRotation::Degrees90 => 90,
            PdfPageRenderRotation::Degrees180 => 180,
            PdfPageRenderRotation::Degrees270 => 270,
        };
        let (mut width, mut height) = (page.width().value as f64, page.height().value as f64);
        if rotation == 90 || rotation == 270 {
            std::mem::swap(&mut width, &mut height);
        }
        if !width.is_finite() || !height.is_finite() || width <= 0. || height <= 0. {
            return Err(CoreError::Parse);
        }
        let mut out = PageText::default();
        let text = page.text().map_err(error)?;
        let mut unusable = false;
        let mut high_surrogate: Option<u16> = None;
        for ch in text.chars().iter() {
            ctx.work(1)?;
            // PDFium's synthetic breaks can have no Unicode mapping. They are
            // layout artifacts, not evidence of an unmapped source glyph.
            if ch.is_generated().map_err(error)? {
                continue;
            }
            // PDFium replaces recognized line-end hyphens with U+0002.
            // Restore an explicit hyphen before our conservative dehyphenation
            // policy, preserving identifiers such as AB- / 123.
            let raw = if ch.unicode_value() == 2 && ch.is_hyphen().map_err(error)? {
                '-' as u32
            } else {
                ch.unicode_value()
            };
            // PDFium may expose a supplementary scalar as two UTF-16 entries.
            if (0xd800..=0xdbff).contains(&raw) {
                high_surrogate = Some(raw as u16);
                continue;
            }
            let scalar = if let Some(high) = high_surrogate.take() {
                if !(0xdc00..=0xdfff).contains(&raw) {
                    unusable = true;
                    continue;
                }
                char::from_u32(0x10000 + ((high as u32 - 0xd800) << 10) + raw - 0xdc00)
            } else {
                char::from_u32(raw)
            };
            let Some(c) = scalar else {
                unusable = true;
                continue;
            };
            if c == '\0'
                || c == '\u{fffd}'
                || matches!(c as u32, 0xfffe..=0xffff | 0xe000..=0xf8ff | 0xf0000..=0xffffd | 0x100000..=0x10fffd)
            {
                unusable = true;
                continue;
            }
            if c.is_control() {
                if !c.is_whitespace() {
                    unusable = true;
                }
                continue;
            }
            // Page text excludes annotation appearances.
            let _object = ch.text_object().map_err(error)?;
            let rect = ch.loose_bounds().map_err(error)?;
            let size = ch.scaled_font_size().value as f64;
            let baseline = ch.origin_y().map_err(error)?.value as f64;
            let bbox = BoundingBox {
                x0: rect.left().value as f64,
                y0: rect.bottom().value as f64,
                x1: rect.right().value as f64,
                y1: rect.top().value as f64,
                unit: CoordinateUnit::Point,
                origin: CoordinateOrigin::BottomLeft,
            };
            if ![bbox.x0, bbox.y0, bbox.x1, bbox.y1, size, baseline]
                .iter()
                .all(|v| v.is_finite())
                || size <= 0.
            {
                unusable = true;
                continue;
            }
            ctx.output(1)?;
            memory.reserve(512)?;
            out.glyphs.push(Glyph {
                text: c.to_string(),
                bbox,
                size,
                bold: matches!(
                    ch.font_weight(),
                    Some(
                        PdfFontWeight::Weight700Bold
                            | PdfFontWeight::Weight800
                            | PdfFontWeight::Weight900
                    )
                ) || ch.font_is_bold_reenforced(),
                baseline: Some(baseline),
                source_order: Some(ch.index()),
            });
        }
        unusable |= high_surrogate.is_some();
        if unusable {
            out.glyphs.clear();
            out.needs_ocr = true;
        }
        for object in page.objects().iter() {
            rules(&object, PdfMatrix::IDENTITY, 0, &mut out, &mut memory)?;
        }
        pages.push(Page {
            number: index + 1,
            width,
            height,
            rotation,
            resources: &null,
            contents: None,
        });
        texts.push(out);
    }
    if pages.is_empty() {
        return Err(CoreError::Parse);
    }
    let result = assemble(
        bytes,
        &pages,
        &texts.iter().collect::<Vec<_>>(),
        json!({"metadata":values,"outline":outline}),
        ctx,
        &mut memory,
        true,
    )?;
    serialize(result, bytes.len(), ctx)
}

fn rules(
    object: &PdfPageObject<'_>,
    parent: PdfMatrix,
    depth: usize,
    out: &mut PageText,
    memory: &mut Memory,
) -> Result<(), CoreError> {
    memory.ctx.work(1)?;
    if depth > 32 {
        return Err(CoreError::Budget);
    }
    if object.as_image_object().is_some() {
        out.has_images = true;
    }
    if let Some(form) = object.as_x_object_form_object() {
        let matrix = form.matrix().map_err(error)?.multiply(parent);
        for index in form.as_range() {
            rules(
                &form.get(index).map_err(error)?,
                matrix,
                depth + 1,
                out,
                memory,
            )?;
        }
    }
    if let Some(path) = object.as_path_object() {
        if !path.is_stroked().map_err(error)? {
            return Ok(());
        }
        let segments = path
            .segments()
            .transform(path.matrix().map_err(error)?.multiply(parent));
        let mut previous = None;
        let mut first = None;
        for segment in segments.iter() {
            memory.ctx.work(1)?;
            let (x, y) = segment.point();
            let point = (x.value as f64, y.value as f64);
            if !point.0.is_finite() || !point.1.is_finite() {
                return Err(CoreError::Parse);
            }
            if segment.segment_type() == PdfPathSegmentType::MoveTo {
                first = Some(point);
            }
            if segment.segment_type() == PdfPathSegmentType::LineTo
                && let Some((x, y)) = previous
            {
                memory.reserve(64)?;
                out.lines.push([x, y, point.0, point.1]);
            }
            if segment.is_close()
                && let Some((x, y)) = first
            {
                memory.reserve(64)?;
                out.lines.push([point.0, point.1, x, y]);
            }
            previous = Some(point);
        }
    }
    Ok(())
}
