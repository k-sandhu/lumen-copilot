# Document taxonomy releases

ADR-0028 / #689 owns this backend reference data; #693 will define its API surface.
Version 1.0.0 contains 17 domains (including `other`), three-level families/types,
an optional NDA subtype decision and 10 independent evidence facets. Select by
primary business purpose, not filename extension. More specific domains take
precedence over generic correspondence/forms/data/presentation categories when
their defining purpose is supported. `other` is an affirmative category;
provider failure is a separate unclassified state. Facet absence from sampled
evidence is unknown, not a negative assertion.

Published release files are immutable. Add a new semantic version file rather
than editing a released one. Removed or renamed node IDs require a mapping from
the preceding release, whose keys cover exactly removed IDs and values identify
an existing target or null for retirement. Facet IDs use `facet:<id>` in mappings.
Retired IDs can never return. Meaning changes require a new ID; reviewers enforce
this semantic rule because text similarity cannot prove unchanged meaning.
Clarifications preserve IDs but still need a versioned release and review.

Run `uv run --project backend python -c "from app.classification.taxonomy import validate_history; validate_history()"`
and `uv run --project backend --extra dev pytest backend/tests/test_classification_taxonomy.py`.
The tests cover published history, hierarchy, positive signals, unknown-field
rejection, removal without mapping, invalid targets and retired-ID reuse.
No private documents or provider calls are needed.
