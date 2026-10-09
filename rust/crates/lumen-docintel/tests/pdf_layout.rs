mod pdf_support;
use lumen_docintel_core::{CoreError, canonical, formats::pdf, runtime::Budget};
use pdf_support::{pdf_bytes, text};

#[test]
fn columns_sidebar_footnote_heading_and_page_boxes() {
    // Deliberately interleaved painting order differs from reading order.
    let content = [
        text(40, 740, 22, "Title"),
        text(40, 680, 12, "LeftOne"),
        text(280, 680, 12, "RightOne"),
        text(470, 680, 10, "Sidebar"),
        text(40, 650, 12, "LeftTwo"),
        text(280, 650, 12, "RightTwo"),
        text(40, 60, 8, "Footnote"),
    ]
    .join("\n");
    let doc = pdf::extract(&pdf_bytes(&[content.as_bytes()], 0), Budget::default()).unwrap();
    let rendered = canonical::render(doc).unwrap();
    let anchors = [
        "Title", "LeftOne", "LeftTwo", "RightOne", "RightTwo", "Sidebar", "Footnote",
    ];
    let positions: Vec<_> = anchors
        .iter()
        .map(|s| rendered.rendered_text.find(s).unwrap())
        .collect();
    assert!(
        positions.windows(2).all(|p| p[0] < p[1]),
        "{}",
        rendered.rendered_text
    );
    assert_eq!(
        rendered.document.blocks[0].kind,
        canonical::BlockKind::Heading
    );
    assert!(
        rendered
            .document
            .blocks
            .iter()
            .all(|b| b.regions[0].number == Some(1) && b.regions[0].bbox.is_some())
    );
    assert!(
        rendered
            .document
            .blocks
            .iter()
            .any(|b| b.kind == canonical::BlockKind::Footnote)
    );
}

#[test]
fn empty_and_mixed_pages_are_typed_needs_ocr_not_empty_success() {
    let doc = pdf::extract(&pdf_bytes(&[b"q Q"], 0), Budget::default()).unwrap();
    assert_eq!(doc.generation.outcome.as_deref(), Some("needs_ocr"));
    assert_eq!(
        doc.generation.diagnostics.as_ref().unwrap()["pages"][0]["outcome"],
        "needs_ocr"
    );
    let doc = pdf::extract(
        &pdf_bytes(&[text(40, 700, 12, "Digital").as_bytes(), b"q Q"], 0),
        Budget::default(),
    )
    .unwrap();
    assert_eq!(doc.generation.outcome.as_deref(), Some("partial"));
    assert_eq!(doc.source_parts.len(), 2);
}

#[test]
fn rotated_page_retains_source_coordinates_and_rotation() {
    let doc = pdf::extract(
        &pdf_bytes(&[text(40, 700, 12, "Rotated").as_bytes()], 90),
        Budget::default(),
    )
    .unwrap();
    assert_eq!(
        doc.generation.diagnostics.as_ref().unwrap()["pages"][0]["rotation"],
        90
    );
    assert_eq!(
        doc.blocks[0].regions[0].bbox.as_ref().unwrap().origin,
        canonical::CoordinateOrigin::BottomLeft
    );
}

#[test]
fn repeated_margins_are_retained_furniture_and_identifiers_unchanged() {
    let content = [
        text(40, 770, 10, "RepeatedHeader"),
        text(40, 700, 12, "AB-"),
        text(40, 680, 12, "123"),
        text(40, 20, 8, "RepeatedFooter"),
    ]
    .join("\n");
    let doc = pdf::extract(
        &pdf_bytes(&[content.as_bytes(), content.as_bytes()], 0),
        Budget::default(),
    )
    .unwrap();
    assert_eq!(
        doc.blocks
            .iter()
            .filter(|b| b.kind == canonical::BlockKind::Furniture)
            .count(),
        4
    );
    let rendered = canonical::render(doc).unwrap();
    assert!(rendered.rendered_text.contains("AB-"));
    assert!(rendered.rendered_text.contains("123"));
}

