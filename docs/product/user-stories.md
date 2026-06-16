# Lumen Copilot — User Stories

Status: candidate product-scope input.
Last updated: 2026-06-16.
Tracking issue: https://github.com/k-sandhu/lumen-copilot/issues/1.

Comprehensive, **product-agnostic** user stories for a knowledge-work-automation product, consolidated from the research in [knowledge-work-automation-research.md](knowledge-work-automation-research.md). Vendor and product names are deliberately **removed here** (they live in the research doc); these stories describe capabilities, not any one competitor.

This document is discovery input. It does **not** decide final product scope, stack, architecture, security invariants, or launch order, and it does **not** close OD-1 in [../specs/0001-open-decisions.md](../specs/0001-open-decisions.md).

## How to read this document

Every story uses: **As a [persona], I want [capability], so that [benefit].** Stories carry an ID (`E<epic>-<n>`) and one or more **AC** (acceptance-criteria) bullets. The format is intentionally aligned with `scripts/stories-to-issues.ps1` so this file can seed one issue per story (`-StoriesFile docs/product/user-stories.md`). Acceptance criteria are implementation-agnostic because the stack is an open decision (OD-2).

## Global acceptance criteria

Apply these to every story where relevant (so per-story ACs can stay short):

- **Permission-aware:** the user can only see, summarize, transform, or act on data they are authorized to access in the source system.
- **Source-grounded:** answers and generated artifacts cite the sources used, unless the output is explicitly unaided drafting.
- **Freshness-visible:** users can see source recency, sync status, or a warning when context may be stale.
- **Uncertainty-aware:** the product says when evidence is weak, conflicting, missing, or out of scope.
- **Audit-ready:** searches, answers, agent runs, tool calls, approvals, and write actions produce logs suitable for admin/security review.
- **Approval-gated:** consequential writes, external sends, destructive changes, spending, access grants, and sensitive communications require human approval unless an admin explicitly allows automation.
- **Reversible where possible:** write actions show a preview and support undo, rollback, or a compensating action where the source system permits it.
- **Governed:** admins can disable sources, tools, models, agents, and capabilities by user group, role, workspace, department, or risk tier.
- **Feedback-ready:** users can rate, correct, or flag poor answers, bad citations, unsafe actions, and stale knowledge.
- **Privacy-respecting:** personal context is explainable and controllable by the user, within admin policy.

## Personas

| Tag | Persona | Description |
|---|---|---|
| **KW** | Knowledge Worker | General employee working across docs, messages, meetings, tasks, and business apps. |
| **NH** | New Hire | Employee onboarding into teams, tools, acronyms, and history. |
| **MGR** | Manager | People or project manager responsible for priorities, blockers, and updates. |
| **EXEC** | Executive | Leader who needs concise, decision-ready context across teams and systems. |
| **OPS** | Operations Owner | Owner of repeatable business processes across teams and tools. |
| **BLD** | Citizen Builder | Non-technical user building agents or automations for a team. |
| **DEV** | Developer / Platform Engineer | Technical builder extending connectors, APIs, tools, and agents. |
| **ADM** | Admin | Owner of platform rollout, configuration, sources, permissions, and adoption. |
| **SEC** | Security / Compliance | Owner of risk, access, sensitive data, audit, and AI governance. |
| **KM** | Knowledge Manager | Owner of content quality, verification, freshness, and source-of-truth behavior. |
| **ENG** | Engineer | Software engineer working across code, tickets, incidents, docs, and reviews. |
| **PM** | Product Manager | Product owner using research, feedback, roadmap, engineering, and customer context. |
| **SALES** | Sales | AE, SDR, solutions, or revenue team member. |
| **CS** | Customer Success | Owner of account health, adoption, renewal, and escalations. |
| **SUP** | Support | Support/service agent resolving customer or employee tickets. |
| **IT** | IT / Service Desk | Owner of internal technical support, access, devices, and service workflows. |
| **HR** | HR / People Ops | Owner of policies, onboarding, employee support, recruiting, and people workflows. |
| **MKT** | Marketing | Owner of messaging, campaigns, content, SEO, events, and customer proof. |
| **LEGAL** | Legal / Compliance Counsel | Owner of contracts, policy, reviews, evidence, and legal self-service. |
| **FIN** | Finance / RevOps | Owner of forecasts, close, spend, pipeline, reporting, and operational metrics. |
| **DATA** | Data Analyst | User who analyzes structured and unstructured data together. |
| **ANALYST** | Research Analyst | User doing document-heavy research, diligence, market, competitive, or investment-style evidence work. |
| **AGENTOPS** | AgentOps Owner | Owner of agent inventory, quality, cost, observability, and lifecycle. |

---

# EPIC 1 — Enterprise Context Foundation

Connect the systems where work happens, preserve permissions, and build the governed context layer that powers every search, answer, artifact, and action.

**E1-1.** As an **ADM**, I want to connect core enterprise systems (productivity suites, chat, ticketing, CRM, code, support, HR, storage, data warehouses), so that the product can reason over the places where work already happens.
- **AC:** Admins can view each connector's required permissions, sync mode, supported object types, and read/write capability before connecting.
- **AC:** Each source records owner, setup status, last successful/failed sync, error count, indexed object count, and permission-sync status.
- **AC:** Read-only connectors are distinguished from write-capable ones; a source can be disabled without deleting its configuration.

**E1-2.** As a **SEC** owner, I want source-system permissions mirrored into the product, so that search, answers, artifacts, and agents never reveal information beyond a user's existing access.
- **AC:** Content visibility is checked against the requesting user's effective access at retrieval time.
- **AC:** Revoked source access is reflected within a documented sync target; private channels, restricted files, confidential tickets, and hidden records are excluded for unauthorized users.
- **AC:** Permission failures are logged without exposing restricted titles or snippets.

**E1-3.** As a **KW**, I want to know whether source context is current, so that I can decide whether to trust an answer or verify it manually.
- **AC:** Results and citations expose last-modified and, where possible, last-indexed time.
- **AC:** Answers warn when important sources are stale, failed to sync, or were excluded.
- **AC:** Users can report "this looks stale" from search, answers, and generated artifacts.

**E1-4.** As a **KW**, I want the product to understand relationships between people, teams, projects, customers, documents, meetings, tickets, code, and business records, so that it can answer questions that span systems.
- **AC:** Documents, tickets, meetings, threads, code changes, and owners can be associated with a project or customer when evidence supports it.
- **AC:** Entity summaries show aliases, owners, related sources, recent activity, and confidence; ambiguous names trigger disambiguation.
- **AC:** Users can correct an entity relationship, subject to permission and governance rules.

**E1-5.** As a **DEV**, I want to build custom connectors for proprietary systems, so that internal tools can participate in search and automation.
- **AC:** Developers can push documents, records, metadata, identities, permissions, and deletion events through a documented API.
- **AC:** Custom-connector data supports citations, freshness, owners, and permission trimming, and can be scoped to a pilot group.
- **AC:** Connector failures are visible to admins and developers.

