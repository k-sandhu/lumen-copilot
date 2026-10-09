use lumen_docintel_core::CoreError;
use lumen_docintel_core::detection::{Format, Route, decode_text, detect, route};
use std::io::{Cursor, Write};

fn archive(entries: &[(&str, &[u8])]) -> Vec<u8> {
    let mut zip = zip::ZipWriter::new(Cursor::new(vec![]));
    for (name, bytes) in entries {
        zip.start_file(
            *name,
            zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Stored),
        )
        .unwrap();
        zip.write_all(bytes).unwrap();
    }
    zip.finish().unwrap().into_inner()
}

#[test]
fn content_wins_and_container_roots_are_not_generic_zip() {
    let pdf = detect(b"%PDF-1.4\n", Some("text/plain")).unwrap();
    assert_eq!(pdf.format, Format::Pdf);
    assert!(pdf.declared_mismatch);
    for (entries, format) in [
        (
            vec![("word/document.xml", b"<w/>".as_slice())],
            Format::Docx,
        ),
        (vec![("xl/workbook.xml", b"<w/>".as_slice())], Format::Xlsx),
        (
            vec![("ppt/presentation.xml", b"<p/>".as_slice())],
            Format::Pptx,
        ),
        (
            vec![("mimetype", b"application/epub+zip".as_slice())],
            Format::Epub,
        ),
        (
            vec![(
                "mimetype",
                b"application/vnd.oasis.opendocument.text".as_slice(),
            )],
            Format::Odt,
        ),
    ] {
        assert_eq!(detect(&archive(&entries), None).unwrap().format, format);
    }
    assert_eq!(
        detect(
            &archive(&[("word/document.xml", b""), ("xl/workbook.xml", b"")]),
            None
        ),
        Err(CoreError::Unsupported)
    );
    assert_eq!(
        detect(b"\0\xff\0\xad\x11", None),
        Err(CoreError::Unsupported)
    );
}

#[test]
fn encodings_and_error_counts_do_not_confuse_literal_replacement() {
    for little in [true, false] {
        let text = "Hello é Ω 😀";
        let data: Vec<u8> = text
            .encode_utf16()
            .flat_map(|n| {
                if little {
                    n.to_le_bytes()
                } else {
                    n.to_be_bytes()
                }
            })
            .collect();
        assert_eq!(decode_text(&data).unwrap().text, text);
    }
    let text = "literal �";
    assert_eq!(decode_text(text.as_bytes()).unwrap().errors, 0);
    let invalid = decode_text(b"\xef\xbb\xbfok\xffbad").unwrap();
    assert_eq!(invalid.errors, 1);
    assert_eq!(invalid.text, "ok�bad");
    let legacy = b"The caf\xe9 serves cr\xe8me br\xfbl\xe9e and pi\xf1a colada.";
    assert!(decode_text(legacy).unwrap().text.contains("café"));
}

#[test]
fn text_families_and_compound_roots() {
    for (data, format) in [
        (b"{\"nbformat\":4,\"cells\":[]}".as_slice(), Format::Ipynb),
        (b"{\"a\":1}".as_slice(), Format::Json),
        (b"{\"a\":1}\n{\"a\":2}".as_slice(), Format::Jsonl),
        (b"<xbrl xmlns=\"x\"></xbrl>".as_slice(), Format::Xbrl),
        (b"<!doctype html><html>body</html>".as_slice(), Format::Html),
        (b"# Title\n\nbody".as_slice(), Format::Markdown),
        (b"A,B\n1,2\n3,4".as_slice(), Format::Csv),
        (
            b"From: a@example.test\nSubject: hi\n\nbody".as_slice(),
            Format::Eml,
        ),
        (
            b"From a@example.test Sat Jan 1\nFrom: a@example.test\n\nbody".as_slice(),
            Format::Mbox,
        ),
    ] {
        assert_eq!(detect(data, None).unwrap().format, format);
    }
    for (root, format) in [
        ("WordDocument", Format::Doc),
        ("Workbook", Format::Xls),
        ("PowerPoint Document", Format::Ppt),
        ("__properties_version1.0", Format::Msg),
    ] {
        let mut compound = cfb::CompoundFile::create(Cursor::new(vec![])).unwrap();
        compound
            .create_stream(format!("/{root}"))
            .unwrap()
            .write_all(b"synthetic")
            .unwrap();
        let bytes = compound.into_inner().into_inner();
        assert_eq!(detect(&bytes, None).unwrap().format, format);
    }
}

#[test]
fn parser_availability_and_enablement_are_independent() {
    assert_eq!(route(Format::Csv, &[], &[Format::Csv]), Route::Unsupported);
    assert_eq!(route(Format::Csv, &[Format::Csv], &[]), Route::Unsupported);
    assert_eq!(
        route(Format::Csv, &[Format::Csv], &[Format::Csv]),
        Route::Native
    );
    assert_eq!(route(Format::Pdf, &[], &[]), Route::PythonFallback);
}
