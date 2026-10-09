use lumen_docintel_core::{
    CoreError,
    formats::{docx::extract_with_context, package::Limits},
    runtime::{Cancellation, Context},
};
use lumen_docintel_core::{
    canonical::{BlockKind, render},
    formats::docx::extract,
    runtime::Budget,
};
use proptest::prelude::*;
use std::io::{Cursor, Write};

fn package(xml: &str) -> Vec<u8> {
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    zip.start_file(
        "word/document.xml",
        zip::write::SimpleFileOptions::default(),
    )
    .unwrap();
    zip.write_all(xml.as_bytes()).unwrap();
    zip.finish().unwrap().into_inner()
}

fn parts(entries: &[(&str, &str)]) -> Vec<u8> {
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name, text) in entries {
        zip.start_file(*name, zip::write::SimpleFileOptions::default())
            .unwrap();
        zip.write_all(text.as_bytes()).unwrap();
    }
    zip.finish().unwrap().into_inner()
}
fn document(body: &str) -> String {
    format!(
        r#"<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>"#
    )
}
fn p(text: &str) -> String {
    format!("<w:p><w:r><w:t>{text}</w:t></w:r></w:p>")
}

#[test]
fn accepted_changes_heading_lists_notes_comments_and_furniture() {
    let xml = document(
        r#"<w:p><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:r><w:t>Heading</w:t></w:r></w:p><w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="3"/></w:numPr></w:pPr><w:del><w:r><w:delText>Deleted</w:delText></w:r></w:del><w:ins><w:r><w:t>Accepted</w:t><w:footnoteReference w:id="2"/></w:r></w:ins><w:r><w:commentReference w:id="4"/></w:r></w:p><w:sectPr><w:headerReference w:id="header"/></w:sectPr><w:sectPr><w:headerReference w:id="header"/></w:sectPr>"#,
    );
    let notes = r#"<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="2"><w:p><w:r><w:t>Note 😀</w:t></w:r></w:p></w:footnote></w:footnotes>"#;
    let header = r#"<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Furniture</w:t></w:r></w:p></w:hdr>"#;
    let comments = r#"<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:id="4" w:author="Editor"><w:p><w:r><w:t>Annotation</w:t></w:r></w:p></w:comment></w:comments>"#;
    let rels = r#"<Relationships><Relationship Id="header" Type="header" Target="header1.xml"/></Relationships>"#;
    let bytes = parts(&[
        ("word/document.xml", &xml),
        ("word/footnotes.xml", notes),
        ("word/header1.xml", header),
        ("word/comments.xml", comments),
        ("word/_rels/document.xml.rels", rels),
    ]);
    let doc = extract(&bytes, Limits::default()).unwrap();
    assert_eq!(doc.blocks[0].kind, BlockKind::Heading);
    assert_eq!(doc.blocks[1].kind, BlockKind::List);
    assert_eq!(doc.blocks[1].text, "Accepted");
    assert_eq!(doc.blocks[2].kind, BlockKind::Footnote);
    assert_eq!(doc.blocks[2].parent_id.as_ref(), Some(&doc.blocks[1].id));
    assert_eq!(
        doc.blocks
            .iter()
            .filter(|b| b.kind == BlockKind::Furniture)
            .count(),
        1
    );
    assert!(
        doc.generation
            .diagnostics
            .unwrap()
            .to_string()
            .contains("Annotation")
    );
}

#[test]
fn merged_and_nested_cells_preserve_origins() {
    let nested = format!("<w:tbl><w:tr><w:tc>{}</w:tc></w:tr></w:tbl>", p("Leaf"));
    let first = format!(
        "<w:tr><w:tc><w:tcPr><w:gridSpan w:val=\"2\"/><w:vMerge w:val=\"restart\"/></w:tcPr>{}{nested}</w:tc></w:tr>",
        p("Group")
    );
    let second =
        "<w:tr><w:tc><w:tcPr><w:gridSpan w:val=\"2\"/><w:vMerge/></w:tcPr><w:p/></w:tc></w:tr>";
    let doc = extract(
        &package(&document(&format!(
            "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>{first}{second}</w:tbl>"
        ))),
        Limits::default(),
    )
    .unwrap();
    assert_eq!(doc.blocks[0].text.matches("Leaf").count(), 1);
    let cell = &doc.blocks[0].table.as_ref().unwrap().cells[0];
    assert_eq!((cell.row_span, cell.column_span), (2, 2));
    assert!(
        doc.blocks[0]
            .text
            .contains("[nested tables at R1C1] [merged from R1C1]")
    );
    render(doc).unwrap();
}

