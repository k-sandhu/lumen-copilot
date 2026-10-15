# Residual QA execution — issue 777

Tracking: [issue 777](https://github.com/k-sandhu/lumen-copilot/issues/777),
[PR 778](https://github.com/k-sandhu/lumen-copilot/pull/778), stacked on unmerged
PR 775. Scope: the 39 residual checks from the earlier partial-coverage report,
with missing prerequisites provisioned and normal Chrome showing real behavior.
This report does not rewrite the historical 71-area outcomes or certify a release.

## Versions and test context

| Phase | Application / runtime | Definition and method |
|---|---|---|
| Functional Chrome | Clean application `3c022fcc4880228636a3cfda067c7014c89441f9`; backend image `5cc09be8…`, frontend `7a99a3e2…`; localhost:47180; new synthetic QA 777 Alpha/Beta | QA definitions `2d2f2517…`; real Chrome + authenticated read-back. Existing standard local DB connection, no new RLS backstop certificate. Ling 3.0 Flash + Nemo embedding 2048; summaries on, keep 4/minbatch 2; access 900 s. Full digests/configuration are in each ledger. |
| Expiry / concurrent tabs | Same functional app/config; two tabs offline for 920.254/912.915 s, then real protected requests | Definitions `aca946cf…` committed before expiry execution, allowing measured natural 900 s expiry. No shortened-lifetime claim. |
| Eval appearance/build | Same static frontend; modified mounted Eval backend at app base 3c022fcc, patch SHA `ea4797f2…`; model GPT 6 Luna/text-embedding-3-large 2048 | Definitions `da2abe21…`; appearance restored Aurora/System/Cozy. No transfer of Eval provider/failure timing to clean canonical configuration. |
| Clean integration | Clean archive app `70c3c2e4775aeb031012625bc018f6dd2d96ba04` loaded with explicit PYTHONPATH inside pinned harness; test fixture commit `0e9fd8f43b0a5358eda3e1f99e3712eee9135313` | Independent database/index/Redis resources; serial live tests. Protocol golden summaries **off**, distinct from sequential Chrome companion with summaries **on**. |
| Fresh main restart/smoke | Main `70c3c2e4775aeb031012625bc018f6dd2d96ba04`; backend/worker/media/beat image `60ab7f9168052e3b82af97ee43abce1c77d806c7c40043126ec9b52a6bd172d6`, frontend unchanged `7a99a3e2…`; same configuration and database volumes | After the completed older-runtime soak, all 205 Python files match main by hash, no source mounts. Readiness 6/6; fresh Silver Maple upload Ready, exact code/date cited and citation opened; owner text200, member/foreign404. Two atomic smoke results only; the 39 earlier residual results are not inherited. |
| Hardened login | Dedicated `qa777_app` NOSUPERUSER/NOBYPASSRLS app, own database/index/bucket | Frozen failure #644: valid login casts bypass sentinel to UUID. Restricted role unchanged. Later user approval permits functional use of standard local connection; this does not turn the hardened failure into a pass. |

A final content-hash audit corrected an earlier provenance assumption: canonical
Chrome used backend 3c022fcc, while the clean harness archive at fixture commit
0e9fd8f4 contains the exact main70c3c2e4 application tree. All four intervening
backend files were compared against both Git revisions and both containers. The
frontend trees match; the backend trees differ by the #759 usage-accounting fix.
Raw setup contexts with the old label are preserved alongside
[the retained provenance comparison](../../qa/evidence/qa777-20261015-provenance.json); new ledgers use the corrected revision.
The dependency-fault phase explicitly lists its mixed component versions.
Repository/test-definition commits and deployed application commits remain
separate. Application fixes are separate issues; no PR merged.

## Outcomes


Latest residual-case outcomes: **39/39 recorded** — **10 failed**, **5 partial**, **24 passed**.

These are scoped residual results, not automatic promotion of entire catalog areas. Case 050 has both a passing protocol threshold result and a partial Chrome companion; the table retains the latest companion limitation. Phase ledgers preserve both.

| Case | Result | What was verified / remaining gap |
|---|---|---|
| QA-FILL-001 | passed | Two Chrome tabs expired for >900 s: 401 → refresh 200 → retry 200. Rotation/replay/logout negatives passed; hardened login #644 remains separate. |
| QA-FILL-003 | passed | Owner/member/security/backup/foreign role matrix; explicit user/group/collection grants and revocation; forbidden direct reads 404. |
| QA-FILL-004 | passed | Permitted-only live search and ranking assertions. Fixed-vector fault tests are not semantic-quality evidence. |
| QA-FILL-005 | failed | Permitted initial answer cited; explicit factual follow-up asserted the date without a citation (#781). Unauthorized retrieval/direct citation boundaries passed. |
| QA-FILL-006 | passed | Fresh two-message disposable chat renamed, reloaded/reopened, deleted in Chrome; session/messages 404 and absent from history. |
| QA-FILL-007 | failed | Real Redis ordering/replay tests passed. Guarded active Chrome reload lost the assistant output and left only the user message (#348). |
| QA-FILL-008 | failed | Real summary v1, coverage cursor, ID-only evidence and system-origin audit persisted; prose still retained a source-only code (#427). Post-summary answer cited correctly. |
| QA-FILL-009 | passed | Approved 144-byte file deleted in Chrome. List, row, chunks and object absent; text/access capability 404; exact local backup retained. |
| QA-FILL-010 | passed | TXT/PDF/DOCX/PPTX/XLSX canaries reached Ready; real DOCX re-ingestion replaced its chunk at ordinal 0 without duplicates. |
| QA-FILL-013 | passed | Owner-only public source synced one document; member/foreign denied. Approved disposable source removal cascaded collection/document/chunk/object; direct reads 404. |
| QA-FILL-016 | failed | 463 public models discovered. Discovered Ling invocation failed without a provider adapter route (#782). Disposable provider removed; models disappeared; secret/tenant negatives passed. |
| QA-FILL-017 | passed | Fragment reconstruction, prompt/cache budget and compression assertions passed; Chrome context corroborated usage. |
| QA-FILL-018 | partial | Preview, distinct backup, publish v1/v2, rollback→v3, governance restoration→v4 persisted. Live saved-case round trip blocked by consecutive native-prompt control limitation; store/verdict unit tests passed. |
| QA-FILL-019 | passed | Published launch, certification/feature state, disable/denied launch, invalid ownership/foreign backup transitions and restoration verified. |
| QA-FILL-020 | passed | Unknown/disabled/unapproved calls executed zero handlers and emitted denial evidence. Approved bounded Python call separately audited; gate restored. |
| QA-FILL-021 | failed | One actual print(777 + 1) succeeded: stdout 778, exit 0, 93 ms, no packages/artifacts; started/finished audit. Initial sandbox-disabled attempt denied. Both gates restored. write_file unavailable (#576); fresh web search still rate-limited. |
| QA-FILL-022 | passed | Chrome Context7 discovery + two real T0 MCP calls succeeded. Allowlist provisioned through supported API because editor omits MCP (#575); typed-error/egress negatives passed. DeepWiki T2 unchanged. |
| QA-FILL-027 | failed | Cadence/DST/overlap fixtures passed, but actual due schedule did not dispatch (#718); manual run worker failed while API/UI stayed Running (#578). Schedule paused. |
| QA-FILL-030 | partial | Filtered two policy audit rows, role denials and 11 CSV assertions passed. Browser Blob download observer timed out; actual CSV bytes unproven (#588). |
| QA-FILL-031 | passed | QA-only budget 20→5 read back and restored to 20; non-admin mutation 403 and both audited updates verified. |
| QA-FILL-034 | passed | Adversarial filename normalized, exact-byte round trip and cross-tenant denial passed. Raw adapter teardown and product document deletion both verified. |
| QA-FILL-035 | passed | Invalid settings, malformed inputs and problem-envelope/unknown-exception tests passed in scoped suites; safe readiness UI failure corroborated. |
| QA-FILL-036 | passed | Nonexistent model preference rejected 422; original valid selection unchanged on API read-back and Chrome reload. |
| QA-FILL-037 | passed | Expired access recovered in both tabs with one refresh per tab; logout then other-tab reload reached stable login. Client retry/credentials/header fixtures passed. |
| QA-FILL-040 | failed | All 14 theme/mode states and System emulation exercised/restored. Small light-theme links Graphite 4.095, Meridian 3.922, Sunset 3.670 below 4.5:1 (#779); default Aurora passed. |
| QA-FILL-043 | passed | Reading position stayed at scrollTop 0 while output grew 7316→7375 characters. Completed answer reloaded stably without duplicate text. Interruption defect recorded separately. |
| QA-FILL-044 | passed | Owner/shared/denied citation access and answer-time/access/freshness semantics checked; no forbidden passage shown. |
| QA-FILL-046 | passed | 69 frontend residual assertions, including21 real Vite build/layout guards; dev payload excluded with positive flag-on control, Markdown lazy-split, deployed dev routes404. |
| QA-FILL-049 | passed | Owner upload→grounded citation positive; same-tenant member and foreign user could not retrieve/open private fact/document. |
| QA-FILL-050 | partial | Protocol aggregate passed 0.8 thresholds: groundedness 0.8333, citation 0.8333, recall 1.0 (summaries off). Chrome companion (summaries on): 4/5 supported answers correct+cited; runbook answer falsely refused despite exact retrieval (#785), ticker negative honest. Safe clarification scorer false-negative separately #784. |
| QA-FILL-052 | failed | Compression/budgets passed. No valid clean first-token sample: cached embedding client closed-loop failure blocked setup (#780). Separate modified Eval unreachable-provider terminal 25.062 s ≤ 30 s is not a canonical latency pass. |
| QA-FILL-056 | partial | 54 named Chrome controls/lang=en, 12 Tab steps with visible focus and scoped keyboard/ARIA tests. No actual screen reader or full external accessibility scan; untested assistive behavior remains. |
| QA-FILL-059 | passed | Completed measured two-hour workload; see stability section. Bounded workload, no general memory-leak certification. |
| QA-FILL-060 | partial | Own storage outage/restart recovered bytes but adapter leaked vendor connection exception (#783). Isolated worker pause/queue/drain, Redis outage/recovery, ordered backplane and own-index write-block/idempotent retry passed; fixtures removed. In-flight ingestion crash/whole-search-engine outage untested. |
| QA-FILL-062 | passed | Populated 0048 dump restored into separate DB, upgraded to 0049 and clean app started; 2 tenants/4 users/doc/chunk/vector/grant and owner/grantee 200 vs ungranted/foreign 404. Both fixture DBs dropped after idle/count guards; backup retained. |
| QA-FILL-063 | passed | Two harmless source/tool-reply injection payloads × owner/granted member: correct cited canaries, no takeover marker/action, private follow-up denied. Only retrieval tools invoked. |
| QA-FILL-066 | passed | Two real expired tabs concurrently recovered 401→refresh200→retry200 with no loop; replay/rotation fixtures passed. Bounded timing observation, not all race interleavings. |
| QA-FILL-067 | failed | Bounded valid DOCX expansion safely rejected at 2M-character cap before embedding, 0 chunks/text 409. The 77-byte corrupt PDF exposed raw PdfStreamError (#384); transient recovery UI stale until reload (#767). General CPU/OOM isolation untested. |
| QA-FILL-070 | failed | Confirmed Stop generating and 712 rendered characters immediately before reload. History afterward contained only the user message, no recovered prefix/error/recovery control (#348). |

## Automated evidence

The clean harness uses dependency image22530ff5 with explicit archive PYTHONPATH; the final deployed image60ab7f91 inherits dependency base5cc09be8. These are distinct contexts, recorded in the provenance comparison.

Primary clean main 70c3c2e4 offline backend run: **1019 passed, 8 live exclusions, 1 known xfail #421** (1028 total), zero failures/errors. The HTTP auth negatives were captured earlier at 21:37 UTC using unchanged criteria from definition2d2f2517; the natural-expiry Chrome method ran after aca946cf at 22:35 UTC. They are labelled supplementary evidence rather than a new HTTP execution after the definition change.

Frontend workflow group: **391 passed in 42 files**; earlier 69-test residual group includes 21 real Vite build/layout guards. Clean live RLS/permission group: **6 passed** with temporary restricted roles and teardown. Dedicated S3/CORS live group: **3 passed**. Real Redis replay group: **2 passed**. Counts overlap between targeted and expanded runs and must not be added as independent coverage.

The existing six-question real-model protocol golden test passed its original 0.8 aggregate thresholds: groundedness 0.833333, citation correctness 0.833333, retrieval recall 1.0. Per-item results are retained. Its unanswerable case scored false on groundedness/citation because the scorer did not recognize a safe clarification (#784); a focused diagnostic preserves that failure without rewriting scores or thresholds. In Chrome, four of five supported questions answered correctly with citations; the runbook question falsely refused although the exact supporting passage was retrieved (#785). The unsupported ticker question refused honestly.

Clean latency group: one quality test passed and two latency setup errors; no valid first-token timing was measured. Reproducer first embedding task succeeded with 2048 dimensions; second task on a new event loop failed with cached-client closed-loop error (#780). Separate modified-Eval unreachable-provider terminal result 25.062 s within 30 s remains explicitly separate.

Setup failures were retained: RLS document fixture/SQL privileges, missing mounted contracts, stale migration-head assertion, S3 CORS, live/unit environment contamination, stale fixture identity and missing xdist databases. Only test prerequisites were repaired. Re-run results are linked individually rather than replacing red evidence. Raw JUnit hashes, suite counts and per-test suite ledgers are indexed.

## Measured stability

Completed workload: **7200.0 seconds**, **463 iterations**, **0 request errors**, **7 successful natural refreshes**, **47 empty disposable chats deleted**. The soak itself made zero model calls; manual Chrome concurrently exercised model/upload/search flows.

| Container | First / last MiB | Observed min / max MiB |
|---|---|---|
| lumen-copilot-backend-1 | 629.90 / 642.70 | 619.40 / 758.80 |
| lumen-copilot-worker-1 | 638.20 / 644.00 | 632.90 / 698.40 |
| lumen-copilot-postgres-1 | 73.56 / 117.50 | 73.56 / 127.00 |

One of the 20 connection samples contained one idle transaction; the other 19 contained none. No duration is inferred from that single sampled observation.

Direct database connections were sampled only for the final **2400.0 seconds**, every 120 s: total range [11, 11], idle-in-transaction maximum 1. This is not two hours of direct connection sampling. Final container restart/OOM state is retained in stability-derived.json.

Memory increased during concurrent ingestion/model activity. This finite mixed workload does not establish absence of a memory leak or behavior at production load.

## Cleanup, restoration and retained evidence

The user approved the named disposable file deletion, cleanup of new items explicitly labelled QA 777 disposable, and one bounded Python expression with the gate restored. Chrome removed that file, a fresh two-message disposable chat, the new public source/collection/document, and the new disposable provider; direct reads/object absence and model-list removal were verified. Retained evidence chats, private/golden documents, MCP servers and published assistants remain inspectable. Existing populated Eval data was not changed.

Python executed only `print(777 + 1)`: one succeeded run, stdout 778, exit 0, 93 ms, no packages/artifacts, exact executor image `sha256:8161fe4f1aa127d6a258bcb8c1b470957ffeac1d40fb535c995e3d4255713379`. Its preceding denied attempt did not execute code. Start/finish audits and tool traces persist. The explicitly disposable sandbox was then closed through the supported lifecycle API while execution stayed disabled; its container is absent and the code-run/history evidence remains. Tenant code execution is Off again and run_python Requires approval is On. The QA 777 schedule is paused; appearance/network/viewport overrides restored. Final read-back found an intervening audited resume at 23:00:44 UTC; Chrome paused it again at 23:17:55, and both reload and API read-back confirm disabled. No cause for that intervening action is attributed.

Both populated-upgrade fixture databases were verified idle with expected counts and dropped without FORCE; dump retained by SHA256 `021c82d8982f48eb3a2b6656c420d33a7fffb3b33656d9447d69b97405b702e8`. Isolated fault worker/Redis containers were verified by exact ID/labels, drained and removed; scratch search index/pipeline removed. The dedicated QA MinIO restart preserved its own data volume. Docker startup repair preserved old runtime directories; no data-volume reset.

Exact upload fixtures and hashes: [`qa/fixtures/777`](../../qa/fixtures/777). Immutable run records: [`qa/runs`](../../qa/runs). Sanitized artifact index: [`qa/evidence/qa777-20261015-index.json`](../../qa/evidence/qa777-20261015-index.json). Raw screenshots/JUnit/traces remain **local, ignored** at `output/qa/777`; preserve that directory before local cleanup. Hash references do not imply cloud retention. CI artifacts retain 90 days. Logs/env files are not committed.

## Remaining limits and findings

New tracked findings: [779 contrast](https://github.com/k-sandhu/lumen-copilot/issues/779), [780 embedding lifecycle](https://github.com/k-sandhu/lumen-copilot/issues/780), [781 uncited follow-up](https://github.com/k-sandhu/lumen-copilot/issues/781), [782 provider route](https://github.com/k-sandhu/lumen-copilot/issues/782), [783 storage error boundary](https://github.com/k-sandhu/lumen-copilot/issues/783), [784 scorer clarification](https://github.com/k-sandhu/lumen-copilot/issues/784), [785 retrieved-answer refusal](https://github.com/k-sandhu/lumen-copilot/issues/785). Existing issues 348/384/427/575/576/578/644/718/767 received linked corroboration where relevant. No fixes or clean-release/security certification in this change.

Live saved-case persistence/rerun, browser CSV bytes, actual screen reader/full scanner, general parser containment, in-flight ingestion crash/whole-search-engine fault recovery, and valid clean first-token measurement remain explicit. The first three are capability/evidence limits; the latter cases retain their failures/partial scope.

An earlier automatic approval review rejected opening Chrome’s internal downloads page under its URL policy. It was not retried or bypassed; the CSV byte result remains unconfirmed.

## Verification and review

Final local verification: 21 tracking regression tests passed; validator accepted 71 catalog entries and128 concrete cases including all new records; scoped Ruff lint/format and git diff checks passed. PR 778 remains stacked on PR 775; its updated description links the exact final CI run. No merge performed.
