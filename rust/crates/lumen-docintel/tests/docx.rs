use lumen_docintel_core::{canonical::{BlockKind, render}, formats::docx::extract, runtime::Budget};
use std::io::{Cursor, Write};

fn package(xml: &str) -> Vec<u8> {
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    zip.start_file("word/document.xml", zip::write::SimpleFileOptions::default()).unwrap();
    zip.write_all(xml.as_bytes()).unwrap();
    zip.finish().unwrap().into_inner()
}

#[test]
fn preserves_body_order_unicode_and_table_labels() {
    let bytes = package(r#"<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Before 😀</w:t></w:r></w:p><w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>USD</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>West</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>After é</w:t></w:r></w:p></w:body></w:document>"#);
    let doc = extract(&bytes, Budget::default()).unwrap();
    assert_eq!(doc.blocks.len(), 3);
    assert_eq!(doc.blocks[1].kind, BlockKind::Table);
    assert!(doc.blocks[1].text.contains("Row 2: C1 [Region]=West | C2 [USD]=12"));
    assert_eq!(doc.blocks[1].table.as_ref().unwrap().cells.len(), 4);
    let rendered = render(doc).unwrap();
    for (span, block) in rendered.spans.iter().zip(&rendered.document.blocks) {
        assert_eq!(rendered.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect::<String>(), block.text);
    }
}