#[test]
fn untrusted_packages_fail_closed_with_typed_errors() {
    let xml = document(&p("Hello"));
    for path in [
        "../escape.xml",
        "/absolute.xml",
        "word/../../escape.xml",
        "word\\escape.xml",
    ] {
        assert_eq!(
            extract(
                &parts(&[("word/document.xml", &xml), (path, "x")]),
                Limits::default()
            )
            .unwrap_err(),
            CoreError::InvalidInput
        );
    }
    assert_eq!(
        extract(b"broken", Limits::default()).unwrap_err(),
        CoreError::Parse
    );
    assert_eq!(
        extract(
            &parts(&[("word/document.xml", &xml), ("word/vbaProject.bin", "x")]),
            Limits::default()
        )
        .unwrap_err(),
        CoreError::Unsupported
    );
    assert_eq!(
        extract(
            &package("<!DOCTYPE x [<!ENTITY a 'expanded'>]><x>&a;</x>"),
            Limits::default()
        )
        .unwrap_err(),
        CoreError::Unsupported
    );
    assert_eq!(
        extract(
            &package(&document("<w:p><w:r><w:t>&unknown;</w:t></w:r></w:p>")),
            Limits::default()
        )
        .unwrap_err(),
        CoreError::Parse
    );
    assert_eq!(
        extract(&package(&document("<w:p>")), Limits::default()).unwrap_err(),
        CoreError::Parse
    );
    for limits in [
        Limits {
            max_entries: 1,
            ..Limits::default()
        },
        Limits {
            max_part_bytes: 4,
            ..Limits::default()
        },
        Limits {
            max_expanded_bytes: 4,
            ..Limits::default()
        },
    ] {
        assert_eq!(
            extract(
                &parts(&[("word/document.xml", &xml), ("other.xml", "x")]),
                limits
            )
            .unwrap_err(),
            CoreError::Budget
        );
    }
    for budget in [
        Budget {
            max_output_chars: 2,
            ..Budget::default()
        },
        Budget {
            max_work_units: 3,
            ..Budget::default()
        },
        Budget {
            max_memory_bytes: 20,
            ..Budget::default()
        },
        Budget {
            max_input_bytes: 4,
            ..Budget::default()
        },
        Budget {
            timeout_ms: 0,
            ..Budget::default()
        },
    ] {
        assert_eq!(
            extract(&package(&xml), budget).unwrap_err(),
            CoreError::Budget
        );
    }
    let token = Cancellation::default();
    token.cancel();
    let ctx = Context::new(Budget::default(), token).unwrap();
    assert_eq!(
        extract_with_context(&package(&xml), Limits::default(), &ctx).unwrap_err(),
        CoreError::Cancelled
    );
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    zip.start_file(
        "word/document.xml",
        zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated),
    )
    .unwrap();
    zip.write_all(document(&p(&"a".repeat(100000))).as_bytes())
        .unwrap();
    assert_eq!(
        extract(&zip.finish().unwrap().into_inner(), Limits::default()).unwrap_err(),
        CoreError::Budget
    );
}

proptest! {
    #[test]
    fn source_offsets_and_cell_coordinates_roundtrip(text in "[a-zéΩ😀中]{0,60}", rows in 1usize..12, columns in 1usize..5) {
        let mut body=String::from("<w:tbl>");
        for _ in 0..rows {body.push_str("<w:tr>");for _ in 0..columns {body.push_str("<w:tc>");body.push_str(&p(&text));body.push_str("</w:tc>");}body.push_str("</w:tr>");}body.push_str("</w:tbl>");
        let rendered=render(extract(&package(&document(&body)),Limits::default()).unwrap()).unwrap();
        let table=rendered.document.blocks[0].table.as_ref().unwrap();
        prop_assert_eq!(table.cells.len(),rows*columns);
        for cell in &table.cells {prop_assert_eq!(&cell.text,&text);prop_assert!(cell.row<=rows && cell.column<=columns);}
        let annotations=rendered.document.generation.diagnostics.as_ref().unwrap()["annotations"].as_array().unwrap();
        for a in annotations.iter().filter(|a|a["kind"]=="cell_span") {
            let start=a["char_start"].as_u64().unwrap() as usize;let end=a["char_end"].as_u64().unwrap() as usize;
            let slice:String=rendered.document.blocks[0].text.chars().skip(start).take(end-start).collect();
            prop_assert_eq!(&slice,&text);
        }
        let restored=render(serde_json::from_str(&serde_json::to_string(&rendered.document).unwrap()).unwrap()).unwrap();
        prop_assert_eq!(rendered,restored);
    }
}

#[test]
fn preserves_body_order_unicode_and_table_labels() {
    let bytes = package(
        r#"<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Before 😀</w:t></w:r></w:p><w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>USD</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>West</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>After é</w:t></w:r></w:p></w:body></w:document>"#,
    );
    let doc = extract(&bytes, Budget::default()).unwrap();
    assert_eq!(doc.blocks.len(), 3);
    assert_eq!(doc.blocks[1].kind, BlockKind::Table);
    assert!(
        doc.blocks[1]
            .text
            .contains("Row 2: C1 [Region]=West | C2 [USD]=12")
    );
    assert_eq!(doc.blocks[1].table.as_ref().unwrap().cells.len(), 4);
    let rendered = render(doc).unwrap();
    for (span, block) in rendered.spans.iter().zip(&rendered.document.blocks) {
        assert_eq!(
            rendered
                .rendered_text
                .chars()
                .skip(span.char_start)
                .take(span.char_end - span.char_start)
                .collect::<String>(),
            block.text
        );
    }
}

#[test]
fn build_identity_covers_shared_extraction_dependencies() {
    use lumen_docintel_core::formats::package::{generation, sha256};
    let first = generation(b"same input", "rust-docx", "format source v1");
    assert_ne!(first.build_id.as_deref(), Some(sha256(b"format source v1").as_str()));
    assert_eq!(first, generation(b"same input", "rust-docx", "format source v1"));
    assert_ne!(first.build_id, generation(b"same input", "rust-docx", "format source v2").build_id);
}