**E1-6.** As a **DATA** analyst, I want structured records and unstructured documents represented together, so that I can ask business questions that combine narrative context and system data.
- **AC:** The system indexes structured fields, record metadata, document text, comments, attachments, and relationships.
- **AC:** Users can ask questions combining text with fields such as account stage, ticket priority, renewal date, or repository; answers cite structured and unstructured sources separately.
- **AC:** The system warns when a field is unavailable, restricted, or not yet indexed.

**E1-7.** As a **SEC** owner, I want data-minimization controls for indexing and retrieval, so that the product only uses data needed for approved use cases.
- **AC:** Admins can exclude sources, folders, channels, object types, fields, file classes, or sensitivity labels.
- **AC:** Admins can choose whether personal browsing history, private notes, DMs, or email are enabled.
- **AC:** Excluded data is not retrievable, cited, summarized, embedded into output, or used by agents; exclusion changes are logged.

**E1-8.** As an **ENG**, I want code repositories indexed with code-aware retrieval that respects repo and team access, so that I can find implementations by name or intent without seeing code I'm not entitled to.
- **AC:** Code is searchable with identifier-aware tokenization and semantic matching; repo/team ACLs are enforced at query time.
- **AC:** Results support filters such as file type, repository, and path.

---

# EPIC 2 — Unified Search And Trusted Answers

Help users find, verify, and act on company knowledge without switching between every source system.

**E2-1.** As a **KW**, I want one search box across all permitted company systems, so that I can find knowledge without guessing which app holds it.
- **AC:** A query returns ranked results across all permitted sources, showing source app, title, snippet, owner, last-modified, and why it matched when available.
- **AC:** Users can open the source or an inline preview; restricted results never appear as titles, snippets, counts, or inferred answer content.

**E2-2.** As a **KW**, I want a direct, cited answer above the results, so that I can orient quickly and inspect the evidence.
- **AC:** Grounded claims cite specific records or passages; sourced statements are distinguished from general reasoning.
- **AC:** The answer warns when evidence is thin or conflicting; users can expand citations and flag missing or incorrect evidence.

**E2-3.** As a **KW**, I want personalized, role-aware ranking, so that the most relevant results for me surface first on ambiguous queries.
- **AC:** Ranking can use role, team, project, recency, ownership, collaboration, and prior interactions, alongside relevance.
- **AC:** Two users with different access or context can get different, individually-relevant results for the same query.

**E2-4.** As a **KW**, I want advanced filters and operators for source, type, owner, team, customer, project, date, status, and sensitivity, so that I can narrow broad searches precisely.
- **AC:** Filters work through UI controls and natural language, with include and exclude options.
- **AC:** Result counts update without exposing restricted data; filters persist across follow-up questions.

**E2-5.** As a **NH**, I want to find the person who knows about a topic, customer, system, or process, so that I can ask the right human when docs aren't enough.
- **AC:** Expert suggestions are based on visible evidence (authorship, ownership, comments, ticket resolution, code contribution, meeting participation) and explain why each person was suggested.
- **AC:** Experts can be suggested without exposing restricted underlying content; results filter by team, location, role, or relationship.

**E2-6.** As an **EXEC**, I want an explorable view of org structure, reporting lines, and responsibilities, so that I can find the right stakeholders quickly.
- **AC:** Users can navigate teams, managers, and roles; data respects directory permissions and can be exported by authorized admins.

**E2-7.** As a **PM**, I want to search for prior decisions and their rationale, so that I don't reopen settled questions or miss constraints.
- **AC:** Decisions are retrieved from docs, notes, tickets, comments, and threads; answers separate decision, rationale, date, owners, and open follow-ups when evidence exists.
- **AC:** Conflicting or superseded decisions are highlighted; a validated decision can be promoted to a canonical knowledge item.

**E2-8.** As a **KW**, I want the product to surface conflicts between sources, so that I don't rely on outdated or contradictory information.
- **AC:** When sources disagree, the answer states the conflict with recency, owners, verification state, and citations instead of silently choosing one.
- **AC:** Users can route a conflict to a knowledge owner; resolutions can update canonical answers or metadata.

**E2-9.** As a **KW**, I want to create memorable shortcuts and curated collections of links/docs across apps, so that my team can reach key resources instantly.
- **AC:** Shortcuts resolve to a destination and can be shared org-wide within source permissions; collections aggregate cross-app items and appear in search.
- **AC:** Usage of shortcuts and collections is visible to owners.

**E2-10.** As a **KM**, I want feedback from failed searches and poor answers, so that I can improve knowledge quality and relevance.
- **AC:** Users can mark answers helpful, incorrect, stale, missing citations, or lacking context.
- **AC:** Owners can view top failed queries and low-satisfaction themes without exposing restricted query content to unauthorized reviewers.

---

# EPIC 3 — Assistant Workspace

Provide a company-grounded assistant that can answer, summarize, draft, analyze, and move work forward from one workspace and across the surfaces where work happens.

**E3-1.** As a **KW**, I want to ask natural-language questions about company knowledge, so that I get answers without manually collecting sources.
- **AC:** The assistant searches permitted context and returns a concise cited answer; follow-ups preserve conversation context.
- **AC:** Users can request broader, narrower, faster, or deeper responses; the assistant asks for clarification when it cannot ground the request.

**E3-2.** As a **KW**, I want to summarize documents, threads, tickets, meetings, repositories, or collections, so that I can understand long material quickly.
- **AC:** Summaries include key points, decisions, risks, owners, dates, and action items when present, and cite sources.
- **AC:** Users choose length, audience, tone, and format; the product warns when sources are too large, inaccessible, or partially processed.

**E3-3.** As a **MGR**, I want the assistant to reason across docs, tickets, meetings, and chat, so that I see the full picture behind a project or problem.
- **AC:** The answer shows which source groups were searched and separates facts, interpretation, assumptions, and next steps.
- **AC:** Missing stakeholders or source systems are called out when obvious; the intermediate source set is inspectable.

**E3-4.** As a **KW**, I want the assistant to draft emails, updates, memos, and replies using my role, audience, and source context, so that I don't start from a blank page.
- **AC:** Drafts adapt by audience, tone, length, format, and objective, and include citations or hidden source notes where appropriate.
- **AC:** The user can compare alternative versions; nothing is sent or published without explicit approval.

**E3-5.** As a **KW**, I want the assistant to adapt to my writing style and remember my role, team, and active projects, so that output fits me without re-explaining context each time.
- **AC:** The assistant can learn tone/style from my prior approved writing and apply it on request.
- **AC:** Remembered facts and preferences are viewable, editable, and deletable by the user; memory can be disabled for a specific chat.

**E3-6.** As a **KW**, I want to upload or reference files for one-off analysis, so that temporary working material is usable without becoming broadly indexed knowledge.
- **AC:** Users can attach supported files; the product explains whether the file is temporary, private, indexed, retained, or shareable.
- **AC:** Answers cite the uploaded file when used; admin policy can restrict types, size, retention, and sensitive content.

**E3-7.** As a **DATA** analyst, I want the assistant to analyze a spreadsheet or dataset conversationally (stats, distributions, trends), so that I can explore data without writing code.
- **AC:** The assistant computes results in an inspectable, reproducible way and separates computed results from narrative explanation.
- **AC:** Generated charts/tables cite the underlying data; sensitive fields are masked or excluded per policy.

