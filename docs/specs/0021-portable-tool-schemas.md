# Portable tool schemas and actionable validation

Tracking: [#623](https://github.com/k-sandhu/lumen-copilot/issues/623).

Tool definitions document purpose, input constraints, omission defaults and
result limits. Every object rejects additional properties. The local schema
retains legacy optional omissions. The gateway projects a separate schema in
which every property is required and optional values are nullable; it removes
schema defaults, closes nested objects and never mutates the local definition.
This projection is provider-neutral and does not enable a provider's native
strict-mode feature.

The governed runner validates after resolving the tool and checking its
allow-list, before approval prompts, handler execution or call-scope I/O.
Optional provider nulls restore documented omission defaults, including nested
objects; required nullable values remain present. Unknown null properties are
still rejected. Declared types, enums, bounds and nested constraints use the
same recursive validator for built-ins and MCP tools. MCP direct invocation
retains its validation boundary as defense in depth.
Object members of nullable type unions obey the same closure policy, including
objects with no declared fields. Normalization follows local references for
every value shape, including arrays, and selects applicable anyOf/oneOf branches
by their declared constraints; unknown fields remain invalid. Unconditional
allOf object declarations are combined before closure, retaining required fields
and intersecting constraints on shared properties. An undeclared sibling field
is rejected without making declared fields in separate fragments incompatible.
After successful normalization, approval records, handler arguments and
invocation/audit hashes identify the same canonical argument object. Invalid
calls retain the rejected input's hash and never reach approval or execution.

Invalid input returns tool_bad_args with a field path, the violated constraint
and a corrective next step. Configuration errors return a safe repair message;
neither schema error details nor argument values reach logs or audit summaries.
Validation uses an explicit offline reference registry: local references work,
remote references are rejected, and malformed references cannot perform I/O or
crash the answer stream. Tool invocation/outcome audits remain mandatory for
rejected calls.
Schema metadata is checked before normalization or projection. Invalid offered
definitions are excluded from the provider payload with a safe configuration
warning; an attempted invocation still returns tool_bad_args and emits the normal
invocation/outcome records. The context engine and gateway share provider tool
serialization, including reversible names and projected nullable/required/closed
schemas, for both initial assembly and later transcript fitting. Oversized fixed
segments retain the existing context_too_large refusal before provider I/O.

Verification covers nested schemas, legacy omissions, optional/required nulls,
unknown properties, bounds/enums, bad references, unchanged source schemas and
zero handler calls for invalid input. Discovery/range tools and call
deduplication are separate changes.
