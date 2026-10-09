use lumen_docintel_core::canonical::{Block,BlockKind,Document,PartKind,SourcePart,SourceRegion};
use lumen_docintel_core::normalization::normalize_document;
use lumen_docintel_core::runtime::{Budget,Cancellation,Context};
use lumen_docintel_core::CoreError;
use proptest::prelude::*;
fn ctx()->Context {Context::new(Budget::default(),Cancellation::default()).unwrap()}
fn block(id:&str,text:&str)->Block {Block{id:id.into(),text:text.into(),..Default::default()}}

#[test]
fn normalize_prose_without_changing_identifiers_numbers_or_units() {
    let source="ofﬁce  infor\u{ad}\nmation A-123\nB 12.50 kg µg 1e-3 AB-\n123 state-of-the-art infor-\nmation";
    let d=Document{blocks:vec![block("a",source)],..Default::default()};
    let result=normalize_document(d,&ctx()).unwrap();
    assert_eq!(result.rendered.rendered_text,source);
    let text=&result.normalized_blocks[0].text;
    assert!(text.starts_with("office information"));
    for protected in ["A-123\nB","12.50 kg µg 1e-3","AB-\n123","state-of-the-art","infor-\nmation"] {assert!(text.contains(protected),"{protected}");}
    assert!(result.diagnostics.warnings.contains(&"ambiguous_hyphenation".into()));
}

#[test]
fn furniture_needs_distinct_pages_and_never_removes_body() {
    let furniture=|id:&str,page|Block{id:id.into(),text:"Confidential".into(),kind:BlockKind::Furniture,regions:vec![SourceRegion{kind:Some(PartKind::Page),number:page,..Default::default()}],..Default::default()};
    let mut d=Document{blocks:vec![furniture("h1",Some(1)),block("body","Confidential"),furniture("h2",Some(2)),furniture("unknown",None)],..Default::default()};
    d.blocks[3].regions.clear();
    let result=normalize_document(d,&ctx()).unwrap();
    assert_eq!(result.diagnostics.excluded_block_ids,vec!["h1","h2"]);
    assert_eq!(result.rendered.spans.len(),4);
    let result=normalize_document(Document{blocks:vec![furniture("a",Some(1)),furniture("b",Some(1))],..Default::default()},&ctx()).unwrap();
    assert!(result.diagnostics.excluded_block_ids.is_empty());
}

#[test]
fn empty_and_garbled_pages_are_not_success_claims() {
    let d=Document{blocks:vec![block("a","�Ã�\u{0001}")],source_parts:vec![SourcePart{kind:PartKind::Page,name:"1".into(),number:1,char_start:0,char_end:4},SourcePart{kind:PartKind::Page,name:"2".into(),number:2,char_start:4,char_end:4}],..Default::default()};
    let result=normalize_document(d,&ctx()).unwrap();
    assert_eq!(result.diagnostics.outcome,"partial");
    assert!(result.diagnostics.pages[0].garbled_ratio>0.5);
    assert!(result.diagnostics.pages[1].warnings.contains(&"no_native_text_layer".into()));
    assert_eq!(result.diagnostics.pages[1].language,None);
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]
    #[test]
    fn original_evidence_is_never_normalized(chars in prop::collection::vec(any::<char>(),0..200)) {
        let source:String=chars.iter().collect();
        let result=normalize_document(Document{blocks:vec![block("a",&source)],..Default::default()},&ctx()).unwrap();
        prop_assert_eq!(&result.rendered.rendered_text,&source);
        for span in result.rendered.spans {prop_assert_eq!(chars[span.char_start..span.char_end].iter().collect::<String>(),source.clone());}
    }
}

#[test]
fn cancellation_is_typed() {
    let token=Cancellation::default();token.cancel();
    let context=Context::new(Budget::default(),token).unwrap();
    assert_eq!(normalize_document(Document::default(),&context).unwrap_err(),CoreError::Cancelled);
}