**E3-8.** As a **KW**, I want the assistant to understand images I share and generate visuals when useful, so that I can work with and produce visual content in line.
- **AC:** The assistant can describe/answer questions about an uploaded image and generate images on request, with provenance noted.
- **AC:** Image generation respects content policy and admin enablement.

**E3-9.** As a **KW**, I want the assistant to translate content and work in my language, so that I can collaborate across a multilingual organization.
- **AC:** The assistant can translate selected text or answers into supported languages and preserve meaning and key terms.

**E3-10.** As a **KW**, I want a browsable library of saved and shareable prompts and templates, so that I can start from proven patterns instead of a blank box.
- **AC:** Prompts/templates are organized by team and task type, can be created/tested/saved/shared, and respect permissions.

**E3-11.** As a **KW**, I want the assistant available across web, browser, chat platforms, desktop, and embedded app contexts, so that I can use it where work already happens.
- **AC:** Core answer, citation, feedback, and approval behaviors are consistent across surfaces; surface context (current page/thread) is visible before use.
- **AC:** Admins can enable/disable each surface; deep links continue work in the full workspace.

**E3-12.** As a **KW**, I want to choose whether the assistant uses company sources, selected sources, uploaded files, the web, or general model knowledge, so that I can control grounding and privacy.
- **AC:** Active knowledge modes are visible and changeable; answers disclose which modes were used.
- **AC:** Admins can restrict external web use and general model knowledge; the assistant warns when a mode is unavailable.

**E3-13.** As a **KW**, I want to pick or auto-select the model behind a chat, so that I can match quality, speed, and cost to the task.
- **AC:** Users can select among admin-approved models per chat, or accept a smart default that routes by task.
- **AC:** Model choice is disclosed; admins govern which models are available and the default.

---

# EPIC 4 — Proactive Work Intelligence

Move from reactive chat to proactive help with commitments, briefs, blockers, project changes, and next steps — opt-in and transparent.

**E4-1.** As a **KW**, I want a daily brief of meetings, priorities, changes, open requests, and commitments, so that I start the day oriented.
- **AC:** The brief combines permitted calendar, task, email, chat, ticket, document, and project signals, grouped by urgency, owner, due date, project, and source.
- **AC:** Each item links to evidence; users can tune sources, timing, format, and delivery channel.

**E4-2.** As a **KW**, I want meeting prep generated before important meetings, so that I can walk in with context, agenda, risks, and likely questions.
- **AC:** Prep includes attendees, recent related work, decisions, open issues, account context, and suggested questions when available, distinguishing confirmed facts from inferred relevance.
- **AC:** Prep can be scheduled or opened from a calendar event; sensitive attendee/customer data is permission-trimmed per viewer.

**E4-3.** As a **MGR**, I want commitments and follow-ups detected from messages, meetings, emails, and tickets, so that owners don't lose track of promised work.
- **AC:** Detected commitments include owner, due date, source, confidence, and suggested next action.
- **AC:** Users can accept, edit, snooze, dismiss, or convert to a task; no work is assigned to another person without confirmation.

**E4-4.** As a **MGR**, I want proactive risk cards for stalled work, missing owners, missed dates, repeated escalations, and dependency changes, so that I can intervene earlier.
- **AC:** Cards cite the signals that caused them and identify project, severity, owner, affected milestone, and suggested next step.
- **AC:** Users can mark a card valid, false positive, handled, or monitor; detection scopes by team, project, customer, or priority.

**E4-5.** As a **KW**, I want to subscribe to changes in projects, customers, docs, tickets, competitors, or policies, so that I'm notified when something materially changes.
- **AC:** Watch rules can be created from results, entity pages, docs, projects, or chat; alerts explain what changed, why it matters, and where the evidence is.
- **AC:** Users choose frequency and channel; admins can restrict monitoring of sensitive sources.

**E4-6.** As a **KW**, I want to understand and tune why a proactive card was surfaced, so that I can trust and control the assistant.
- **AC:** Each card explains its trigger (source, relationship, deadline, assignment, watched entity).
- **AC:** Users can remove a signal from future personalization where policy allows and pause monitoring; admins can set org defaults and required monitoring for compliance workflows.

**E4-7.** As a **KW**, I want proactive recommendations prioritized and batched, so that automation reduces noise instead of creating it.
- **AC:** Related alerts are deduplicated; low-urgency alerts batch into digests; users set quiet hours, thresholds, and channels.
- **AC:** Feedback on noisy alerts affects future ranking.

---

# EPIC 5 — Work Execution And Actions

Help users move from insight to action across systems of record while preserving review, control, and auditability.

**E5-1.** As a **KW**, I want the assistant to recommend next actions after an answer or summary, so that I can keep momentum without deciding every step.
- **AC:** Suggested actions fit the current context (draft reply, create ticket, update record, schedule meeting, summarize for team, create knowledge article) and explain why they were suggested.
- **AC:** Suggestions respect available connectors and permissions; unavailable actions show a clear reason.

**E5-2.** As a **SUP** or **IT** user, I want to create a ticket from a conversation, meeting, or answer, so that work is tracked without copying details manually.
- **AC:** The draft ticket includes title, description, evidence links, priority, owner/team suggestion, and source context, with every field editable.
- **AC:** Write-back happens only after confirmation; the created ticket links back to source context and the audit log.

**E5-3.** As a **SALES** or **CS** user, I want the assistant to draft updates to records from calls, emails, and support context, so that systems of record stay current.
- **AC:** Draft updates map evidence to specific fields (next step, stage, risk, renewal date, competitor, forecast note, health) with a before/after preview.
- **AC:** Validation rules surface before submission; the update logs sources and the approving user.

**E5-4.** As a **KW**, I want to draft and route emails, chat messages, and stakeholder updates from source context, so that communication is accurate and fast.
- **AC:** Drafts include recipients, channel, tone, summary, ask, and references; sensitive recipients, external domains, or restricted content are flagged before sending.
- **AC:** The user can send manually or copy to the target system; admins can require approval for external or high-risk communications.

**E5-5.** As a **KW**, I want the assistant to act inside a working document, spreadsheet, or database when asked (build a table, write formulas, restructure content, update records), so that the deliverable itself is produced, not just described.
- **AC:** Edits are applied to the actual artifact/object with a preview and are reversible where the host system permits.
- **AC:** The action respects the user's permissions on the target and is logged.

**E5-6.** As a **SEC** owner, I want risky actions categorized by tier and gated by approval, so that agents cannot cause harm through unintended writes or sends.
- **AC:** Admins define which risk tiers require approval and who can approve; prompts show tool, target, arguments, source evidence, and expected effect.
- **AC:** Rejected actions are logged and do not execute.

**E5-7.** As an **OPS** owner, I want the product to confirm a write actually succeeded with a read-back, so that I'm not left with a false sense of completion.
- **AC:** After write-back, the product reads the created/updated record when supported and shows record ID, link, changed fields, and timestamp.
- **AC:** Partial failures are reported clearly; retry never repeats a consequential action without safeguards.

**E5-8.** As an **OPS** owner, I want one request to draft, review, update, notify, and schedule follow-up across systems, so that common workflows complete end to end.
- **AC:** The assistant shows a plan before multi-step execution; each step identifies tool, data used, output, and approval requirement.
- **AC:** Low-risk steps can be approved together and high-risk individually; the result includes a run summary and links to changed objects.

