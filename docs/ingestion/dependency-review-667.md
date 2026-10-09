# Tokenizer dependency review — #667

Reviewed 2026-10-08. Exception owner: k-sandhu. Expires: 2026-11-07.

HF tokenizers 0.23.2 (Apache-2.0) uses paste 1.0.15 (MIT OR Apache-2.0) for
compile-time type/identifier concatenation. The macro receives the crate's
fixed Rust source, not document/tokenizer bytes, and does not ship as a runtime
document-processing API. Cargo.lock pins both. Default and hub features are
disabled; the core acquires no network client. sha2 0.10.9 is MIT OR Apache-2.0.

[RUSTSEC-2024-0436](https://rustsec.org/advisories/RUSTSEC-2024-0436.html)
reports unmaintained status, without a patched version or a listed runtime
vulnerability. Upgrading from tokenizers 0.22.2 to the currently available
0.23.2 did not remove this dependency. Review of tokenizers' uses and paste's
proc-macro manifest supports a narrowly scoped temporary exception, not a
general relaxation of unmaintained/advisory checks. Residual risk: future Rust
compatibility/security defects in an archived macro lack upstream maintenance.

cargo-deny 0.20.2 supports only an id/reason exception. The workspace's
`dependency_review` test restricts it to tokenizers 0.23.2/paste 1.0.15, rejects
additional paste consumers, and fails at the UTC expiry (2026-11-07). Every other
advisory, unmaintained crate and yanked version remains denied. A newer upstream
release replacing paste is the preferred removal path. Review or remove this
exception before expiry; never extend it merely to make CI pass.
