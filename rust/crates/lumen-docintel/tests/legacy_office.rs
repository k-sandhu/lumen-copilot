use lumen_docintel_core::{
    CoreError,
    detection::{Format, detect},
    formats::legacy_office,
    runtime::{Budget, Cancellation, Context},
};
use proptest::prelude::*;
use std::io::{Cursor, Write};

fn fixture(stream: &str, text: &str) -> Vec<u8> {
    let mut compound = cfb::CompoundFile::create(Cursor::new(Vec::new())).unwrap();
    let mut payload = vec![0u8; 64];
    payload[0..2].copy_from_slice(&0xa5ecu16.to_le_bytes());
    for u in text.encode_utf16() {
        payload.extend(u.to_le_bytes());
    }
    compound
        .create_stream(stream)
        .unwrap()
        .write_all(&payload)
        .unwrap();
    compound.into_inner().into_inner()
}
#[test]
fn generated_doc_ppt_are_recognized_but_never_empty_success() {
    for (stream, fmt) in [
        ("/WordDocument", Format::Doc),
        ("/PowerPoint Document", Format::Ppt),
    ] {
        let bytes = fixture(stream, "Evidence café 😀");
        assert_eq!(detect(&bytes, None).unwrap().format, fmt);
        let error = legacy_office::extract(&bytes, Budget::default()).unwrap_err();
        assert_eq!(error, CoreError::UnsupportedLegacyFormat);
        assert_eq!(error.to_string(), "unsupported_legacy_format");
        eprintln!(
            "generated fixture: format={fmt:?}, bytes={}, result=unsupported_legacy_format",
            bytes.len()
        );
    }
}
#[test]
fn corrupt_wrong_family_and_limits_are_typed() {
    let bytes = fixture("/WordDocument", "evidence");
    assert_eq!(
        legacy_office::extract(b"broken", Budget::default()),
        Err(CoreError::Parse)
    );
    assert_eq!(
        legacy_office::extract(&fixture("/Workbook", "cell"), Budget::default()),
        Err(CoreError::Unsupported)
    );
    for budget in [
        Budget {
            max_input_bytes: 1,
            ..Budget::default()
        },
        Budget {
            max_memory_bytes: 1,
            ..Budget::default()
        },
        Budget {
            max_work_units: 0,
            ..Budget::default()
        },
        Budget {
            timeout_ms: 0,
            ..Budget::default()
        },
    ] {
        assert_eq!(
            legacy_office::extract(&bytes, budget),
            Err(CoreError::Budget)
        );
    }
    let token = Cancellation::default();
    token.cancel();
    let ctx = Context::new(Budget::default(), token).unwrap();
    assert_eq!(
        legacy_office::extract_with_context(&bytes, ctx),
        Err(CoreError::Cancelled)
    );
}
proptest! {
    #[test]
    fn arbitrary_embedded_unicode_never_becomes_evidence(text in "[a-zé東京🦀]{0,100}"){
        prop_assert_eq!(legacy_office::extract(&fixture("/WordDocument",&text),Budget::default()),Err(CoreError::UnsupportedLegacyFormat));
    }
}
