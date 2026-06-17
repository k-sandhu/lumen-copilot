"""Identity & tenant resolution — the only token validator.

Single responsibility (ADR-0004 boundary table): answer "who is asking" and
"which tenant" for every request. **Nobody else may resolve a user/tenant or
validate a token.** The resolved identity keys the permission filter in
``app.retrieval`` (mission filter #1, permissioned by default). The concrete
IdP is deferred (ADR-0003, CC-3); the OpenAPI ``bearerAuth`` scheme is reserved
for it. No identity logic exists yet — this is the reserved seam.
"""
