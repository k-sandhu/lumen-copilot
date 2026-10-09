use lumen_docintel_core::{
    CoreError,
    canonical::{BlockKind, render},
    formats::{common::Limits, epub::parse},
};
use proptest::prelude::*;
use std::io::{Cursor, Write};
fn package(extra: Option<(&str, &str)>, version: &str, spine: &str) -> Vec<u8> {
    let mut z = zip::ZipWriter::new(Cursor::new(Vec::new()));
    let opf = format!(
        r#"<package version="{version}"><metadata><title>Book</title></metadata><manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/><item id="b" href="b.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="toc">{spine}</spine></package>"#
    );
    let mut files = vec![
        ("mimetype", "application/epub+zip"),
        (
            "META-INF/container.xml",
            r#"<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>"#,
        ),
        ("OPS/book.opf", &opf),
        (
            "OPS/a.xhtml",
            r#"<html><body><p>café 東京</p><aside role="doc-footnote">Note</aside></body></html>"#,
        ),
        ("OPS/b.xhtml", "<html><body><p>Second</p></body></html>"),
        (
            "OPS/nav.xhtml",
            r#"<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><a href="a.xhtml">Alpha</a><a href="b.xhtml">Beta</a></nav></body></html>"#,
        ),
        (
            "OPS/toc.ncx",
            r#"<ncx><navMap><navPoint><navLabel><text>Alpha</text></navLabel><content src="a.xhtml"/></navPoint><navPoint><navLabel><text>Beta</text></navLabel><content src="b.xhtml"/></navPoint></navMap></ncx>"#,
        ),
    ];
    if let Some(e) = extra {
        files.push(e)
    }
    for (name, text) in files {
        z.start_file(name, zip::write::SimpleFileOptions::default())
            .unwrap();
        z.write_all(text.as_bytes()).unwrap();
    }
    z.finish().unwrap().into_inner()
}
#[test]
fn versions_spine_titles_footnotes_and_negative_packages() {
    for version in ["2.0", "3.0"] {
        let bytes = package(None, version, r#"<itemref idref="b"/><itemref idref="a"/>"#);
        let doc = parse(&bytes, Limits::default()).unwrap();
        let chapters = &doc.generation.diagnostics.as_ref().unwrap()["chapters"];
        assert_eq!(chapters[0]["title"], "Beta");
        assert_eq!(chapters[1]["title"], "Alpha");
        assert!(
            doc.blocks
                .iter()
                .any(|b| b.kind == BlockKind::Footnote && b.text == "Note")
        );
        let r = render(doc).unwrap();
        assert!(r.rendered_text.find("Second").unwrap() < r.rendered_text.find("café").unwrap());
    }
    let spine = r#"<itemref idref="a"/>"#;
    assert_eq!(
        parse(
            &package(Some(("../escape", "x")), "3.0", spine),
            Limits::default()
        ),
        Err(CoreError::InvalidInput)
    );
    assert_eq!(
        parse(
            &package(
                Some(("META-INF/encryption.xml", "<encryption/>")),
                "3.0",
                spine
            ),
            Limits::default()
        ),
        Err(CoreError::Unsupported)
    );
    assert_eq!(parse(b"PKbroken", Limits::default()), Err(CoreError::Parse));
    let mut limits = Limits::default();
    limits.budget.max_output_chars = 1;
    assert_eq!(
        parse(&package(None, "3.0", spine), limits),
        Err(CoreError::Budget)
    );
}
proptest! {
 #[test]
 fn chapter_ordinals_and_unicode_spans(reverse in any::<bool>()) {
  let spine=if reverse {r#"<itemref idref="b"/><itemref idref="a"/>"#}else{r#"<itemref idref="a"/><itemref idref="b"/>"#};
  let r=render(parse(&package(None,"3.0",spine),Limits::default()).unwrap()).unwrap();
  for (span,b) in r.spans.iter().zip(&r.document.blocks) {
   let n=if b.text=="Second"||b.text=="Beta" {if reverse {1}else{2}}else{if reverse {2}else{1}};
   let expected=format!("chapter:{n};");
   prop_assert!(b.regions[0].name.as_ref().unwrap().contains(&expected));
   prop_assert_eq!(r.rendered_text.chars().skip(span.char_start).take(span.char_end-span.char_start).collect::<String>(),b.text.clone());
  }
 }
}
