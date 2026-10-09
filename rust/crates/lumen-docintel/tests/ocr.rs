use lumen_docintel_core::{
    canonical::{self, Document, Generation, PartKind, SourcePart},
    ocr::{OcrText, merge},
};
use proptest::prelude::*;
use serde_json::json;

fn scanned() -> Document {
    Document {
        source_parts: vec![SourcePart {
            kind: PartKind::Page,
            name: "Page 1".into(),
            number: 1,
            char_start: 0,
            char_end: 0,
        }],
        generation: Generation {
            outcome: Some("needs_ocr".into()),
            diagnostics: Some(json!({"pages":[{"number":1,"outcome":"needs_ocr"}]})),
            ..Default::default()
        },
        ..Default::default()
    }
}
proptest! {
    #[test]
    fn ocr_preserves_codepoint_offsets(text in "[a-zé😀\\u{0301}]{1,120}") {
        let output=merge(scanned(),vec![OcrText {page:1,text:text.clone(),engine_id:"fixture".into(),confidence:None}]).unwrap();
        let r=canonical::render(output).unwrap();
        prop_assert_eq!(&r.rendered_text,&text);
        prop_assert_eq!(r.spans[0].char_end,text.chars().count());
        prop_assert_eq!(r.document.source_parts[0].char_end,text.chars().count());
        prop_assert_eq!(&r.document.blocks[0].origin, &canonical::Origin::Ocr);
    }
}
#[test]
fn refuses_digital_pages_and_duplicate_results() {
    let result = OcrText {
        page: 1,
        text: "scan".into(),
        engine_id: "fixture".into(),
        confidence: None,
    };
    let mut doc = scanned();
    doc.generation.diagnostics = Some(json!({"pages":[{"number":1,"outcome":"extracted"}]}));
    assert!(merge(doc, vec![result.clone()]).is_err());
    assert!(merge(scanned(), vec![result.clone(), result]).is_err());
}

#[test]
fn image_codecs_produce_one_page_pdf_and_obey_budgets() {
    use lumen_docintel_core::{
        ocr_image,
        runtime::{Budget, Cancellation, Context},
    };
    use std::io::Cursor;
    for format in [
        image::ImageFormat::Png,
        image::ImageFormat::Jpeg,
        image::ImageFormat::WebP,
        image::ImageFormat::Tiff,
    ] {
        let image = image::DynamicImage::ImageRgb8(image::RgbImage::from_pixel(
            2,
            2,
            image::Rgb([30, 40, 50]),
        ));
        let mut bytes = Cursor::new(Vec::new());
        image.write_to(&mut bytes, format).unwrap();
        let ctx = Context::new(Budget::default(), Cancellation::default()).unwrap();
        let result =
            ocr_image::wrap(bytes.get_ref(), &ctx).unwrap_or_else(|e| panic!("{format:?}: {e:?}"));
        let raw: serde_json::Value = serde_json::from_str(&result).unwrap();
        assert_eq!(raw["page"], 1);
        assert!(raw["pdf_hex"].as_str().unwrap().starts_with("255044462d"));
        let tiny = Context::new(
            Budget {
                max_memory_bytes: 1,
                ..Budget::default()
            },
            Cancellation::default(),
        )
        .unwrap();
        assert!(ocr_image::wrap(bytes.get_ref(), &tiny).is_err());
    }
}
