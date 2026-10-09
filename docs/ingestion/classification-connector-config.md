# Classification configuration isolation — #742

The fail-closed ADR-0019 conformance partition explicitly classifies all eight
classification Settings fields. Model identifiers, taxonomy versions, artifact
checksums and scheduling interval are immutable deployment configuration. The two
local tokenizer artifact paths name orchestration-owned resources and remain
forbidden to connectors. Credentials remain behind the existing vault boundary.

Verification: the completeness gate first failed in #693's full offline suite;
`uv run --no-sync --extra dev pytest tests/test_connector_conformance.py` with
`PYTEST_ADDOPTS="-n 2"` passed 383 tests (9 capability skips), including synthetic
negative modules that attempt both forbidden artifact-path reads. Ruff passed.