---

# EPIC 6 — Agent Builder, Library, And Reusable Skills

Let teams turn repeated knowledge-work patterns into governed, reusable agents — accessible to non-technical builders and advanced builders alike.

**E6-1.** As a **BLD**, I want to describe an agent in plain language (and refine it conversationally), so that I can automate recurring work without writing code.
- **AC:** The builder generates a draft name, purpose, inputs, sources, tools, steps, output format, and approval policy, and asks clarifying questions for missing scope, ownership, or risk.
- **AC:** The configuration is editable before testing; the builder warns when the agent touches unavailable systems or high-risk actions.

**E6-2.** As a **BLD**, I want to choose which sources and tools an agent can use, so that the agent stays focused and safe.
- **AC:** Builders select sources, collections, object types, entities, and tool permissions; the expected data and action scope is shown before publishing.
- **AC:** Admin policy can prevent certain combinations; agents cannot access data or tools outside their configured scope.

**E6-3.** As a **BLD**, I want branching, variables, conditional steps, and per-step model selection, so that agents handle real workflows and I can tune cost/quality per step.
- **AC:** Variables can come from user input, trigger payloads, retrieved data, and prior steps; branches can use fields, classification, approvals, confidence, or source availability.
- **AC:** The preview shows which branch ran and why; a model and creativity level can be set per agent and per step.

**E6-4.** As a **BLD**, I want to package reusable expertise as a shareable Skill (instructions + knowledge + tools) that any agent can load, so that I maintain a capability once instead of per-agent.
- **AC:** A Skill is reusable across agents, versioned, and permission-inheriting; updating it propagates to every agent that uses it.
- **AC:** Skills are discoverable and shareable org-wide within governance rules.

**E6-5.** As a **BLD**, I want to preview, test, and debug an agent before publishing, so that I can catch errors safely.
- **AC:** Builders run test cases with sample inputs and sandboxed/read-only tools; the debug view shows prompt, retrieval, sources, tool calls, outputs, approvals, errors, and timing.
- **AC:** Test runs perform no real write actions unless explicitly enabled in a safe environment; test cases can be saved as regression checks.

**E6-6.** As a **KW**, I want to browse a library of approved agents and templates, so that I can deploy proven automations without rebuilding them.
- **AC:** The library supports search, categories, owner, rating, certification state, department, required permissions, and supported surfaces.
- **AC:** Detail pages show purpose, example prompts, sources, tools, risk tier, last updated, and owner; admins can feature, certify, deprecate, or disable agents.

**E6-7.** As an **AGENTOPS** owner, I want agent version history and rollback, so that changes don't silently break production workflows.
- **AC:** Every published change creates a version with author, timestamp, diff, test status, and notes; runs record which version produced them.
- **AC:** Owners can roll back; breaking changes require republishing and notifying affected subscribers.

**E6-8.** As an **ADM**, I want every agent to have an accountable owner, so that agents don't become abandoned automation.
- **AC:** An agent cannot be published without an owner and backup owner; ownership transfer is logged.
- **AC:** Agents without active owners are flagged; admins can bulk reassign or disable orphaned agents.

---

# EPIC 7 — Autonomous, Scheduled, And Event-Driven Agents

Support agents that run without a user typing every prompt, while keeping autonomy controlled and observable.

**E7-1.** As an **OPS** owner, I want an agent to run on a schedule, so that recurring reports and updates happen automatically.
- **AC:** Owners configure cadence, timezone, input parameters, and delivery destination; runs show upcoming time, last run, status, and failure reason.
- **AC:** Outputs include citations, run logs, and action summaries; users can pause, resume, or run manually.

**E7-2.** As an **OPS** owner, I want agents to run when a business event occurs, so that work starts when the signal appears.
- **AC:** Triggers include new record, changed field, incoming message, file created, meeting ended, ticket escalated, or webhook received; the payload is visible in the run log.
- **AC:** Trigger rules filter by source, team, customer, severity, status, or content pattern; admins limit which events may trigger agents.

**E7-3.** As a **SUP** or **OPS** user, I want an agent to monitor shared inboxes or channels, so that requests are triaged and routed quickly.
- **AC:** The agent classifies new items by intent, urgency, owner, customer, and missing information, and can draft a reply, create a ticket, route to a queue, or ask a clarifying question.
- **AC:** Human review can be required before external replies or ticket creation; duplicate handling is avoided.

**E7-4.** As an **OPS** owner, I want low-risk agent outputs to write back automatically within policy, so that routine processes complete without manual clicks.
- **AC:** Admin policy defines which tools and fields are eligible for automatic write-back; runs record the policy that allowed automation.
- **AC:** Users can configure exceptions requiring approval; failed write-backs alert and never silently drop work.

**E7-5.** As an **OPS** owner, I want agents to escalate ambiguity, missing data, policy conflicts, and tool failures, so that humans handle cases the agent shouldn't decide.
- **AC:** Exception rules trigger on low confidence, source conflict, missing required fields, restricted data, failed tool call, or approval timeout, and include context, evidence, and attempted steps.
- **AC:** The receiving human can resume, cancel, or reroute the run; outcomes can improve future behavior when approved.

**E7-6.** As a **BLD**, I want one agent to call specialist agents, so that complex workflows divide into research, analysis, drafting, review, and action.
- **AC:** Parent runs show child agents, inputs, outputs, and ownership; child agents inherit or further restrict permissions per policy.
- **AC:** Child failures are visible and recoverable; the final output identifies which agent produced each section or action.

**E7-7.** As a **SEC** owner, I want each agent assigned an autonomy level, so that users understand whether it suggests, drafts, acts with approval, or acts automatically.
- **AC:** Autonomy levels are visible in the library and run details; higher autonomy requires stronger owner, test, approval, and monitoring requirements.
- **AC:** Admins can cap autonomy by department, source, action type, or user group; changes are reviewed and logged.

**E7-8.** As an **AGENTOPS** owner, I want agents to detect when their scope or context changes materially and pause for review, so that drifting or out-of-policy agents stop themselves.
- **AC:** A material change in permissions, data scope, or configuration can auto-pause an agent and notify the owner.
- **AC:** Resuming requires explicit owner action and is logged.

---

# EPIC 8 — Research, Analysis, And Evidence Work

Automate the research loop: gather evidence, analyze it, expose assumptions, and produce decision-ready outputs.

**E8-1.** As an **ANALYST** or **EXEC**, I want deep research across company sources and approved external sources, so that I get a decision-ready report from a complex question.
- **AC:** The agent shows a research plan; users can approve scope, sources, time budget, and output format.
- **AC:** The report includes summary, findings, evidence, citations, assumptions, gaps, and next steps, and states which sources were unavailable or excluded.

**E8-2.** As an **ANALYST**, I want to run the same questions across many documents or records, so that I can compare evidence systematically.
- **AC:** Rows can be documents, records, companies, customers, or deals and columns can be questions; each cell includes answer, citation, confidence, and extraction notes.
- **AC:** Users can filter, sort, export, and drill into source passages; the matrix flags missing, conflicting, or low-confidence cells.