#[test]
fn corrupt_encrypted_and_limits_fail_closed_and_next_document_succeeds() {
    assert_eq!(
        pdf::extract(b"%PDF-1.7 broken", Budget::default()),
        Err(CoreError::Parse)
    );
    let valid = pdf_bytes(&[text(40, 700, 12, "Healthy").as_bytes()], 0);
    let tiny = Budget {
        max_memory_bytes: 100,
        ..Budget::default()
    };
    assert_eq!(pdf::extract(&valid, tiny), Err(CoreError::Budget));
    let tiny = Budget {
        max_work_units: 1,
        ..Budget::default()
    };
    assert_eq!(pdf::extract(&valid, tiny), Err(CoreError::Budget));
    let expired = Budget {
        timeout_ms: 0,
        ..Budget::default()
    };
    assert_eq!(pdf::extract(&valid, expired), Err(CoreError::Budget));
    assert!(pdf::extract(&valid, Budget::default()).is_ok());
}

proptest::proptest! {
    #[test]
    fn offsets_and_boxes_match_exact_source(text_value in "[a-zA-Z0-9 ]{1,80}", pages in 1usize..5) {
        let stream=text(40,700,10,&text_value);
        let contents:Vec<_>=(0..pages).map(|_|stream.as_bytes()).collect();
        let rendered=canonical::render(pdf::extract(&pdf_bytes(&contents,0),Budget::default()).unwrap()).unwrap();
        for (span,block) in rendered.spans.iter().zip(&rendered.document.blocks) {
            let slice:String=rendered.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect();
            proptest::prop_assert_eq!(&slice,&block.text);
            let region=&block.regions[0];
            proptest::prop_assert!(region.number.unwrap()<=pages);
            let bbox=region.bbox.as_ref().unwrap();
            proptest::prop_assert!(bbox.x0>=0.0 && bbox.x1>=bbox.x0 && bbox.y1>=bbox.y0);
        }
    }
}

#[test]
fn metadata_outline_and_soft_hyphen_normalization_retain_source_evidence() {
    let content = text(40, 700, 12, "Body");
    let input=pdf_support::objects(&[
        b"<< /Type /Catalog /Pages 2 0 R /Outlines 6 0 R >>".to_vec(),
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>".to_vec(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>".to_vec(),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>".to_vec(),
        format!("<< /Length {} >>\nstream\n{content}\nendstream",content.len()).into_bytes(),
        b"<< /Type /Outlines /First 7 0 R /Last 7 0 R >>".to_vec(),
        b"<< /Title (Section) /Parent 6 0 R /Dest [4 0 R /Fit] >>".to_vec(),
        b"<< /Title (Synthetic) /Author (Fixture) >>".to_vec(),
    ],"/Info 8 0 R");
    let doc = pdf::extract(&input, Budget::default()).unwrap();
    let diagnostics = doc.generation.diagnostics.as_ref().unwrap();
    assert_eq!(diagnostics["metadata"]["Title"], "Synthetic");
    assert_eq!(diagnostics["outline"][0]["title"], "Section");
    let doc = pdf::extract(
        &pdf_support::unicode_pdf("co\u{ad}\noperation"),
        Budget::default(),
    )
    .unwrap();
    assert!(doc.blocks[0].text.contains('\u{ad}'));
    assert_eq!(
        doc.generation.diagnostics.as_ref().unwrap()["normalization_hooks"]["normalized_blocks"][0]
            ["text"],
        "cooperation"
    );
}

proptest::proptest! {
    #[test]
    fn unicode_codepoint_offsets_survive_font_maps(value in "[a-z世界مرحبا😀é]{1,40}") {
        let rendered=canonical::render(pdf::extract(&pdf_support::unicode_pdf(&value),Budget::default()).unwrap()).unwrap();
        proptest::prop_assert_eq!(&rendered.rendered_text,&value);
        proptest::prop_assert_eq!(rendered.spans[0].char_end,value.chars().count());
    }
}
