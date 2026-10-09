use lumen_docintel_core::{
    CoreError,
    canonical::{BlockKind, render},
    formats::{odt, package::Limits, rtf},
    runtime::{Budget, Cancellation, Context},
};
use proptest::prelude::*;
use std::io::{Cursor, Write};

fn odt_package(body: &str) -> Vec<u8> {
    let xml = format!(
        r#"<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:text>{body}</office:text></office:body></office:document-content>"#
    );
    let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name, text) in [
        ("mimetype", "application/vnd.oasis.opendocument.text"),
        ("content.xml", &xml),
    ] {
        archive
            .start_file(name, zip::write::SimpleFileOptions::default())
            .unwrap();
        archive.write_all(text.as_bytes()).unwrap();
    }
    archive.finish().unwrap().into_inner()
}

#[test]
fn odt_order_mixed_text_tables_and_notes() {
    let bytes = odt_package(
        r#"<text:h text:outline-level="1">Intro</text:h><text:p>Before <text:span>middle</text:span><text:s text:c="2"/>after<text:note><text:note-citation>1</text:note-citation><text:note-body><text:p>Note</text:p></text:note-body></text:note></text:p><text:list><text:list-item><text:p>Item</text:p></text:list-item></text:list><table:table><table:table-row><table:table-cell><text:p>A</text:p></table:table-cell><table:table-cell><text:p>B</text:p></table:table-cell></table:table-row></table:table>"#,
    );
    let d = odt::extract(&bytes, Limits::default()).unwrap();
    assert_eq!(
        d.blocks.iter().map(|b| b.kind).collect::<Vec<_>>(),
        vec![
            BlockKind::Heading,
            BlockKind::Paragraph,
            BlockKind::Footnote,
            BlockKind::List,
            BlockKind::Table
        ]
    );
    assert_eq!(d.blocks[1].text, "Before middle  after");
    assert_eq!(d.blocks[2].parent_id, Some(d.blocks[1].id.clone()));
    assert_eq!(d.blocks[4].table.as_ref().unwrap().cells[1].column, 2);
    render(d).unwrap();
}

#[test]
fn rtf_order_codepages_unicode_and_scoped_formatting() {
    let bytes = br"{\rtf1\ansi\ansicpg1252\outlinelevel0 Heading\par\outlinelevel9 caf\'e9 \uc1\u-10179?\u-8704?\par\ls1 Item\par\ls0\trowd\cellx100\cellx200\intbl A\cell B\cell\row}";
    let d = rtf::extract(bytes, Limits::default()).unwrap();
    assert_eq!(d.blocks[0].kind, BlockKind::Heading);
    assert_eq!(d.blocks[1].text, "café 😀");
    assert_eq!(d.blocks[2].kind, BlockKind::List);
    assert_eq!(d.blocks[3].table.as_ref().unwrap().columns, 2);
    let cyrillic = rtf::extract(
        br"{\rtf1\ansi\ansicpg1251 \'cf\'f0\'e8\'e2\'e5\'f2}",
        Limits::default(),
    )
    .unwrap();
    assert_eq!(cyrillic.blocks[0].text, "Привет");
    render(d).unwrap();
}

#[test]
fn rtf_footnote_and_scope() {
    let d = rtf::extract(
        br"{\rtf1 Before{\footnote Note} after\par {\outlinelevel0 Heading}\par Plain}",
        Limits::default(),
    )
    .unwrap();
    assert_eq!(d.blocks[0].text, "Before after");
    assert_eq!(d.blocks[1].text, "Note");
    assert_eq!(d.blocks[1].parent_id, Some(d.blocks[0].id.clone()));
    assert_eq!(d.blocks.last().unwrap().kind, BlockKind::Paragraph);
    let ranges = &d.generation.diagnostics.as_ref().unwrap()["byte_ranges"];
    let note = &ranges[1];
    assert!(note["byte_start"].as_u64().unwrap() < note["byte_end"].as_u64().unwrap());
}

