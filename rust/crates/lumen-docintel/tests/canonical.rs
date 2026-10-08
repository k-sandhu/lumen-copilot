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
