use lumen_docintel_core::canonical::{Block, BlockKind, Cell, Document, HeaderRole, Table};
use lumen_docintel_core::chunking::{ChunkSettings, chunk_document};
use lumen_docintel_core::runtime::{Budget, Cancellation, Context};
use lumen_docintel_core::CoreError;
use proptest::prelude::*;

fn tokenizer() -> String {
    // Generated character BPE. No model artifacts or network in tests.
    let mut vocab = serde_json::Map::new();
    vocab.insert("[UNK]".into(), 0.into());
    for (n, c) in "ab .!?\n\t0123456789$kgé🙂中".chars().enumerate() {
        vocab.insert(c.to_string(), (n + 1).into());
    }
    serde_json::json!({"version":"1.0","truncation":null,"padding":null,
        "added_tokens":[],"normalizer":null,"pre_tokenizer":null,
        "post_processor":null,"decoder":null,
        "model":{"type":"BPE","dropout":null,"unk_token":"[UNK]",
        "continuing_subword_prefix":null,"end_of_word_suffix":null,
        "fuse_unk":false,"byte_fallback":false,"vocab":vocab,"merges":[]}}).to_string()
}
fn context() -> Context { Context::new(Budget::default(), Cancellation::default()).unwrap() }
fn doc(text: String) -> Document {
    Document { blocks: vec![Block { id: "body".into(), text, ..Default::default() }], ..Default::default() }
}
fn settings(size: usize, overlap: usize) -> ChunkSettings {
    ChunkSettings { max_tokens: size, max_chars: size, overlap_chars: overlap, embedding_model: "fixture".into() }
}

#[test]
fn early_sentence_boundary_retains_exact_overlap() {
    let d = doc(format!("{}. {}", "a".repeat(800), "b".repeat(1500)));
    let result = chunk_document(d, &tokenizer(), &settings(1200, 200), &context()).unwrap();
    assert_eq!(result.chunks.iter().map(|c| (c.char_start,c.char_end)).collect::<Vec<_>>(), vec![(0,801),(601,1801),(1601,2302)]);
    assert!(result.rendered.document.generation.tokenizer_id.is_some());
    assert!(result.rendered.document.generation.fingerprint.is_some());
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]
    #[test]
    fn exact_unicode_coverage_and_progress(chars in prop::collection::vec(prop::sample::select(vec!['a','b','é','🙂','中','.','\n',' ']), 1..250), size in 8usize..70, ratio in 0usize..8) {
        let source: String = chars.iter().collect();
        let overlap = ratio.min(size-1);
        let result = chunk_document(doc(source.clone()), &tokenizer(), &settings(size,overlap), &context()).unwrap();
        let mut covered = vec![false; chars.len()];
        for (n,c) in result.chunks.iter().enumerate() {
            prop_assert_eq!(c.ord,n);
            prop_assert_eq!(&c.text,&chars[c.char_start..c.char_end].iter().collect::<String>());
            prop_assert!(c.token_count<=size);
            covered[c.char_start..c.char_end].fill(true);
        }
        for pair in result.chunks.windows(2) {
            prop_assert!(pair[0].char_start<pair[1].char_start);
            prop_assert!(pair[0].char_end<pair[1].char_end);
            // Omitted blank windows can reduce returned overlap.
            prop_assert!(pair[1].char_start+overlap>=pair[0].char_end);
        }
        prop_assert!(chars.iter().zip(covered).all(|(c,seen)| c.is_whitespace()||seen));
    }
}

#[test]
fn table_rows_repeat_headers_and_units_separate_from_evidence() {
    let cell = |row,column,text:&str,header_role,unit| Cell { row,column,text:text.into(),header_role,unit,row_span:1,column_span:1,..Default::default() };
    let table = Table { rows:3, columns:2, cells:vec![cell(1,1,"a",HeaderRole::Column,None),cell(1,2,"kg",HeaderRole::Column,None),cell(2,1,"b",HeaderRole::Row,None),cell(2,2,"12",HeaderRole::Unknown,Some("kg".into())),cell(3,1,"a",HeaderRole::Row,None),cell(3,2,"34",HeaderRole::Unknown,Some("kg".into()))],caption:None };
    let d = Document { blocks:vec![Block { id:"t".into(),kind:BlockKind::Table,text:"a\tkg\nb\t12\na\t34".into(),table:Some(table),..Default::default() }],..Default::default() };
    let result = chunk_document(d.clone(), &tokenizer(), &settings(64,0), &context()).unwrap();
    assert_eq!(result.chunks.len(),3);
    assert!(result.chunks[1].context.contains("kg"));
    assert!(result.chunks[2].context.contains("kg"));
    assert_eq!(result.chunks[1].text,"b\t12\n");
    assert_eq!(chunk_document(d,&tokenizer(), &settings(3,0), &context()).unwrap_err(),CoreError::Budget);
}

#[test]
fn invalid_settings_and_cancelled_work_are_typed() {
    assert_eq!(chunk_document(doc("a".into()), &tokenizer(), &settings(0,0), &context()).unwrap_err(),CoreError::InvalidInput);
    let token = Cancellation::default(); token.cancel();
    let ctx = Context::new(Budget::default(),token).unwrap();
    assert_eq!(chunk_document(doc("a".into()), &tokenizer(), &settings(10,0), &ctx).unwrap_err(),CoreError::Cancelled);
}