#[test]
fn table_rows_share_coordinates_and_final_render_obeys_budget() {
    let d = rtf::extract(br"{\rtf1\trowd\cellx100\cellx200\intbl A\cell B\cell\row\trowd\cellx100\cellx200\intbl C\cell D\cell\row}", Limits::default()).unwrap();
    assert_eq!(d.blocks.len(), 1);
    let table = d.blocks[0].table.as_ref().unwrap();
    assert_eq!(table.rows, 2);
    assert_eq!(table.cells[3].row, 2);
    assert_eq!(table.cells[3].column, 2);
    let limits = Limits {
        budget: Budget {
            max_output_chars: 2,
            ..Budget::default()
        },
        ..Limits::default()
    };
    assert_eq!(
        rtf::extract(br"{\rtf1 a\par b}", limits),
        Err(CoreError::Budget)
    );
    assert_eq!(
        odt::extract(&odt_package("<text:p>a</text:p><text:p>b</text:p>"), limits),
        Err(CoreError::Budget)
    );
}

#[test]
fn malformed_unsupported_and_budget_inputs_fail_closed() {
    for bytes in [
        br"{\rtf1 broken".as_slice(),
        br"{\rtf1 \'zz}",
        br"{\rtf1\u99999999999999 x}",
        br"{\rtf1\u-10179?x}",
        br"{\rtf1}garbage",
    ] {
        assert_eq!(
            rtf::extract(bytes, Limits::default()),
            Err(CoreError::Parse)
        );
    }
    for bytes in [
        br"{\rtf1{\object x}}".as_slice(),
        br"{\rtf1\ansicpg99999 x}",
        br"{\rtf1{\*\unknown x}}",
        br"{\rtf1\bin3 abc}",
    ] {
        assert_eq!(
            rtf::extract(bytes, Limits::default()),
            Err(CoreError::Unsupported)
        );
    }
    let deep = format!("{{\\rtf1 {}x{}}}", "{".repeat(129), "}".repeat(129));
    assert_eq!(
        rtf::extract(deep.as_bytes(), Limits::default()),
        Err(CoreError::Budget)
    );
    let limits = Limits {
        budget: Budget {
            max_output_chars: 1,
            ..Budget::default()
        },
        ..Limits::default()
    };
    assert_eq!(
        rtf::extract(br"{\rtf1 long}", limits),
        Err(CoreError::Budget)
    );
    assert_eq!(
        odt::extract(&odt_package("<text:p>long</text:p>"), limits),
        Err(CoreError::Budget)
    );
    assert_eq!(
        odt::extract(
            &odt_package("<!DOCTYPE x><text:p>x</text:p>"),
            Limits::default()
        ),
        Err(CoreError::Unsupported)
    );
    assert_eq!(
        odt::extract(
            &odt_package(
                r#"<table:table><table:table-row table:number-rows-repeated="2"/></table:table>"#
            ),
            Limits::default()
        ),
        Err(CoreError::Unsupported)
    );
    let token = Cancellation::default();
    token.cancel();
    let ctx = Context::new(Budget::default(), token).unwrap();
    assert_eq!(
        rtf::extract_with_context(br"{\rtf1 x}", Limits::default(), ctx),
        Err(CoreError::Cancelled)
    );
}

proptest! {
    #[test]
    fn odt_unicode_offsets_and_paragraph_paths(values in prop::collection::vec("[a-zé東京🦀]{1,20}", 1..12)) {
        let body = values.iter().map(|s| format!("<text:p>{s}</text:p>")).collect::<String>();
        let r = render(odt::extract(&odt_package(&body), Limits::default()).unwrap()).unwrap();
        for (i, (span, block)) in r.spans.iter().zip(&r.document.blocks).enumerate() {
            prop_assert_eq!(&block.text, &values[i]);
            let path = format!("content.xml/office:text/text:p[{}]", i+1);
            prop_assert_eq!(block.regions[0].name.as_deref(), Some(path.as_str()));
            prop_assert_eq!(r.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect::<String>(), block.text.clone());
        }
    }
    #[test]
    fn rtf_unicode_offsets_and_ordinals(values in prop::collection::vec("[a-zé東京🦀]{1,20}", 1..12)) {
        let mut source = String::from("{\\rtf1\\ansi\\uc0 ");
        for value in &values { for unit in value.encode_utf16() { source.push_str(&format!("\\u{} ", unit as i16)); } source.push_str("\\par "); }
        source.push('}');
        let r = render(rtf::extract(source.as_bytes(), Limits::default()).unwrap()).unwrap();
        for (i, (span, block)) in r.spans.iter().zip(&r.document.blocks).enumerate() {
            prop_assert_eq!(&block.text, &values[i]);
            let path = format!("rtf/paragraph[{}]", i+1);
            prop_assert_eq!(block.regions[0].name.as_deref(), Some(path.as_str()));
            prop_assert_eq!(r.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect::<String>(), block.text.clone());
        }
    }
}
