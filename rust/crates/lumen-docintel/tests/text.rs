use lumen_docintel_core::{
    CoreError,
    canonical::{BlockKind, render},
    formats::{
        common::Limits,
        text::{Mode, parse},
    },
};
use proptest::prelude::*;

#[test]
fn markdown_roles_code_and_lines() {
    let doc = parse(
        "# Intro\r\n\r\n- café\r\n- 東京\r\n\r\n```rust\r\nlet x = 1;\r\nlet y = 2;\r\n```\r\n"
            .as_bytes(),
        Limits::default(),
        Mode::Markdown,
    )
    .unwrap();
    assert_eq!(doc.blocks[0].kind, BlockKind::Heading);
    assert_eq!(doc.blocks[0].heading_level, Some(1));
    assert!(
        doc.blocks
            .iter()
            .any(|b| b.kind == BlockKind::List && b.text.contains("東京"))
    );
    let code = doc
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Code)
        .unwrap();
    assert_eq!(code.text, "let x = 1;\nlet y = 2;\n");
    assert_eq!(code.regions[0].name.as_deref(), Some("lines:6-9"));
    assert_eq!(code.heading_path, vec!["Intro"]);
}
#[test]
fn encoding_binary_and_limits() {
    let doc = parse(
        b"\xef\xbb\xbfhello\rworld\xff",
        Limits::default(),
        Mode::Text,
    )
    .unwrap();
    assert_eq!(
        doc.generation.diagnostics.as_ref().unwrap()["decoding_errors"],
        1
    );
    assert_eq!(doc.generation.outcome.as_deref(), Some("partial"));
    assert!(doc.blocks[0].text.contains("hello\nworld"));
    let data: Vec<u8> = "café\r\n世界"
        .encode_utf16()
        .flat_map(u16::to_le_bytes)
        .collect();
    assert!(
        parse(&data, Limits::default(), Mode::Text).unwrap().blocks[0]
            .text
            .contains("世界")
    );
    assert_eq!(
        parse(b"%PDF-1.7\n", Limits::default(), Mode::Text),
        Err(CoreError::Unsupported)
    );
    assert_eq!(
        parse(b"\0\xff\0\xad", Limits::default(), Mode::Text),
        Err(CoreError::Unsupported)
    );
    let mut limits = Limits::default();
    limits.budget.max_output_chars = 2;
    assert_eq!(
        parse(b"long", limits, Mode::Code("python")),
        Err(CoreError::Budget)
    );
}
proptest! {
 #[test]
 fn unicode_exact_offsets_and_line_provenance(lines in prop::collection::vec("[a-zé東京🦀]{1,20}",1..20)) {
  let source=lines.join("\r\n\r\n");
  let r=render(parse(source.as_bytes(),Limits::default(),Mode::Text).unwrap()).unwrap();
  for (n,(span,block)) in r.spans.iter().zip(&r.document.blocks).enumerate() {
   prop_assert_eq!(&block.text,&lines[n]);
   let expected=format!("lines:{}-{}",2*n+1,2*n+1);
   prop_assert_eq!(block.regions[0].name.as_deref(),Some(expected.as_str()));
   prop_assert_eq!(r.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect::<String>(),block.text.clone());
  }
 }
}

#[test]
fn ordinary_zip_initial_letters_remain_text() {
    let doc = parse(
        b"PK means a supplied source label",
        Limits::default(),
        Mode::Text,
    )
    .unwrap();
    assert_eq!(doc.blocks[0].text, "PK means a supplied source label");
}
