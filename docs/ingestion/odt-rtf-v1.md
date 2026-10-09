# ODT and RTF candidate extraction (#677)

Issue #677 specifies ordered headings, lists, tables and footnotes, declared RTF
character sets, and fail-closed malformed/deep inputs. Candidates return canonical
v1 from bytes plus package/runtime limits. They perform no I/O and have no child
document semantics. The generated tests are the acceptance corpus; no external
documents are used.

ODT reuses #711's bounded ZIP/XML reader. Require the text mimetype and the ODF
office/text/table namespaces; reject DTD/entities, encrypted entries, unsafe ZIP
names and unsupported repeated/merged/nested table shapes rather than fabricate
cells. Preserve mixed inline text, whitespace controls and XML paragraph paths.
Footnotes retain paragraph parentage. Heading levels 1..6 map to canonical roles.

RTF uses a bounded byte/control-word state machine, not conversion or execution.
Preserve paragraphs, supplied outline levels, list markers, rectangular tables
and text footnotes. Honour supported ansicpg encodings and signed UTF-16 Unicode
escapes, including surrogate pairs and scoped uc fallback counts. Reject malformed
numbers/escapes, unbalanced groups, excess depth, binary/object/image payloads and
unsupported encodings; unsupported content never becomes empty success. Formatting
controls may be ignored, but unknown starred destinations fail typed unsupported.
Source regions carry paragraph/table/footnote ordinal paths and original byte
ranges in diagnostics; canonical spans remain exact local Unicode code points.

Both input families share deadline/cancellation, accounted memory, work, output
and expansion budgets. Package limits are engineering defaults, not approved
production capacity. The optional facade requires independent default-OFF format
acceptance, candidate/shadow and cutover flags. Candidate calls do not persist,
publish or grant permissions. Python remains authoritative; live cutover requires
#687 and owner-approved baseline measurement. Upload acceptance is not widened.

Known candidate scope exclusions must return typed unsupported: RTF font-specific
charset switches, legacy codepage ambiguity, drawing/object/binary destinations,
and ODT table repeats/spans/nested tables. No claims of complete format fidelity
or production activation are made. Full baseline/RSS evaluation remains a merge
gate even when the generated acceptance corpus passes.