**E8-3.** As a **FIN** or **DATA** user, I want to analyze metrics alongside documents and comments, so that numerical changes can be explained by business context.
- **AC:** Questions can combine tables, records, tickets, documents, and notes; outputs include charts, tables, written interpretation, and citations.
- **AC:** Computed results are separated from narrative and are inspectable and reproducible where possible.

**E8-4.** As a **LEGAL** or **FIN** analyst, I want to analyze a large corpus of contracts, PDFs, spreadsheets, and notes, so that diligence is faster and traceable.
- **AC:** Users define extraction fields (term, renewal, liability cap, change-of-control, risk, obligation, exception); results return a table with citations to exact source locations.
- **AC:** The analysis identifies missing documents, unusual clauses, outliers, and risks; exports retain citations.

**E8-5.** As a **MKT** or **PM** user, I want competitive research synthesized from public web, internal notes, calls, and battlecards, so that positioning stays current.
- **AC:** Reports separate external facts, internal observations, customer objections, and recommended messaging, and highlight what changed since the last report.
- **AC:** Users can generate persona- or account-specific versions; sources and freshness are visible.

**E8-6.** As an **EXEC**, I want research outputs to show assumptions, uncertainty, and conflicting evidence, so that I can decide with appropriate caution.
- **AC:** Reports include explicit assumptions and confidence indicators; conflicting evidence is grouped and cited.
- **AC:** The assistant suggests follow-up research to reduce uncertainty; users can mark assumptions accepted, rejected, or needs-validation.

**E8-7.** As an **ANALYST**, I want to turn a useful research process into a reusable template or agent, so that recurring analysis repeats consistently.
- **AC:** Users save sources, questions, output format, and review steps as a template that reruns with new entities, dates, customers, or files.
- **AC:** Version history preserves methodology changes; reused outputs cite the template and run inputs.

---

# EPIC 9 — Artifact And Content Creation

Turn context and reasoning into durable, editable work products with provenance.

**E9-1.** As an **EXEC** or **MGR**, I want to generate decision memos from research, meetings, metrics, and project context, so that decisions are framed clearly and backed by evidence.
- **AC:** Memos include context, options, recommendation, risks, tradeoffs, owners, decision needed, and citations, and flag unresolved questions and weak evidence.
- **AC:** Users choose audience and length; the memo can be exported or written to an approved document system.

**E9-2.** As a **PM**, I want to draft a PRD from customer feedback, support tickets, calls, roadmap notes, and engineering constraints, so that product work starts from synthesized evidence.
- **AC:** The draft includes problem, users, goals, non-goals, requirements, acceptance criteria, risks, metrics, dependencies, and open questions, with evidence cited.
- **AC:** The assistant identifies gaps needing more discovery; the PRD iterates through follow-ups and exports.

**E9-3.** As a **KW**, I want to generate presentation outlines, briefs, decks, and spreadsheet drafts from context, so that I can communicate complex work quickly.
- **AC:** Outputs include titles, key points, speaker notes, suggested visuals, fields/formulas, source mappings, and caveats.
- **AC:** Users choose audience, tone, and length; exports preserve citations or presenter/source notes where possible.

**E9-4.** As a **KW**, I want a co-authoring canvas where AI output becomes a durable, editable, exportable artifact, so that I can iterate on the deliverable instead of disposable chat text.
- **AC:** Users edit directly or conversationally; the canvas keeps version history, a sources panel, and export to common document/slide/sheet/message formats.
- **AC:** Regenerated artifacts preserve prior versions for comparison.

**E9-5.** As a **KM** or **SUP** user, I want to turn resolved tickets, expert answers, and repeated questions into draft knowledge articles, so that knowledge improves as work happens.
- **AC:** Draft articles include problem, answer, steps, examples, audience, owner, review cadence, and sources, with suggested tags and related articles.
- **AC:** Drafts require owner review before being marked verified; the assistant detects if a similar article exists.

**E9-6.** As a **MKT** or **EXEC** user, I want generated content to follow approved style, tone, terminology, and brand rules, so that outputs are consistent.
- **AC:** Admins/owners define style guides and approved terminology; the assistant can explain which rules it applied and offer compliant alternatives.
- **AC:** External-facing content flags unapproved claims or restricted language.

**E9-7.** As a **SEC** or **KM** owner, I want generated artifacts to preserve provenance, so that reviewers know what evidence shaped the output.
- **AC:** Artifacts retain source links, generation time, user, run ID, and model/agent where policy allows; source notes are inspectable separately from final content.
- **AC:** Exported artifacts include citations or a source appendix when supported.

**E9-8.** As a **KW**, I want to turn a useful assistant conversation or workflow into a reusable, shareable mini-app, so that a one-off prompt chain becomes a tool my team can run.
- **AC:** The app captures inputs, steps, and output format and can be shared to a library within governance rules.
- **AC:** App runs respect permissions, citations, and approval policy like any other agent.

---

# EPIC 10 — Meetings, Communication, And Follow-Up Intelligence

Turn conversations into searchable knowledge, action items, decisions, and follow-through.

**E10-1.** As a **KW**, I want meetings captured, summarized, and indexed with permission controls, so that meeting knowledge is not lost.
- **AC:** Meeting records include title, attendees, transcript or notes, summary, decisions, action items, and source links; access follows meeting and source permissions.
- **AC:** Users can exclude or redact sensitive meetings per policy; content is searchable as a knowledge asset after processing.

**E10-2.** As a **MGR**, I want action items extracted from meetings with owners and due dates, so that follow-up becomes trackable.
- **AC:** Action items include owner, due date, source timestamp, confidence, and suggested task destination; users confirm, edit, assign, dismiss, or create tasks.
- **AC:** No person is notified or assigned without confirmation; confirmed items can appear in briefs and reminders.

**E10-3.** As a **KW**, I want long chat and email threads summarized with decisions, open questions, and asks, so that I can catch up quickly.
- **AC:** Summaries distinguish facts, decisions, asks, blockers, sentiment, and unresolved items, and cite messages/segments where possible.
- **AC:** Users can choose "catch me up," "what do I owe," "what changed," or "draft a reply"; external recipients and sensitive content are flagged before drafting.

**E10-4.** As a **MGR**, I want stakeholder updates generated from project activity, so that I communicate progress without manual status chasing.
- **AC:** Updates include progress, blockers, risks, decisions needed, milestones, and asks, and cite docs, tickets, commits, meetings, and messages used.
- **AC:** Users choose executive, team, customer, or cross-functional format; updates post/send only after review unless policy allows auto-delivery.

**E10-5.** As a **PM** or **MGR**, I want important decisions captured into a decision log, so that teams can find what was decided and why.
- **AC:** The system suggests candidate decisions from meetings, docs, tickets, and chat; users confirm decision, date, owner, rationale, scope, and evidence.
- **AC:** Entries support superseded/replaced status; search and answers prefer current decisions while showing history.

**E10-6.** As an **OPS** owner, I want recurring communication agents for weekly reports, release notes, pipeline summaries, and leadership updates, so that repeated communication is automated.
- **AC:** Owners configure cadence, audience, sources, format, approval policy, and channel; drafts include citations and change summaries.
- **AC:** The owner can approve, edit, skip, or send; missed/failed runs alert the owner.

