#[test]
fn advisory_exception_is_scoped_and_expires() {
    use std::time::{SystemTime, UNIX_EPOCH};
    // cargo-deny 0.20.2 supports id/reason only; enforce expiry and scope here.
    let utc_days = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
        / 86400;
    assert!(
        utc_days < 20764,
        "RUSTSEC-2024-0436 exception expired; review/remove it"
    );
    let lock = include_str!("../../../Cargo.lock");
    assert!(lock.contains("name = \"tokenizers\"\nversion = \"0.23.2\""));
    assert!(lock.contains("name = \"paste\"\nversion = \"1.0.15\""));
    let dependencies: Vec<&str> = lock
        .split("[[package]]")
        .filter(|p| p.contains(" \"paste\",\n"))
        .collect();
    assert_eq!(
        dependencies.len(),
        1,
        "exception must not admit new paste consumers"
    );
    assert!(dependencies[0].contains("name = \"tokenizers\""));
}
