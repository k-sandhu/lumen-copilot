use lumen_docintel_core::CoreError;
use lumen_docintel_core::canonical::{Block, BlockKind, Document, render};
use proptest::prelude::*;

fn document(texts: Vec<String>) -> Document {
    Document {
        blocks: texts
            .into_iter()
            .enumerate()
            .map(|(n, text)| Block {
                id: format!("b{n}"),
                text,
                ..Block::default()
            })
            .collect(),
        ..Document::default()
    }
}

#[test]
fn rejects_cycles_orphans_duplicate_ids_and_wrong_heading() {
    let mut doc = document(vec!["one".into(), "two".into()]);
    doc.blocks[0].parent_id = Some("b1".into());
    doc.blocks[1].parent_id = Some("b0".into());
    assert_eq!(render(doc).unwrap_err(), CoreError::InvalidInput);
    let mut doc = document(vec!["one".into()]);
    doc.blocks[0].parent_id = Some("absent".into());
    assert_eq!(render(doc).unwrap_err(), CoreError::InvalidInput);
    let mut doc = document(vec!["one".into(), "two".into()]);
    doc.blocks[1].id = "b0".into();
    assert_eq!(render(doc).unwrap_err(), CoreError::InvalidInput);
    let mut doc = document(vec!["one".into()]);
    doc.blocks[0].kind = BlockKind::Heading;
    assert_eq!(render(doc).unwrap_err(), CoreError::InvalidInput);
}

proptest! {
    #[test]
    fn unicode_offsets_are_exact_nonoverlapping_and_roundtrip(
        texts in prop::collection::vec(prop::collection::vec(any::<char>(),0..80),0..20)
    ) {
        let texts: Vec<String> = texts.into_iter().map(|chars| chars.into_iter().collect()).collect();
        let rendered = render(document(texts.clone())).unwrap();
        let mut end = 0;
        for (span, text) in rendered.spans.iter().zip(&texts) {
            prop_assert!(span.char_start >= end);
            prop_assert!(span.char_end <= rendered.rendered_text.chars().count());
            let slice: String = rendered.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect();
            prop_assert_eq!(&slice, text);
            end = span.char_end;
        }
        let json = serde_json::to_string(&rendered.document).unwrap();
        let restored: Document = serde_json::from_str(&json).unwrap();
        prop_assert_eq!(render(restored).unwrap(), rendered);
    }
}

#[test]
fn metadata_and_sparse_spanned_table_roundtrip() {
    let input = r#"{"blocks":[{"id":"t","kind":"table","text":"合計 12 kg","table":{"rows":2,"columns":3,"cells":[{"row":1,"column":1,"row_span":1,"column_span":2,"text":"合計","header_role":"column","role_origin":"source"},{"row":2,"column":2,"row_span":1,"column_span":1,"text":"12","unit":"kg","formula":"=A1","cached_value":12,"cache_freshness":"unknown"}]},"regions":[{"kind":"sheet","number":1,"name":"Data","cell_range":{"row_start":1,"row_end":2,"column_start":1,"column_end":3}}]}],"generation":{"fingerprint":{"schema_version":1},"outcome":"partial","diagnostics":{"warning_codes":["blank_part"]}}}"#;
    let rendered: lumen_docintel_core::canonical::RenderedDocument =
        serde_json::from_str(&lumen_docintel_core::canonical::render_json(input).unwrap()).unwrap();
    let restored =
        render(serde_json::from_str(&serde_json::to_string(&rendered.document).unwrap()).unwrap())
            .unwrap();
    assert_eq!(rendered, restored);
    assert_eq!(rendered.spans[0].char_end, 8);
}

#[test]
fn bad_geometry_spans_and_overlapping_cells_fail_closed() {
    for input in [
        r#"{"blocks":[{"id":"b","regions":[{"kind":"page","number":0}]}]}"#,
        r#"{"blocks":[{"id":"b","regions":[{"bbox":{"x0":4,"x1":1,"y0":0,"y1":1,"unit":"point","origin":"top_left"}}]}]}"#,
        r#"{"blocks":[{"id":"b","kind":"table","table":{"rows":2,"columns":2,"cells":[{"row":1,"column":1,"row_span":2,"column_span":2},{"row":2,"column":2,"row_span":1,"column_span":1}]}}]}"#,
        r#"{"blocks":[{"id":"b","text":"abc"}],"source_parts":[{"kind":"page","name":"p","number":1,"char_start":0,"char_end":4}]}"#,
    ] {
        assert_eq!(
            lumen_docintel_core::canonical::render_json(input),
            Err(CoreError::InvalidInput)
        );
    }
}

#[test]
fn compact_json_cannot_expand_unbounded_model_collections() {
    let values = std::iter::repeat_n("null", 100_001)
        .collect::<Vec<_>>()
        .join(",");
    let input = format!(r#"{{"generation":{{"diagnostics":[{values}]}}}}"#);
    assert!(matches!(
        lumen_docintel_core::canonical::render_json(&input),
        Err(CoreError::Budget)
    ));
    let cells = std::iter::repeat_n("{}", 100_001)
        .collect::<Vec<_>>()
        .join(",");
    let input = format!(
        r#"{{"blocks":[{{"id":"table","kind":"table","table":{{"rows":1,"columns":1,"cells":[{cells}]}}}}]}}"#
    );
    assert!(matches!(
        lumen_docintel_core::canonical::render_json(&input),
        Err(CoreError::Budget)
    ));
}
