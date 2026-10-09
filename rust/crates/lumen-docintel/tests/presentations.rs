use lumen_docintel_core::{
    CoreError,
    formats::presentations::extract_with_context,
    runtime::{Budget, Cancellation, Context},
};
use lumen_docintel_core::{
    canonical::{PartKind, render},
    formats::{package::Limits, presentations::extract},
};
use proptest::prelude::*;
use std::io::{Cursor, Write};
fn deck(slide: &str, extras: &[(&str, &str)]) -> Vec<u8> {
    let presentation = r#"<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="slide"/></p:sldIdLst></p:presentation>"#;
    let rels = r#"<Relationships><Relationship Id="slide" Type="slide" Target="slides/slide1.xml"/></Relationships>"#;
    let xml = format!(
        r#"<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:cSld><p:spTree>{slide}</p:spTree></p:cSld></p:sld>"#
    );
    let mut parts = vec![
        ("ppt/presentation.xml", presentation),
        ("ppt/_rels/presentation.xml.rels", rels),
        ("ppt/slides/slide1.xml", xml.as_str()),
    ];
    parts.extend_from_slice(extras);
    archive(&parts)
}
fn shape(id: usize, text: &str, y: usize) -> String {
    format!(
        "<p:sp><p:nvSpPr><p:cNvPr id=\"{id}\" name=\"Text\"/></p:nvSpPr><p:spPr><a:xfrm><a:off x=\"12700\" y=\"{y}\"/><a:ext cx=\"25400\" cy=\"12700\"/></a:xfrm></p:spPr><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"
    )
}
#[test]
fn supplied_shape_positions_order_and_boxes_are_explicit() {
    let bytes = deck(
        &(shape(1, "Later", 25400) + &shape(2, "Earlier 😀", 12700)),
        &[],
    );
    let doc = extract(&bytes, Limits::default()).unwrap();
    assert_eq!(doc.blocks[1].text, "Earlier 😀");
    assert_eq!(doc.blocks[2].text, "Later");
    assert_eq!(doc.blocks[1].regions[0].bbox.as_ref().unwrap().y0, 1.0);
    assert_eq!(doc.blocks[1].regions[0].number, Some(1));
    render(doc).unwrap();
}
#[test]
fn charts_diagrams_and_image_alt_are_retained_with_source_metadata() {
    let shapes = r#"<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="2"/></p:nvGraphicFramePr><a:graphic><a:graphicData><c:chart r:id="chart"/></a:graphicData></a:graphic></p:graphicFrame><p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3"/></p:nvGraphicFramePr><a:graphic><a:graphicData><dgm:relIds r:dm="diagram"/></a:graphicData></a:graphic></p:graphicFrame><p:pic><p:nvPicPr><p:cNvPr id="4" descr="Source alt text"/></p:nvPicPr></p:pic>"#;
    let rels = r#"<Relationships><Relationship Id="chart" Type="chart" Target="../charts/chart1.xml"/><Relationship Id="diagram" Type="diagram" Target="../diagrams/data1.xml"/></Relationships>"#;
    let chart = r#"<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:ser><c:tx><c:v>Revenue USD</c:v></c:tx><c:cat><c:strCache><c:pt idx="0"><c:v>West</c:v></c:pt></c:strCache></c:cat><c:val><c:numCache><c:pt idx="0"><c:v>12</c:v></c:pt></c:numCache></c:val></c:ser></c:chart></c:chartSpace>"#;
    let diagram = r#"<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><dgm:ptLst><dgm:pt><dgm:t><a:p><a:r><a:t>Diagram fact 😀</a:t></a:r></a:p></dgm:t></dgm:pt></dgm:ptLst></dgm:dataModel>"#;
    let doc = extract(
        &deck(
            shapes,
            &[
                ("ppt/slides/_rels/slide1.xml.rels", rels),
                ("ppt/charts/chart1.xml", chart),
                ("ppt/diagrams/data1.xml", diagram),
            ],
        ),
        Limits::default(),
    )
    .unwrap();
    let rendered = render(doc).unwrap();
    assert!(rendered.rendered_text.contains("West=12"));
    assert!(rendered.rendered_text.contains("Diagram fact 😀"));
    assert!(rendered.rendered_text.contains("[Image: Source alt text]"));
    assert!(
        rendered
            .document
            .generation
            .diagnostics
            .unwrap()
            .to_string()
            .contains("unknown")
    );
}
#[test]
fn corrupt_oversized_traversing_and_entity_packages_fail_closed() {
    assert_eq!(
        extract(b"broken", Limits::default()).unwrap_err(),
        CoreError::Parse
    );
    assert_eq!(
        extract(&archive(&[("../escape.xml", "x")]), Limits::default()).unwrap_err(),
        CoreError::InvalidInput
    );
    let bytes = deck(
        &shape(1, "Text", 12700),
        &[("ppt/media/image1.bin", "large media")],
    );
    assert_eq!(
        extract(
            &bytes,
            Limits {
                max_part_bytes: 8,
                ..Limits::default()
            }
        )
        .unwrap_err(),
        CoreError::Budget
    );
    assert_eq!(
        extract(&deck("<!DOCTYPE x>", &[]), Limits::default()).unwrap_err(),
        CoreError::Unsupported
    );
    let token = Cancellation::default();
    token.cancel();
    let ctx = Context::new(Budget::default(), token).unwrap();
    assert_eq!(
        extract_with_context(&bytes, Limits::default(), &ctx).unwrap_err(),
        CoreError::Cancelled
    );
}
proptest! {
    #[test]
    fn slide_shape_unicode_spans_and_regions_are_exact(text in "[a-zéΩ😀中]{1,60}", y in 0usize..100000) {
        let doc=extract(&deck(&shape(7,&text,y),&[]),Limits::default()).unwrap();let rendered=render(doc).unwrap();
        for (span,block) in rendered.spans.iter().zip(&rendered.document.blocks) {prop_assert_eq!(rendered.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect::<String>(),block.text.clone());prop_assert_eq!(block.regions[0].number,Some(1));}
        prop_assert_eq!(&rendered.document.blocks[1].text,&text);
        prop_assert!(rendered.document.blocks[1].id.ends_with("shape[7]"));
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
fn odp_slides_and_mixed_shape_text_keep_provenance() {
    let xml = r#"<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:presentation><draw:page draw:name="Plan"><draw:frame draw:name="shape-1"><draw:text-box><text:p>Before <text:span>😀</text:span> after</text:p></draw:text-box></draw:frame></draw:page><draw:page draw:name="Blank"/></office:presentation></office:body></office:document-content>"#;
    let doc = extract(
        &archive(&[
            (
                "mimetype",
                "application/vnd.oasis.opendocument.presentation",
            ),
            ("content.xml", xml),
        ]),
        Limits::default(),
    )
    .unwrap();
    assert_eq!(doc.source_parts.len(), 2);
    assert_eq!(doc.source_parts[0].kind, PartKind::Slide);
    assert!(doc.blocks.iter().any(|b| b.text == "Before 😀 after"));
    assert_eq!(doc.source_parts[1].char_start, doc.source_parts[1].char_end);
    render(doc).unwrap();
}
