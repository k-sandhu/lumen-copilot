use lumen_docintel_core::{
    canonical::render,
    formats::{package::Limits, spreadsheets::extract},
};
use std::io::{Cursor, Write};
use lumen_docintel_core::{CoreError,formats::spreadsheets::WorkbookLimits};
use proptest::prelude::*;
fn legacy_xls(rows:u32)->Vec<u8> {
    fn record(output:&mut Vec<u8>,kind:u16,data:&[u8]) {output.extend(kind.to_le_bytes());output.extend((data.len() as u16).to_le_bytes());output.extend(data);}
    let mut workbook=Vec::new();record(&mut workbook,0x0809,&[0,6,5,0]);
    let mut bounds=29u32.to_le_bytes().to_vec();bounds.extend([0,0,5,0]);bounds.extend(b"Sheet");record(&mut workbook,0x0085,&bounds);record(&mut workbook,0x000a,&[]);
    record(&mut workbook,0x0809,&[0,6,16,0]);
    let mut dimensions=0u32.to_le_bytes().to_vec();dimensions.extend(rows.to_le_bytes());dimensions.extend(0u16.to_le_bytes());dimensions.extend(1u16.to_le_bytes());dimensions.extend(0u16.to_le_bytes());record(&mut workbook,0x0200,&dimensions);
    let mut label=vec![0,0,0,0,0,0,6,0,0];label.extend(b"Amount");record(&mut workbook,0x0204,&label);
    let mut number=vec![1,0,0,0,0,0];number.extend(12f64.to_le_bytes());record(&mut workbook,0x0203,&number);record(&mut workbook,0x000a,&[]);
    let mut compound=cfb::CompoundFile::create(Cursor::new(Vec::new())).unwrap();
    {let mut stream=compound.create_stream("/Workbook").unwrap();stream.write_all(&workbook).unwrap();}
    compound.into_inner().into_inner()
}
#[test]
fn legacy_xls_generated_biff_cells_and_dimension_budget() {
    let doc=extract(&legacy_xls(2),Limits::default()).unwrap();
    assert!(doc.blocks[0].text.contains("A2 [Amount]=12"));
    assert_eq!(doc.source_parts[0].name,"Sheet");render(doc).unwrap();
    let partial=extract(&legacy_xls(65536),WorkbookLimits {max_rows:2,..WorkbookLimits::default()}).unwrap();
    assert_eq!(partial.generation.outcome.as_deref(),Some("partial"));
    assert!(partial.blocks.is_empty());
}
fn ods(body:&str)->Vec<u8> {archive(&[("mimetype","application/vnd.oasis.opendocument.spreadsheet"),("content.xml",&format!(r#"<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet>{body}</office:spreadsheet></office:body></office:document-content>"#))])}
#[test]
fn ods_mixed_text_and_repeated_cells_are_ordered_and_bounded() {
    let bytes=ods(r#"<table:table table:name="Mixed"><table:table-row><table:table-cell table:number-columns-repeated="3" office:value-type="string"><text:p>Before <text:span>middle 😀</text:span> after</text:p></table:table-cell></table:table-row></table:table>"#);
    let doc=extract(&bytes,Limits::default()).unwrap();
    assert_eq!(doc.blocks[0].table.as_ref().unwrap().cells[0].text,"Before middle 😀 after");
    let partial=extract(&bytes,WorkbookLimits {max_cells:2,..WorkbookLimits::default()}).unwrap();
    assert_eq!(partial.generation.outcome.as_deref(),Some("partial"));
    assert_eq!(partial.blocks[0].table.as_ref().unwrap().cells.len(),2);
}
#[test]
fn oversized_repeats_are_partial_and_security_errors_are_typed() {
    let bytes=ods(r#"<table:table table:name="Big"><table:table-row table:number-rows-repeated="1000000000"><table:table-cell office:value-type="string"><text:p>value</text:p></table:table-cell></table:table-row></table:table>"#);
    let doc=extract(&bytes,WorkbookLimits {max_rows:2,max_cells:3,..WorkbookLimits::default()}).unwrap();
    assert_eq!(doc.generation.outcome.as_deref(),Some("partial"));
    assert_eq!(doc.blocks[0].table.as_ref().unwrap().cells.len(),2);
    assert_eq!(extract(b"broken",Limits::default()).unwrap_err(),CoreError::Parse);
    assert_eq!(extract(&archive(&[("mimetype","application/vnd.oasis.opendocument.spreadsheet"),("content.xml","<!DOCTYPE x [<!ENTITY a 'expanded'>]><x>&a;</x>")]),Limits::default()).unwrap_err(),CoreError::Unsupported);
    assert_eq!(extract(&archive(&[("../escape.xml","x")]),Limits::default()).unwrap_err(),CoreError::InvalidInput);
    assert_eq!(extract(&bytes,Limits {max_part_bytes:10,..Limits::default()}).unwrap_err(),CoreError::Budget);
}
proptest! {
    #[test]
    fn ods_unicode_offsets_and_native_coordinates_are_exact(text in "[a-zéΩ😀中]{1,30}", count in 1usize..8) {
        let bytes=ods(&format!("<table:table table:name=\"Unicode\"><table:table-row><table:table-cell table:number-columns-repeated=\"{count}\" office:value-type=\"string\"><text:p>{text}</text:p></table:table-cell></table:table-row></table:table>"));
        let rendered=render(extract(&bytes,Limits::default()).unwrap()).unwrap();
        for cell in &rendered.document.blocks[0].table.as_ref().unwrap().cells {prop_assert_eq!(&cell.text,&text);prop_assert_eq!(cell.row,1);prop_assert!(cell.column<=count);}
        for a in rendered.document.generation.diagnostics.as_ref().unwrap()["annotations"].as_array().unwrap().iter().filter(|a|a["kind"]=="cell_span") {let start=a["char_start"].as_u64().unwrap() as usize;let end=a["char_end"].as_u64().unwrap() as usize;prop_assert_eq!(rendered.rendered_text.chars().skip(start).take(end-start).collect::<String>(),text.clone());}
        prop_assert_eq!(rendered.document.source_parts[0].char_end,rendered.rendered_text.chars().count());
    }
}
fn archive(parts: &[(&str, &str)]) -> Vec<u8> {
    let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name, text) in parts {
        zip.start_file(*name, zip::write::SimpleFileOptions::default())
            .unwrap();
        zip.write_all(text.as_bytes()).unwrap();
    }
    zip.finish().unwrap().into_inner()
}
#[test]
fn ods_cells_keep_types_coordinates_and_source_parts() {
    let xml = r#"<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet><table:table table:name="Sales"><table:table-row><table:table-cell office:value-type="string"><text:p>Region</text:p></table:table-cell><table:table-cell office:value-type="string"><text:p>USD</text:p></table:table-cell></table:table-row><table:table-row><table:table-cell office:value-type="string"><text:p>West 😀</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="12"><text:p>12</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet></office:body></office:document-content>"#;
    let doc = extract(
        &archive(&[
            ("mimetype", "application/vnd.oasis.opendocument.spreadsheet"),
            ("content.xml", xml),
        ]),
        Limits::default(),
    )
    .unwrap();
    assert!(
        doc.blocks[0]
            .text
            .contains("A2 [Region]=West 😀 | B2 [USD]=12")
    );
    assert_eq!(doc.source_parts[0].name, "Sales");
    assert_eq!(
        doc.blocks[0].table.as_ref().unwrap().cells[3].cached_value,
        Some(serde_json::json!(12.0))
    );
    render(doc).unwrap();
}
