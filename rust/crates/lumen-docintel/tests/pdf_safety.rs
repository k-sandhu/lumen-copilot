mod pdf_support;
use lumen_docintel_core::{
    CoreError,
    formats::pdf,
    runtime::{Budget, Cancellation, Context, Runtime},
};
use pdf_support::{objects, pdf_bytes, text};
use std::io::Write;

fn one_stream(content: &[u8], extra: &str) -> Vec<u8> {
    let mut stream = format!("<< /Length {} {extra} >>\nstream\n", content.len()).into_bytes();
    stream.extend_from_slice(content);
    stream.extend_from_slice(b"\nendstream");
    objects(&[b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>".to_vec(),b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>".to_vec(),b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>".to_vec(),stream],"")
}
#[test]
fn compressed_content_and_decompression_bomb_have_bounded_outcomes() {
    let content = text(40, 700, 12, "Compressed");
    let mut encoder = flate2::write::ZlibEncoder::new(vec![], flate2::Compression::default());
    encoder.write_all(content.as_bytes()).unwrap();
    let valid = one_stream(&encoder.finish().unwrap(), "/Filter /FlateDecode");
    assert!(pdf::extract(&valid, Budget::default()).is_ok());
    let mut encoder = flate2::write::ZlibEncoder::new(vec![], flate2::Compression::best());
    // Expansion is generated in fixed windows without a large fixture allocation.
    for _ in 0..8192 {
        encoder.write_all(&[b' '; 8192]).unwrap();
    }
    let bomb = one_stream(&encoder.finish().unwrap(), "/Filter /FlateDecode");
    let started = std::time::Instant::now();
    assert_eq!(
        pdf::extract(
            &bomb,
            Budget {
                max_memory_bytes: 1024 * 1024,
                ..Budget::default()
            }
        ),
        Err(CoreError::Budget)
    );
    assert!(started.elapsed() < std::time::Duration::from_secs(2));
    assert!(pdf::extract(&valid, Budget::default()).is_ok());
}
#[test]
fn encryption_cycles_deep_objects_and_unknown_evidence_fail_closed() {
    let encrypted = objects(
        &[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [] /Count 0 >>".to_vec(),
        ],
        "/Encrypt 3 0 R",
    );
    assert_eq!(
        pdf::extract(&encrypted, Budget::default()),
        Err(CoreError::Unsupported)
    );
    let cycle = objects(
        &[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [2 0 R] /Count 1 >>".to_vec(),
        ],
        "",
    );
    assert_eq!(
        pdf::extract(&cycle, Budget::default()),
        Err(CoreError::Parse)
    );
    let deep = objects(
        &[format!("{}0{}", "[".repeat(256), "]".repeat(256)).into_bytes()],
        "",
    );
    assert_eq!(
        pdf::extract(&deep, Budget::default()),
        Err(CoreError::Budget)
    );
    let unknown = pdf_bytes(&[b"BT /F1 12 Tf 40 700 Td (Hidden) Tj ET /Unknown gs"], 0);
    assert_eq!(
        pdf::extract(&unknown, Budget::default()),
        Err(CoreError::Unsupported)
    );
    let enormous = one_stream(b"q Q", "/Length 4294967295");
    assert_eq!(
        pdf::extract(&enormous, Budget::default()),
        Err(CoreError::Parse)
    );
}
#[test]
fn parallelism_determinism_and_cancellation_use_the_foundation_runtime() {
    let content = text(40, 700, 12, "Healthy");
    let input = pdf_bytes(&[content.as_bytes(); 12], 0);
    let mut documents = vec![];
    for width in [1, 2, 4] {
        let runtime = Runtime::new(width, 1).unwrap();
        let ctx = Context::new(Budget::default(), Cancellation::default()).unwrap();
        let doc = pdf::extract_with_context(&input, &ctx, &runtime).unwrap();
        documents.push(doc.blocks);
    }
    assert!(documents.windows(2).all(|d| d[0] == d[1]));
    let token = Cancellation::default();
    token.cancel();
    let ctx = Context::new(Budget::default(), token).unwrap();
    assert_eq!(
        pdf::extract_with_context(&input, &ctx, &Runtime::new(2, 1).unwrap()),
        Err(CoreError::Cancelled)
    );
}
proptest::proptest! {
    #![proptest_config(proptest::test_runner::Config::with_cases(128))]
    #[test]
    fn arbitrary_bytes_never_panic(bytes in proptest::collection::vec(proptest::num::u8::ANY,0..4096)) {
        let result=std::panic::catch_unwind(||pdf::extract(&bytes,Budget{timeout_ms:100,max_memory_bytes:1024*1024,max_work_units:1000,..Budget::default()}));
        proptest::prop_assert!(result.is_ok());
        proptest::prop_assert!(result.unwrap().is_err());
    }
}
