"""Retrieval adapter — the only issuer of vector + lexical search.

Single responsibility (ADR-0004 boundary table): hybrid search (pgvector
semantic similarity fused with Postgres full-text) followed by a cross-encoder
re-rank before context assembly. **Nobody else may issue a pgvector / full-text
search query.** The permission filter is applied **inside** this module, keyed
off the identity resolved in ``app.auth`` — there is no unfiltered retrieval
path (CC-1, mission filter #1). Passages carry source + char offsets through to
the answer for citations (CC-11). No retrieval logic exists yet — this is the
reserved seam.
"""