**E10-7.** As a **KW**, I want requests made to me across chat, email, tickets, and meetings consolidated, so that I know what I owe others.
- **AC:** Requests include source, requester, due date, project, status, and confidence; users can convert to tasks or mark not actionable.
- **AC:** Duplicate requests are merged; private requests are not exposed to other users.

---

# EPIC 11 — Knowledge Governance, Trust, And Source Quality

Make the knowledge base trustworthy enough for humans and agents to rely on.

**E11-1.** As a **KM**, I want important knowledge verified by accountable owners, so that users and agents know what is trustworthy.
- **AC:** Items can carry owner, verifier, verification date, expiration, audience, and status; search and answers expose verification state.
- **AC:** Expired verification triggers review reminders; only authorized reviewers can mark content verified.

**E11-2.** As a **KM**, I want stale content detected automatically, so that outdated answers don't keep circulating.
- **AC:** Staleness signals include age, superseding docs, low feedback, broken links, changed owners, and conflicting newer sources; stale content is flagged in search and answers.
- **AC:** Owners receive review tasks with evidence; users can report stale content from any surface.

**E11-3.** As a **KM**, I want duplicate and conflicting knowledge detected, so that teams consolidate around one source of truth.
- **AC:** The system groups likely duplicates with rationale and identifies contradictory claims, policies, values, dates, or instructions.
- **AC:** Owners can merge, deprecate, redirect, or keep separate with rationale; agents prefer canonical sources.

**E11-4.** As a **KM**, I want search failures and repeated unanswered questions surfaced as knowledge gaps, so that we know what content to create.
- **AC:** Gaps derive from failed searches, low-rated answers, repeated expert routing, and unresolved support patterns, grouped without leaking restricted query content.
- **AC:** Owners can create draft articles from a gap cluster; closing a gap can tie to improved search satisfaction.

**E11-5.** As a **KW**, I want unresolved questions routed to the right expert, so that AI helps me find the human who can answer.
- **AC:** Routing shows suggested experts, rationale, and availability signals where available, and can send a prefilled context packet.
- **AC:** Experts can answer once and promote the answer to reusable knowledge, respecting source permissions and audience rules.

**E11-6.** As a **KM**, I want frequently used answers promoted into canonical answer cards, so that teams get consistent responses.
- **AC:** The product suggests promotion candidates from repeated questions and high-confidence answers; canonical answers include owner, audience, citations, review cadence, and related sources.
- **AC:** Canonical answers can appear in search, chat, messaging surfaces, and agent workflows; superseded answers redirect to the current one.

**E11-7.** As a **KW**, I want relevant verified knowledge surfaced proactively based on what I'm working on, so that I get the right answer without searching.
- **AC:** Context rules can surface a verified item based on the current task/record/page (e.g., a deal stage or a specific topic), within permissions.
- **AC:** Proactive surfacing is explainable, dismissible, and tunable by the user and admin.

**E11-8.** As a **KM**, I want agents that help maintain knowledge — drafting updates from conversations, merging duplicates, flagging stale or conflicting content, and proposing re-verification — so that quality scales without manual upkeep.
- **AC:** Agent-proposed changes require owner approval before publish/verify; every change is logged with evidence.
- **AC:** Agents can auto-unverify content that usage signals show is out of date, with owner notification.

**E11-9.** As a **KM** or **EXEC**, I want trust dashboards for key knowledge domains, so that I can see where automation is safe versus risky.
- **AC:** Dashboards show verified coverage, stale content, unresolved conflicts, gap clusters, answer satisfaction, and owner responsiveness, filterable by department, topic, source, audience, and time.
- **AC:** Low-trust domains can be excluded from high-autonomy agents; trends can be exported for governance review.

---

# EPIC 12 — Departmental Automation

Apply the core platform to high-value workflows for specific teams.

**E12-1.** As a **SALES** user, I want an account brief combining CRM, emails, meetings, support tickets, product usage, contracts, and chat context, so that I enter customer conversations prepared.
- **AC:** Briefs include account summary, stakeholders, open opportunities, recent activity, risks, support issues, competitors, next steps, and suggested questions, each cited.
- **AC:** Users can generate pre-call, post-call, renewal, expansion, and executive-review variants; restricted customer data is permission-trimmed.

**E12-2.** As a **SALES**, **LEGAL**, or **SEC** user, I want RFP and questionnaire answers drafted from approved past responses and policy docs, so that repetitive responses are fast and consistent.
- **AC:** The assistant matches questions to approved answers, policies, certifications, and prior proposals, with confidence, citations, and "needs human review" flags.
- **AC:** Unapproved or stale answers cannot be marked final without review; exports preserve question-answer mapping and sources.

**E12-3.** As a **SUP** user, I want ticket summaries, suggested resolutions, and draft replies grounded in policies and prior cases, so that I resolve issues faster.
- **AC:** The assistant summarizes the issue, timeline, sentiment, related tickets, product area, and likely root cause, and cites knowledge articles and prior tickets.
- **AC:** Draft replies adapt by tone and customer tier; the product can create follow-up tasks or update ticket fields after approval.

**E12-4.** As an employee (**KW**), I want IT and HR questions answered and routine workflows initiated from one assistant, so that I don't need to find the right portal or queue.
- **AC:** The assistant answers policy questions and initiates approved workflows (access request, equipment issue, PTO guidance, benefits, payroll case); sensitive HR answers respect employee/manager/HR permissions.
- **AC:** Workflows show required information, approvals, and expected path; escalations include context so the service team doesn't restart discovery.

**E12-5.** As an **IT** or **HR** service owner, I want autonomous self-service that resolves a large share of routine requests end to end with measurable deflection, so that agents handle routine work before a human sees it.
- **AC:** The agent can troubleshoot, answer, file, or route across domains, auto-resolving where confident and escalating with full context where not.
- **AC:** Deflection and resolution rates are reported; high-risk actions remain approval-gated.

**E12-6.** As an **ENG** user, I want an incident assistant that gathers alerts, logs, tickets, code changes, runbooks, and chat context, so that incident response starts with relevant context.
- **AC:** The assistant produces current status, suspected causes, affected systems, recent changes, owners, and runbook links, and can draft incident updates and postmortem sections.
- **AC:** It does not execute production operations without explicit approval and policy support; recommendations cite evidence and separate hypotheses from confirmed facts.

**E12-7.** As an **ENG** user, I want coding agents that turn a spec or ticket into a reviewed change and review others' changes, so that routine implementation and review are accelerated.
- **AC:** The agent can draft a change with a description grounded in the codebase and propose review feedback against standards.
- **AC:** Changes remain proposals requiring human review and approval before merge; actions are logged and permission-scoped.

**E12-8.** As a **PM**, I want feature requests synthesized across tickets, calls, CRM, surveys, chat, and roadmap docs, so that prioritization uses real evidence.
- **AC:** The system clusters requests by theme, persona, segment, revenue impact, frequency, and urgency, with representative quotes and source links.
- **AC:** It identifies duplicates, contradictions, and existing roadmap coverage; clusters export into PRDs, epics, or planning docs.

**E12-9.** As a **MKT** user, I want campaign briefs generated from customer proof, sales objections, competitive research, product updates, and past campaigns, so that messaging is grounded and current.
- **AC:** Briefs include audience, message, proof points, channels, risks, assets needed, and source evidence, and flag unapproved claims or outdated positioning.
- **AC:** Users can generate channel-specific drafts; brand and style rules apply when configured.

