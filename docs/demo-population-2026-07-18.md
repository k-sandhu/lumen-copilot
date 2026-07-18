# Northstar Harbor demo population — 2026-07-18

Issue: [#449](https://github.com/k-sandhu/lumen-copilot/issues/449)

This record describes the persistent demo data loaded into the long-running local
Docker Compose stack. It contains no provider credential or other secret.

## Access

- Frontend: <http://localhost:47180>
- Tenant: `Northstar Harbor Group`
- Tenant ID: `69e03073-4f79-4522-9245-e8af191b9063`
- Shared local-demo password: `LumenDemo!2026`

| User | Roles | Focus |
|---|---|---|
| `ava.chen@northstar.test` | member, admin | executive strategy and board decisions |
| `marco.ruiz@northstar.test` | member | operations and customer experience |
| `priya.shah@northstar.test` | member | finance and commercial planning |
| `elena.petrov@northstar.test` | member, security | legal, compliance, and supplier risk |
| `jordan.lee@northstar.test` | member | engineering, security, and AI governance |

## Pinned models

- Chat and headless assistant runs: `openrouter/tencent/hy3:free`
- Embeddings: `openai/nvidia/nemotron-3-embed-1b:free`
- Persisted embedding width: 2,048 dimensions

The chat model registry contains only Hy3. The embedding migration pads existing
vectors to the Nemotron model's native width and deliberately omits a pgvector HNSW
index because pgvector's HNSW `vector` operator class is limited to 2,000 dimensions.
The derived OpenSearch index remains the serving retrieval index.

## Loaded inventory

| Item | Count | Verification |
|---|---:|---|
| Users | 5 | tenant-scoped SQL count |
| Collections | 7 | five primary plus two web-source collections |
| Audited collection grants | 19 | tenant-scoped SQL count |
| Public web sources | 2 | both `ready`, one indexed document each |
| Ready documents | 23 | all 23 in `ready` state |
| Indexed chunks | 44 | PostgreSQL and OpenSearch both report 44 |
| Interactive chats | 12 | 3 Ava, 2 Marco, 2 Priya, 2 Elena, 3 Jordan |
| Interactive messages | 54 | 27 user plus 27 Hy3 assistant turns |
| Grounded citations | 108 | persisted citation rows |
| Saved assistants | 5 | all published; seven immutable versions |
| Schedules | 5 | four enabled and one paused |
| Public MCP servers | 2 | both `ready`; seven discovered tools |
| Tool invocations | 140 | retrieval, document, web-search, and policy probes |
| LLM usage records | 29 | 27 chat answers plus two headless runs |
| Successful headless runs | 2 | 778 run steps and four deliveries |
| Audit events | 588 | tenant-scoped append-only audit rows at final count |

Every one of the 44 stored chunk embeddings has `vector_dims(embedding) = 2048`.
Every LLM usage row records `openrouter/tencent/hy3:free`; together they account for
392,897 tokens in the persisted usage ledger.

### Document mix

The 21 uploaded originals and two public-source snapshots cover:

- two styled Word documents;
- two multi-page PDFs;
- one four-slide PowerPoint deck;
- one four-sheet Excel workbook with formulas, checks, and a chart;
- eleven Markdown records and six plain-text records.

The content spans strategy, board packets, incident response, postmortems, service
levels, budgets, forecasts, vendor risk, compliance controls, model risk, MCP
governance, security exceptions, and operating procedures. The Word, PDF,
PowerPoint, and Excel artifacts were rendered and visually inspected before upload;
the workbook inspection found no formula-error tokens.

## Assistants and governance

| Owner | Assistant | Autonomy | Notable capabilities |
|---|---|---|---|
| Ava | Executive Decision Brief | suggest | grounded search, document reads, clarification |
| Marco | Incident Operations Coordinator | draft | grounded search, clarification, governed file drafting |
| Priya | Finance Scenario Analyst | act with approval | grounded search and approval-gated Python |
| Elena | Compliance Evidence Reviewer | act with approval | grounded search, web search, Microsoft roadmap MCP tool |
| Jordan | Engineering Research Scout | act with approval | grounded search, web search, DeepWiki MCP tool |

The tenant autonomy ceiling is `act_with_approval`. A Python execution probe was
denied with `approval_denied`, proving that T2 execution remains gated. Web search
was exercised successfully four times. MCP servers and their tool schemas were
discovered and tested; no consequential MCP action was forced during population.

## Schedules

All schedules use `America/Toronto` and cover raw/structured recurrence, overlap,
delivery, and paused-state behavior:

- Ava: weekdays at 07:15, skip overlap, daily digest, enabled;
- Marco: Sundays at 08:30, queue overlap, weekly digest, enabled;
- Priya: monthly on day 1 at 10:00, skip overlap, enabled;
- Elena: quarterly on day 1 at 09:00, allow overlap, enabled;
- Jordan: daily at 16:45, daily digest, paused.

Manual fires of Ava's and Marco's definitions both completed through Celery. Their
summaries, transcripts, steps, citations, usage, audits, and inbox/digest deliveries
are persistent.

## Public sources

- [Model Context Protocol remote-server registry documentation](https://modelcontextprotocol.io/registry/remote-servers)
- [Microsoft Release Communications MCP documentation](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/mrc-mcp?view=o365-worldwide)
- DeepWiki public MCP: `https://mcp.deepwiki.com/mcp`
- Microsoft Release Communications public MCP: `https://www.microsoft.com/releasecommunications/mcp`

## Verification record

- Owned document fetch as Priya: HTTP 200.
- Unowned Jordan document fetch as Priya: HTTP 404 (existence non-disclosure).
- PostgreSQL ready document/chunk counts: 23/44.
- OpenSearch tenant chunk count: 44.
- Both public sources and both MCP servers: `ready` with no current error.
- Affected backend suite: 126 passed; four explicit offline/live skips.
- Ruff lint: green.
- Mypy strict application check: green across 163 source files.
- Docker services: running; health-gated services healthy after restart.
- Repository-wide Ruff formatting is pre-existingly red on 96 files. The new
  migration is formatted; unrelated files were not bulk-reformatted. Residual risk:
  the repository's global format gate remains noisy until a separately scoped cleanup.

## Operational note

The requested Hy3 free route is scheduled by OpenRouter to expire on 2026-07-21.
This population intentionally retains that exact requested model; replace the model
pin before the expiration if the demo must keep generating new answers afterward.
