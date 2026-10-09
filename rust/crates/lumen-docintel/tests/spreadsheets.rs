use lumen_docintel_core::{canonical::render,formats::{package::Limits,spreadsheets::extract}};
use std::io::{Cursor,Write};
fn archive(parts:&[(&str,&str)])->Vec<u8> {
    let mut zip=zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name,text) in parts {zip.start_file(*name,zip::write::SimpleFileOptions::default()).unwrap();zip.write_all(text.as_bytes()).unwrap();}
    zip.finish().unwrap().into_inner()
}
#[test]
fn ods_cells_keep_types_coordinates_and_source_parts() {
    let xml=r#"<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet><table:table table:name="Sales"><table:table-row><table:table-cell office:value-type="string"><text:p>Region</text:p></table:table-cell><table:table-cell office:value-type="string"><text:p>USD</text:p></table:table-cell></table:table-row><table:table-row><table:table-cell office:value-type="string"><text:p>West 😀</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="12"><text:p>12</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet></office:body></office:document-content>"#;
    let doc=extract(&archive(&[("mimetype","application/vnd.oasis.opendocument.spreadsheet"),("content.xml",xml)]),Limits::default()).unwrap();
    assert!(doc.blocks[0].text.contains("A2 [Region]=West 😀 | B2 [USD]=12"));
    assert_eq!(doc.source_parts[0].name,"Sales");
    assert_eq!(doc.blocks[0].table.as_ref().unwrap().cells[3].cached_value,Some(serde_json::json!(12.0)));
    render(doc).unwrap();
}