**E12-10.** As a **LEGAL** user, I want contracts reviewed against playbooks and precedent, so that common issues are identified quickly.
- **AC:** The assistant extracts key terms, deviations, risky clauses, missing clauses, and fallback language, each citing contract text and playbook/precedent.
- **AC:** Redlines or comments are drafts until legal approval; restricted matters are accessible only to authorized users.

**E12-11.** As a **FIN** user, I want close and forecast narratives drafted from financial data, pipeline movement, account health, invoices, and commentary, so that reporting explains what happened and why.
- **AC:** Outputs separate metrics, variance explanations, risks, assumptions, and follow-ups; calculations and source records are inspectable.
- **AC:** The assistant flags missing approvals, anomalous changes, and unsupported explanations; narratives export to reporting packs.

**E12-12.** As an **EXEC**, I want an operating-review brief across priorities, metrics, risks, customers, hiring, finance, and open decisions, so that leadership meetings focus on decisions, not status collection.
- **AC:** The brief includes top changes since last review, risks, decisions needed, owners, and evidence, with drill-down on any metric or claim.
- **AC:** Sensitive sections are permission-scoped per audience; resulting decisions and follow-ups are tracked.

**E12-13.** As an **OPS** owner of a global product, I want a localization agent that translates and adapts content at scale, so that go-to-market in every market is faster.
- **AC:** The agent translates and adapts tone/terminology per locale, preserving meaning and brand, and flags content needing human linguistic review.
- **AC:** Outputs are traceable to source content and approved glossaries.

---

# EPIC 13 — Security, Governance, Compliance, And Policy

Make AI use safe, inspectable, policy-driven, and acceptable to enterprise security teams.

**E13-1.** As an **ADM**, I want to control which users can access which sources and AI capabilities, so that rollout happens safely by group and use case.
- **AC:** Admins enable sources, search, assistant, actions, agents, web access, file upload, and surfaces by group; changes show affected users and agents before saving and are logged.
- **AC:** Users see a clear explanation when a capability is disabled by policy.

**E13-2.** As an **ADM**, I want platform roles for users, builders, agent owners, knowledge owners, admins, and auditors, so that duties are separated.
- **AC:** Roles define who can connect sources, build/publish agents, approve actions, view logs, manage policies, and verify knowledge, integrating with IdP groups where possible.
- **AC:** Privileged actions require the appropriate role and are logged; least privilege is the default for new users.

**E13-3.** As a **SEC** owner, I want sensitive data detected in sources, prompts, outputs, and actions, so that AI does not expose secrets, PII, PHI, payment data, or confidential content.
- **AC:** Policies detect and classify configured data types and can block, redact, warn, require approval, or log only, with source, type, severity, and remediation.
- **AC:** False positives can be reviewed without weakening global policy.

**E13-4.** As a **SEC** owner, I want the system to resist malicious or untrusted instructions in retrieved content, so that agents don't follow hidden commands from documents or webpages.
- **AC:** Retrieved content is treated as data, not trusted instruction, unless explicitly configured; suspicious instructions, jailbreaks, or tool-manipulation patterns trigger warnings or blocks.
- **AC:** Tool calls are validated against user intent and agent scope; security events appear in audit and monitoring.

**E13-5.** As an **ADM** or **SEC** owner, I want to control which models are used for which tasks, so that quality, cost, compliance, and data handling stay governed.
- **AC:** Admins define approved, default, blocked, and task-routed models, with retention, training-use, region, and provider notes where available.
- **AC:** Users see when a model is unavailable due to policy; model changes are logged and reviewable.

**E13-6.** As a **SEC** or compliance reviewer, I want searchable audit logs, so that I can investigate AI access, outputs, and actions.
- **AC:** Logs include user, timestamp, source, run ID, agent ID, citations, tools, approvals, action target, and result where policy allows, with safe handling/redaction of sensitive data.
- **AC:** Logs can be filtered, exported, and streamed to security tooling; retention is configurable.

**E13-7.** As an **ADM**, I want to simulate policy changes before enforcing them, so that I understand impact on users and agents.
- **AC:** Admins preview affected sources, agents, users, and workflows, and see which agents would fail under the new policy.
- **AC:** Simulation results can be shared and linked to the enforced change.

**E13-8.** As a **SEC** owner, I want deployment and data-handling options aligned with enterprise requirements (residency, isolation, encryption, key management, and where supported, private/sovereign deployment), so that regulated data is controlled.
- **AC:** The product documents where indexed data, logs, prompts, and outputs are stored and processed, and can enforce allowed regions where supported.
- **AC:** Isolation, encryption, and key-management posture are visible; unsupported residency requirements surface as blockers before deployment.

**E13-9.** As a **SEC** owner, I want guarantees that customer data is not used to train third-party models, so that proprietary content stays controlled.
- **AC:** Model usage operates under no-training and configurable-retention terms; the data-handling posture is documented and auditable.

---

# EPIC 14 — Admin, Analytics, AgentOps, And Adoption

Help organizations operate the product and agent fleet responsibly at scale.

**E14-1.** As an **AGENTOPS** owner, I want a complete inventory of agents, so that I know what automation exists and who owns it.
- **AC:** Inventory includes name, owner, status, version, sources, tools, autonomy level, schedules, triggers, usage, identity, and last run, filterable by department, risk, source, tool, owner, and certification.
- **AC:** Orphaned, inactive, failing, or high-risk agents are highlighted; admins can disable agents from the inventory.

**E14-2.** As an **AGENTOPS** owner, I want step-by-step traces for agent runs, so that failures and bad outputs can be diagnosed.
- **AC:** Traces show inputs, retrieval, reasoning summary, tool calls, approvals, outputs, errors, and timing, with sensitive content redacted per viewer.
- **AC:** Users can compare a failed run to a successful one; trace IDs appear in user-facing summaries and admin logs.

**E14-3.** As an **AGENTOPS** owner, I want evaluation sets and quality checks for agents, so that agents are tested before and after release.
- **AC:** Owners define test cases with expected sources, expected behavior, forbidden behavior, and output checks; evaluations run before publish and on schedule for critical agents.
- **AC:** Results show pass/fail, regressions, and examples; agents failing critical evaluations can be blocked from publishing.

**E14-4.** As a **SEC** owner, I want runtime monitoring of agent behavior, so that hallucinations, policy violations, drift, or unsafe outputs are caught live rather than in postmortems.
- **AC:** Monitoring can flag low-confidence outputs, restricted-data exposure, anomalous tool use, and scope drift in near real time, with alerts to owners/security.
- **AC:** Detections link to the run trace and can trigger auto-pause per policy.

**E14-5.** As an **EXEC** or **ADM**, I want adoption and outcome analytics, so that I can understand whether the product saves time and improves quality.
- **AC:** Analytics include active users, searches, answers, artifacts, agent runs, approvals, actions, feedback, deflected tickets, and time-saved estimates, filterable by team, department, source, and capability.
- **AC:** ROI metrics disclose assumptions and avoid exposing private query or source content to unauthorized viewers.

