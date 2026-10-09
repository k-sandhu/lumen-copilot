//! Offline fixture driver, outside the pure core.
use lumen_docintel_core::{
    canonical::render,
    formats::{package::Limits, spreadsheets::extract},
};
fn main() {
    let result = std::env::args()
        .nth(1)
        .ok_or("missing fixture")
        .and_then(|path| std::fs::read(path).map_err(|_| "fixture read failed"))
        .and_then(|bytes| extract(&bytes, Limits::default()).map_err(|_| "extraction failed"))
        .and_then(|doc| render(doc).map_err(|_| "render failed"))
        .and_then(|doc| serde_json::to_string(&doc).map_err(|_| "serialization failed"));
    match result {
        Ok(json) => println!("{json}"),
        Err(safe) => {
            eprintln!("{safe}");
            std::process::exit(1);
        }
    }
}
