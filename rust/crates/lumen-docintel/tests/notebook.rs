use lumen_docintel_core::{CoreError, canonical::{BlockKind, render}, formats::{common::Limits, notebook::parse}};
use proptest::prelude::*;
use serde_json::json;

fn fixture(value: &str) -> Vec<u8> {
 serde_json::to_vec(&json!({"nbformat":4,"metadata":{"language_info":{"name":"python"}},"cells":[
  {"cell_type":"markdown","source":["# Intro\n", value]},
  {"cell_type":"code","source":["raise RuntimeError('inert')"],"outputs":[
   {"output_type":"stream","text":"North -120 kg"},
   {"output_type":"display_data","data":{"image/png":"AAAA"}}]}
 ]})).unwrap()
}
#[test]
fn ordered_cells_outputs_and_limits() {
 let doc=parse(&fixture("東京"),Limits::default()).unwrap();
 assert_eq!(doc.blocks[0].kind,BlockKind::Heading);
 assert_eq!(doc.blocks[2].kind,BlockKind::Code);
 assert!(doc.blocks[2].text.contains("raise RuntimeError"));
 assert!(doc.blocks[3].text.contains("North -120 kg"));
 assert!(doc.blocks[4].text.contains("image/png"));
 assert!(!doc.blocks[4].text.contains("AAAA"));
 assert!(doc.blocks[3].regions[0].name.as_ref().unwrap().contains("cell:1;output:0"));
 let bytes=serde_json::to_vec(&json!({"nbformat":4,"cells":[{"cell_type":"code","source":"x","outputs":[{"output_type":"stream","text":"abcdefghij"}]}]})).unwrap();
 let l=Limits {max_record_bytes:4,..Limits::default()};
 let d=parse(&bytes,l).unwrap();
 assert_eq!(d.blocks[1].text,"abcd");
 assert_eq!(d.generation.outcome.as_deref(),Some("partial"));
 assert_eq!(d.generation.diagnostics.as_ref().unwrap()["truncated_outputs"][0]["cell"],0);
 assert_eq!(parse(b"{broken",Limits::default()),Err(CoreError::Parse));
 let deep=format!("{}0{}","[".repeat(70),"]".repeat(70));
 assert_eq!(parse(deep.as_bytes(),Limits::default()),Err(CoreError::Budget));
 let mut l=Limits::default();l.budget.max_output_chars=1;
 assert_eq!(parse(&fixture("long"),l),Err(CoreError::Budget));
}
proptest! {
 #[test]
 fn exact_cell_offsets(value in "[a-zé東京🦀]{1,30}") {
  let r=render(parse(&fixture(&value),Limits::default()).unwrap()).unwrap();
  prop_assert_eq!(&r.document.blocks[1].text,&value);
  for (b,span) in r.document.blocks.iter().zip(&r.spans) {
   prop_assert!(b.regions[0].name.as_ref().unwrap().starts_with("cell:"));
   prop_assert_eq!(r.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect::<String>(),b.text.clone());
  }
 }
}
