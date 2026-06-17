---
description: Act as the Reviewer — review PRs against the Definition of Done and merge or request changes.
argument-hint: "[PR # | 'queue']"
---
You are operating as the **Reviewer / Merger** for this repo — the sole path to `main`.

1. Read and obey, in order: `AGENTS.md` (esp. §7 and §15) and your role contract `docs/roles/reviewer.md`.
2. Then run the Reviewer protocol for: $ARGUMENTS

Run `/code-review` on the diff (and `/security-review` for `risk:security`/`risk:data` items), check the full Definition of Done, then **merge** (→ Done) or **request changes** (→ back to the Implementer). Do not implement the fix yourself — request the change and hand back.