**E14-6.** As an **AGENTOPS** owner, I want an incident workflow for bad agent behavior, so that issues are triaged and resolved like production problems.
- **AC:** Users can report a run as wrong, unsafe, stale, unauthorized, noisy, or broken; reports create an incident with trace, reporter, severity, owner, and affected outputs/actions.
- **AC:** Owners can disable, roll back, patch, or mark no-action with rationale; outcomes feed future evaluations and policies.

**E14-7.** As an **ADM**, I want cost and usage controls, so that AI spend is predictable and aligned with value.
- **AC:** Admins view usage by model, source, user group, department, agent, and capability, and can set budgets or rate limits for high-cost features.
- **AC:** Users get graceful degradation or queueing at limits; cost reports export.

**E14-8.** As an **ADM**, I want rollout tools for pilots, training, and adoption, so that teams learn the product safely.
- **AC:** Admins define pilot groups, feature flags, default agents, and onboarding prompts; the product surfaces recommended next features based on usage.
- **AC:** Users access help, examples, and approved templates; admins compare pilot and non-pilot adoption while preserving privacy.

---

# EPIC 15 — Developer Platform, Interoperability, And Extensibility

Let technical teams extend the product into proprietary systems and external AI surfaces.

**E15-1.** As a **DEV**, I want APIs for search, retrieval, citations, and context packages, so that internal tools can use governed company knowledge.
- **AC:** APIs enforce user and app permissions and return source metadata, freshness, citations, and confidence where available.
- **AC:** API usage is logged and rate-limited; developers can test in a sandbox.

**E15-2.** As a **DEV**, I want a connector SDK or API, so that custom internal systems can be indexed without one-off ingestion scripts.
- **AC:** SDKs support content, metadata, ACLs, identity mapping, deletes, updates, and health checks; errors include actionable diagnostics.
- **AC:** Connector data can be isolated by environment, tenant, or pilot scope.

**E15-3.** As a **DEV**, I want to register custom tools/actions, so that agents can safely read from and write to internal systems.
- **AC:** Actions define schema, permissions, risk tier, preview behavior, approval requirement, and read-back verification; arguments are validated before execution.
- **AC:** Actions can be tested in dry-run; tool usage appears in agent traces and audit logs.

**E15-4.** As a **DEV**, I want external AI tools to access governed context through a standard interface, so that developers can use company knowledge in IDEs and agent hosts without bypassing security.
- **AC:** External clients authenticate and receive only permitted context; admins can allow, block, or scope external clients.
- **AC:** Standard tool calls are logged with user, client, source, and result; sensitive sources can be excluded from external access.

**E15-5.** As a **DEV**, I want a code-first agent toolkit where I can define a tool once and reuse it across agent frameworks, so that engineers can build agents in code and reuse capabilities.
- **AC:** A tool defined once is usable by the product's agents and by supported external frameworks, with consistent permissions and logging.
- **AC:** The toolkit includes ready-made tools for search, people, code, and calendar context.

**E15-6.** As a **DEV**, I want events for source sync, agent runs, approvals, policy violations, and knowledge changes, so that the product integrates with existing operations tools.
- **AC:** Webhooks include event type, timestamp, object IDs, tenant/workspace, and safe metadata, with configurable destinations, secrets, retry, and filters.
- **AC:** Failed deliveries are visible and retryable; events exclude restricted content unless explicitly authorized.

**E15-7.** As a **DEV** or **DATA** user, I want agents to run code or transformations in a sandbox, so that they can analyze data without risking local or production systems.
- **AC:** Sandboxes isolate files, network, secrets, and execution time; admins restrict packages, outbound network, runtime, and data egress.
- **AC:** Generated code and outputs are inspectable; sandbox runs link to the parent chat or agent trace.

**E15-8.** As a **DEV**, I want local development and test tools for connectors, actions, and agents, so that I can build and test extensions before deploying.
- **AC:** Developers can run mocked sources, sample ACLs, and test users; the toolchain validates schemas, permissions, action previews, and error handling.
- **AC:** Developer docs include examples for common integration patterns.

**E15-9.** As a **DEV**, I want to embed the copilot in internal applications, so that employees get contextual help inside the tools they use.
- **AC:** Embedded surfaces receive page, record, or workflow context from the host app, visible to the user before use.
- **AC:** Embedded answers and actions respect host-app and platform permissions; admins manage allowed host apps and embed scopes.

---

# EPIC 16 — Computer Use And Browser/Desktop Automation

Let agents operate software through its interface when no clean API exists. **Higher-risk by design** — these capabilities should ship behind strong approval, sandboxing, and audit controls (see research doc, "later-stage candidates").

**E16-1.** As an **OPS** owner, I want an agent to operate a website or app through its UI (navigate, click, fill forms, extract data) when no API exists, so that I can automate the long tail of systems integrations don't cover.
- **AC:** The agent runs in a secure, sandboxed/observed browser session; it shows its plan and asks for confirmation on consequential steps.
- **AC:** Every navigation and action is logged; credentials and sensitive fields are handled within policy and never exposed in traces.

**E16-2.** As a **KW**, I want an agent embedded in the browser that understands the current page and can run multi-step tasks across sites, so that the browser becomes an automation surface.
- **AC:** The agent uses only the page context the user shares; it requests approval before submitting, sending, purchasing, or sharing.
- **AC:** Admins can restrict which sites and actions are permitted.

**E16-3.** As a **KW** (non-engineer), I want a desktop/general-computing agent that automates file and app tasks on my machine within guardrails, so that I can offload computer chores without code.
- **AC:** The agent operates within explicit, user-granted scope (folders, apps, time) and pauses for approval on destructive or external actions.
- **AC:** Actions are reversible where possible and fully logged; the user can stop the agent at any time.

**E16-4.** As an **OPS** owner, I want UI-automation agents to self-correct on failure (retry, recover, find another path) and escalate when stuck, so that automations don't silently fail halfway.
- **AC:** On a failed step (page won't load, element missing, error returned), the agent retries within limits, then escalates with context.
- **AC:** Repeated failures pause the run and notify the owner rather than looping indefinitely.

---

# Backlog Conversion Notes

When product scope (OD-1) is confirmed, convert these into tracked issues in smaller slices via `scripts/stories-to-issues.ps1 -StoriesFile docs/product/user-stories.md` (dry-run first). Suggested first slices (high value, clear safety boundary):

- Context foundation and permission-aware search (Epic 1, E2-1/E2-2).
- Cited answer experience and feedback loop (E2-2, E2-10).
- Daily brief and meeting prep (E4-1, E4-2).
- Draft artifact generation with citations (E3-4, E9-1/E9-3).
- Human-approved ticket or record write-back (E5-2, E5-3, E5-6).
- Read-only research agents (E8-1) and agent-builder MVP (E6-1, E6-5).
- Knowledge verification and stale-content workflow (E11-1, E11-2).
- Admin policy, audit logs, and agent inventory (E13-1, E13-6, E14-1).

Keep later-stage until security invariants (OD-4) and verification gates (OD-5) exist: unattended write-back (E7-4), external sends, **computer/browser/desktop use (Epic 16)**, financial/legal/HR/security recommendations without guardrails, agent-to-agent delegation across vendors, and automated code changes or production operations (E12-6/E12-7 stay approval-gated).
