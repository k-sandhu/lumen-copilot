# Concise provider-neutral tool-use policy

Tracking: #635. Depends on #436's inline evidence contract.

Grounded-answer v4 specifies short broad search, distinct refinement, discovery
for named documents, reading relevant ranges/neighbors and explicit continuation.
Only provided tools may be used. Canonical and legacy names describe the same
workflow; a prompt never grants a missing tool or bypasses its governance.

Each factual claim cites its exact visible S/W evidence. D handles and recalled
conversation prose are navigation only. Stop after sufficient evidence, avoid
identical unhelpful searches, ask when ambiguity changes retrieval intent, and
report unsupported answers with zero citations. No model/provider-native feature
is required. Prompt revision is recorded through the existing audit mechanism.

Acceptance tests verify this versioned policy, its concise length and lack of
provider-specific instructions. Runtime citation/permission regression tests
remain the structural enforcement; prompt wording is guidance, never authority.
