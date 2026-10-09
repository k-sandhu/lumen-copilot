use lumen_docintel_core::{canonical::{PartKind,render},formats::{package::Limits,presentations::extract}};
use std::io::{Cursor,Write};
fn archive(parts:&[(&str,&str)])->Vec<u8> {
    let mut zip=zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name,text) in parts {zip.start_file(*name,zip::write::SimpleFileOptions::default()).unwrap();zip.write_all(text.as_bytes()).unwrap();}
    zip.finish().unwrap().into_inner()
}
#[test]
fn odp_slides_and_mixed_shape_text_keep_provenance() {
    let xml=r#"<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:presentation><draw:page draw:name="Plan"><draw:frame draw:name="shape-1"><draw:text-box><text:p>Before <text:span>😀</text:span> after</text:p></draw:text-box></draw:frame></draw:page><draw:page draw:name="Blank"/></office:presentation></office:body></office:document-content>"#;
    let doc=extract(&archive(&[("mimetype","application/vnd.oasis.opendocument.presentation"),("content.xml",xml)]),Limits::default()).unwrap();
    assert_eq!(doc.source_parts.len(),2);
    assert_eq!(doc.source_parts[0].kind,PartKind::Slide);
    assert!(doc.blocks.iter().any(|b|b.text=="Before 😀 after"));
    assert_eq!(doc.source_parts[1].char_start,doc.source_parts[1].char_end);
    render(doc).unwrap();
}
