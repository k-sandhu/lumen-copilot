# Provider tool name compatibility

Tracking: [#612](https://github.com/k-sandhu/lumen-copilot/issues/612).
Related: [#575](https://github.com/k-sandhu/lumen-copilot/issues/575), which also
requires tenant catalog and registry integration and is not closed by this fix.

The LLM gateway translates unsafe or long tool identities to ASCII letters,
digits and underscores, at most 64 characters. MCP identities have a readable
`mcp__` prefix and a deterministic SHA-256 suffix to distinguish normalized names.
Already portable built-in names keep their spelling. A collision with any
offered or historical identity fails before the provider request.

One mapping covers offered definitions, historical calls, named results and
incoming calls. It is recreated deterministically per completion, including
history when a tool has been revoked. Returned names map back to the product
identity before governance. Unknown names remain unknown to the runner. The
mapping does not grant permissions, load MCP catalogs, or change raw remote names.
Assistant versions, policy entries, audit and trace rows keep their existing IDs.

Verification: synthetic provider payload/response tests cover long and unsafe
names, distinct normalized collisions, historical results, immutable inputs,
reverse mapping and collision rejection. Live name acceptance is part of the
separate opt-in model conformance suite.

The context engine counts projected provider names in offered tool schemas,
historical assistant calls and named tool results. It retains internal names in
returned domain messages and tool definitions. A fixed prompt that fits only
before name projection is refused with `context_too_large` before the next
provider request; the safety margin is not a substitute for counting these bytes.
Regression tests capture the gateway payload and compare its measured cost with
the context estimate, including revoked historical tools.
