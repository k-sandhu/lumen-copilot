# Knowledge Work Automation — Research

Status: candidate product-scope input.
Last updated: 2026-06-16.
Tracking issue: https://github.com/k-sandhu/beacon/issues/1.

Comprehensive research synthesis on Glean and the broader enterprise-AI / knowledge-work-automation market, with emphasis on automation: finding knowledge, reasoning over it, creating deliverables, and safely executing work across business systems. Vendor and product names are retained here intentionally (the companion [user-stories.md](user-stories.md) is vendor-neutral).

This is structured input for **OD-1 ("Product scope and mission adjectives")** in [../specs/0001-open-decisions.md](../specs/0001-open-decisions.md). It is **not** a final product decision and does not close OD-1.

**Source material consolidated here:** the root research files `glean-user-stories.md` (deep Glean teardown, owned by the parallel story effort) and `knowledge-work-automation-user-stories.md` (22-vendor competitive sweep), plus the public product material cited at the end.

---

## 1. Executive Summary

The market has moved beyond "enterprise search" and "chat with company docs." Leading products now converge on one larger pattern:

1. Build a **governed enterprise context layer** from documents, messages, tickets, meetings, code, CRM records, HR/finance systems, and business activity.
2. Use that context to power **search, cited answers, deep research, artifact generation, and personalized assistance**.
3. Let users and teams **build agents that take action** across systems.
4. Run those agents **on demand, on schedules, or in response to events**.
5. **Govern the resulting agent fleet** with identity, permissions, approval gates, audit logs, evaluations, and observability.

Glean is one of the clearest examples of this shift, positioning as a "Work AI platform": search, assistant, agents, enterprise graph, personal graph, connectors, actions, model hub, MCP, governance, and security. Competitors approach the same space from different anchors — Microsoft from productivity and the Graph, Google from Gemini and cloud agents, Atlassian from Jira/Confluence teamwork, Moveworks from employee service, Guru from trusted knowledge, Notion from workspace-native agents, Workday Sana from HR/finance workflow, Salesforce from CRM agents, Writer from full-stack enterprise AI, Hebbia and AlphaSense from high-stakes document analysis, and the frontier labs (OpenAI, Anthropic, Perplexity) from a horizontal assistant that increasingly *acts*.

**Four shifts define 2024→2026, and all point toward automation:**

- **Answer → act.** Every serious player shipped an agent that takes multi-step action, not just chat.
- **Prompted → autonomous.** Agents now run on schedules and event triggers, and write results back into systems.
- **Single agent → governed fleet.** Control planes, per-agent identity, runtime guardrails, evaluations, and observability became table stakes.
- **Standalone → platform consolidation.** ServiceNow acquired Moveworks (~$2.85B), Workday acquired Sana (~$1.1B), and Automation Anywhere acquired Aisera — agentic AI is being folded into larger platform plays.

**Implication for Beacon.** The opportunity is not "another chatbot." It is a **governed knowledge-work automation layer** that helps a worker find the right context, understand and verify it, create a useful artifact, route or execute the next action, then remember, monitor, and follow up — all through auditable, permission-aware automation.

---

## 2. The Seven-Layer Pattern (what the leading products do)

The category can be read as a seven-layer stack. Each layer carries a product implication for Beacon.

### 2.1 Enterprise context layer
Connect enterprise systems and build a structured understanding of people, teams, projects, content, activity, and permissions (Glean calls this the Enterprise Graph + Personal Graph + "system of context"; Atlassian the Teamwork Graph; Microsoft the Microsoft Graph; Writer a graph-based knowledge graph).
- Connectors across productivity suites, file stores, chat, ticketing, CRM, support, code, HR, and data systems.
- Indexing for structured and unstructured data; identity and **permission mirroring** from source systems.
- Activity/relationship signals (authorship, collaboration, recency, ownership); entity understanding (projects, customers, products, people, repos, tickets, acronyms); per-user personalization.

*Implication:* the foundation is a **context layer, not a chat UI**. Every answer, artifact, and automation depends on trustworthy context, permission boundaries, and provenance.

### 2.2 Enterprise search
Not just keyword search: enterprise connectors + semantic/hybrid retrieval + ranking + personalization + facets + people/expert search + verified knowledge + cited AI answers + search analytics and knowledge-gap reporting.

*Implication:* treat search as a **workflow entry point**, not a separate feature. Users start with "where is X," but the real job is "use X to finish Y."

### 2.3 Assistant / coworker
A company-grounded assistant for answering, summarizing, drafting, analyzing, and moving work forward — increasingly **proactive** ("surface work before the user asks"). Multi-surface: web, browser extension, chat platforms, desktop. Capabilities: cited answers, summaries, drafting, research, data analysis, personalized writing, memory, model choice, deep-research modes, multimodal, voice.

*Implication:* the assistant should **monitor commitments, prepare users for upcoming work, and turn context into a first draft** — not only react.

### 2.4 Agents and work execution
The shift from "ask AI" to "delegate work": no-code agent creation, actions, orchestration, an agent library, governance, and execution through tools. Actions update systems of record (tickets, CRM, docs, projects). Agents run scheduled or triggered; sensitive actions require human approval; everything is debuggable, versioned, analyzed, governed.

*Implication:* distinguish **assistive prompts, deterministic workflows, and autonomous agents**. Users need a clear control model for when the system suggests, drafts, acts-with-approval, or acts automatically.

### 2.5 Knowledge governance
Curation primitives: verified answers, collections, pins, shortcuts, deprecation, knowledge management, search analytics. Guru pushes furthest with **verification, a measurable Trust Score, Knowledge Triggers (proactive surfacing by on-screen context), and knowledge agents that *improve* content quality**.

*Implication:* make knowledge trust **explicit**. If an automation depends on stale, unverified, or conflicting information, the user should see that risk before acting.

### 2.6 Security, governance, and compliance
Permission-aware AI, isolated/single-tenant deployment posture, zero-retention model agreements, sensitive-data protection, guardrails, audit logs, and AI-security controls. The frontier is **fleet governance**: a control plane, per-agent identity, runtime monitoring of hallucination/bias/drift/leakage, approval policies, and observability.

*Implication:* security must be a **product surface**, not only an implementation detail. Admins/security/compliance need direct visibility into what agents know, what they can do, what they did, and why.

### 2.7 Developer platform and interoperability
APIs, SDKs, custom indexing, custom tools/actions, and MCP-style interoperability so governed context reaches external AI surfaces (IDEs, agent hosts). Open protocols are emerging (Anthropic's MCP donated to the Linux Foundation; Google's Agent2Agent; agent toolkits that define a tool once and reuse it across frameworks).

*Implication:* the context and automation layer should be **extensible** — enterprises have custom systems, workflows, and preferred AI surfaces.

---

## 3. Competitive Landscape

The products below cluster around the same problem — automate knowledge work using governed company context — from different anchors.

| Company / product | Primary anchor | KW-automation signature | Most unique differentiator |
|---|---|---|---|
| **Glean** | Work AI platform | Search + Assistant + Agents; Agent Library | Enterprise Graph + horizontal, permissions-aware everything |
| **Microsoft 365 Copilot / Copilot Studio** | Productivity suite + Microsoft Graph | Agent Mode in Word/Excel/PowerPoint; autonomous agents; Researcher/Analyst | Graph grounding; **Agent 365** fleet governance + **Entra Agent ID**; 1,400+ connectors |
| **Google Gemini Enterprise** (ex-Agentspace) | Gemini + enterprise search + agents | Workspace Flows; Deep Research & Idea agents; no-code Agent Designer | **A2A protocol**; NotebookLM Enterprise; enterprise knowledge graph |
| **Amazon Q Business / Quick Suite** | AWS Work AI | **Q Apps** (chat→app); Quick Automate; Quick Research; agentic RAG | **Q index** shared to verified ISVs; **Bedrock AgentCore** (browser, gateway, memory) |
| **Dropbox Dash** | Universal-search Work AI | Search + content governance; MCP context layer | Document-level access governance depth; multimodal (image/video/people) search |
| **Atlassian Rovo** | Jira/Confluence Teamwork Graph | **Rovo Studio**, **Max Mode**, **Rovo Dev** (coding) | Teamwork Graph (150B+ connections); bundled into Atlassian plans |
| **Dust** | "Multiplayer OS for enterprise AI" | No-code agents, **Skills**, **Triggers** (cron/webhook + write-back) | Multiplayer shared agents; non-engineers as primary builders |
| **Moveworks** (→ ServiceNow) | Employee service automation | **Agentic Automation** (iPaaS replacement); 1,000+ agents | Slack/Teams self-service depth (millions of employees); **acquired by ServiceNow** |
| **Aisera** (→ Automation Anywhere) | Agentic AI service experience | **Universal Bot**; 64–84% ticket auto-resolution | **LLM Gateway** (model choice + token-cost cuts); AIOps |
| **ServiceNow** (Now Assist + AI Agents) | Workflow platform / system of action | **AI Agent Studio / Orchestrator**; autonomous change agents | **AI Control Tower** (governs 3rd-party agents too); RaptorDB; AI Agent Fabric |
| **Salesforce Agentforce** | CRM / "digital labor" | **Atlas Reasoning** + **Agent Script**; Einstein SDR/Service agents | Slack as "agentic OS"; **Agent Script** determinism; consumption pricing |
| **Writer** | Full-stack enterprise AI | **Action Agent**; **Playbooks/Routines**; event triggers | Self-built **Palmyra** models (Med/Fin) + **graph-based RAG** (#1 RobustQA) |
| **Workday Sana** | Agentic AI across HR/finance/work | Find · Act · Build · Automate; meetings-as-knowledge | Meeting + learning heritage; orchestrates across Workday + 3rd-party agents |
| **Cohere North** | Secure agentic workspace | No-code agents + workflow automations | **Sovereign/air-gapped** deploy (runs on ~2 GPUs); own models |
| **Guru** | Governed knowledge layer | Knowledge Agents; auto-verify; deep research | **Verification + Trust Score + Knowledge Triggers**; agents that *improve* content |
| **Notion AI** | Workspace-native docs/DBs | **Agents that build/edit pages & DBs**; Custom Agents | Agents act *inside the system of record*; no-bot AI meeting notes |
| **OpenAI ChatGPT Enterprise** | Frontier assistant | **ChatGPT agent** (browser+terminal), Deep Research, **AgentKit** | All-in-one research+act agent; Custom GPTs ecosystem; Connector Registry |
| **Anthropic Claude Enterprise** | Frontier assistant | **Claude Code**, **Cowork**, **Agent Skills** | **MCP + Agent Skills** open standards; 500K context; Compliance API |
| **Perplexity Enterprise** | Answer engine | **Labs** (build apps/dashboards); **Comet** browser | Agentic browser; always-cited internal + web |
| **Hebbia** | High-stakes document analysis | **Matrix** grid over thousands of docs (**ISD**) | Exhaustive document reasoning + sentence-level citations |
| **AlphaSense** | Market intelligence | **Deep Research**, **Generative Grid** | Premium licensed corpus + expert-call transcripts |
| **IBM watsonx Orchestrate** | Agentic process automation | **Agent Builder + Catalog** (150+ agents); multi-agent supervisor | "Any agent, any framework" + **watsonx.governance** (EU AI Act accelerators) |
| **Coveo** | AI search & relevance | Generative answers grounded in secure enterprise indexes | Relevance/ranking/search analytics depth even in agentic products |

### 3.1 Notable capabilities by vendor (the competitively important detail)

- **Microsoft 365 Copilot / Copilot Studio** — *Agent Mode* performs real multi-step edits inside the Office object model (formulas, slides). *Researcher* and *Analyst* are deep-reasoning agents fusing Graph + web + line-of-business connectors. *Copilot Studio* offers generative orchestration, deterministic agent flows, 1,400+ connectors incl. MCP, multi-agent (A2A), computer use, and human-in-the-loop. Fleet governance via *Agent 365*, *Entra Agent ID*, Purview, Defender. Hybrid pricing: per-seat + metered Copilot Credits.
- **Google Gemini Enterprise** — "front door for enterprise AI": enterprise search over a per-customer knowledge graph + no-code *Agent Designer*, prebuilt *Deep Research* / *Idea Generation* agents, *NotebookLM Enterprise*, and *Workspace Flows* (Gems as reasoning steps). Open standards: *A2A*, *AP2* (payments), *ADK*. 2026 governance: Agent Identity, Agent Gateway, Model Armor, Agent Simulation/Observability.
- **Amazon Q Business / Quick Suite** — *Q Apps* turn a chat into a shareable no-code app with action-taking plugin cards; *Quick Suite* adds Quick Research (deep research), Quick Sight (agentic BI), Quick Flows, and *Quick Automate* (multi-system workflows) over MCP to 1,000+ apps; *Bedrock AgentCore* provides a managed browser tool, gateway (API→MCP), memory, identity, and policy. The *Q index* is a permission-aware retrieval substrate verified ISVs can plug into.
- **Atlassian Rovo** — Rovo Search/Chat across 50+ connectors; *Rovo Studio* (no-code Agents/Automations/Apps), *Max Mode* (autonomous multi-step with HITL), *Rovo Dev* (agentic coding across CLI/IDE/Bitbucket). The Teamwork Graph is being opened to third-party agents via MCP. Bundled with paid Atlassian plans.
- **Dust** — "multiplayer" shared agents that collaborate with each other (Run Agent) and humans (@mention into a thread); *Skills* (reusable, versioned, propagating); *Triggers* (cron/webhook with write-back); productized for non-engineers (a PM built 242 agents). Open-source core, EU residency, MCP-native with argument-level tool approval.
- **Moveworks** (ServiceNow) — employee-support copilot in Slack/Teams; *Reasoning Engine* (understand→plan→execute→adapt); *Agentic Automation* explicitly positioned to replace iPaaS (Manifest Generator / Slot Resolvers / Policy Validators / Action Orchestrator); AI Agent Marketplace (1,000+ agents). Now ServiceNow's employee-facing AI front end.
- **Aisera** (Automation Anywhere) — *Universal Bot* routes across IT/HR/Finance/CX; disclosed 64–84% auto-resolution; *Agent Composer* + Hyperflow/Workflow/Event/Prompt studios; *LLM Gateway* (BYO models + ~30–60% token-cost cuts); open-standards interop (A2A/MCP/AGNTCY).
- **ServiceNow** (Now Assist + AI Agents) — *AI Agent Studio* (NL build) + *AI Agent Orchestrator* (plans/dispatches multi-agent) on the Now Platform = a system of *action*. Foundation: Workflow Data Fabric + Knowledge Graph + *RaptorDB Pro*. *AI Control Tower* governs native *and third-party* agents with runtime hallucination/bias/drift detection. *AI Agent Fabric* embeds A2A + MCP.
- **Salesforce Agentforce** — *Atlas Reasoning Engine* (System-2 + hybrid reasoning); *Agent Script* (portable, deterministic agent language) addresses reliability; *Agentforce Builder/Studio*, Subagents, prebuilt Einstein SDR/Service/Sales-Coach agents; grounded in Data Cloud; Slack-first; MuleSoft turns APIs into agent actions; *Einstein Trust Layer* + *Command Center* (OpenTelemetry). Consumption pricing (Flex Credits ~$0.10/action).
- **Writer** — full-stack: self-trained *Palmyra* models (incl. domain *Palmyra Med/Fin*, Fin passed CFA III), graph-based RAG (#1 RobustQA 86.31%), *AI Studio* (no-code + Agent Builder + APIs), *Action Agent* (writes/runs its own code in an isolated VM, 600+ connectors, self-correcting), *Playbooks/Routines* (NL reusable workflows, schedulable), *Agent Skills*, *Event-Based Triggers*, and a supervision suite (approval workflows, guardrails, Datadog/Noma/Lakera).
- **Workday Sana** — agentic knowledge assistant (Find/Act/Build/Automate) with a meeting + learning heritage (records/transcribes/indexes meetings as knowledge); model-agnostic; orchestrates Workday-native + custom + third-party agents. Acquired by Workday (~$1.1B).
- **Cohere North** — secure, privacy-first agentic workspace deployable on-prem/VPC/air-gapped on ~2 GPUs; built on Cohere's Command/Embed/Rerank; IdP-aware permissions; targets finance, public sector, regulated industries.
- **Guru** — "governed knowledge layer": cited, permission-aware answers; the Cards model; *Verification* + measurable *Trust Score*; *Knowledge Triggers* (push the right verified card based on on-screen context); *Knowledge Agents* that detect conflicts, merge duplicates, draft updates from chat threads, and auto-verify/unverify; MCP server exposes the governed layer to other AI tools.
- **Notion AI** — workspace-native; Enterprise Search across Slack/Drive/GitHub/Jira; *AI Meeting Notes* (captures system audio, no bot); *Agents* that run 20+ min, create/edit hundreds of pages/databases with memory and row-level permissions; *Custom Agents* run on schedules/triggers; credit-based pricing for Custom Agents.
- **OpenAI ChatGPT Enterprise** — *ChatGPT agent* unifies Deep Research + Operator (visual browser) + terminal + connectors; *Canvas*; *Custom GPTs*; *Tasks* (scheduled); *AgentKit* (Agent Builder, ChatKit, Connector Registry); "company knowledge" with cited, permission-aware answers.
- **Anthropic Claude Enterprise** — *Projects*, *Artifacts*, *Claude Code* (agentic coding on Bedrock/Vertex), *MCP* (open standard, donated to Linux Foundation), *Agent Skills* (open `SKILL.md` standard), *Cowork* (desktop/general-computing agent), 500K context, Compliance API, SSO/SCIM/RBAC.
- **Perplexity Enterprise** — internal knowledge search + live web with always-on citations; *Spaces*; *Labs* (builds reports/dashboards/spreadsheets/web apps); *Comet* (agentic browser, enterprise edition); ZDR posture; multi-model.
- **Hebbia** — *Matrix* spreadsheet-style agentic analysis over thousands of a customer's own documents via *ISD (Iterative Source Decomposition)* for "effectively infinite context," with sentence-level citations; 7-agent "Deeper" research system; FlashDocs for branded slide generation. Finance/legal/diligence focus.
- **AlphaSense** — market-intelligence search over a 500M+ premium-document corpus + Tegus expert-call transcripts; *Assistant* (ASLLM), *Generative Search/Grid*, *Deep Research* (agentic), *Enterprise Intelligence* (internal content with on-network isolation).
- **IBM watsonx Orchestrate** — no-code *Agent Builder* + *Agent Catalog* (150+ agents / 500+ tools), multi-agent supervisor/orchestration, A2A + MCP + Agent Connect, 80+ app integrations, Granite + external models, and *watsonx.governance* (AgentOps, factsheets, Compliance Accelerators for EU AI Act / ISO 42001 / NIST AI RMF).
- **Coveo** — relevance/ranking heritage; generative answers grounded in secure enterprise indexes; strong search analytics — a reminder that retrieval quality and analytics remain core even in agentic products.

---

## 4. Category-Wide Shifts (and the Beacon opportunity in each)

### 4.1 From search to work completion
Search is the first step of a larger workflow — decide, prep a meeting, resolve a ticket, update a record, answer a customer. Products that stop at retrieval leave the work unfinished.
*Beacon opportunity:* always ask what the next step is; offer one-click follow-on actions after a cited answer; preserve traceability from answer → source → action.

### 4.2 From chat to artifacts
Users need durable deliverables: memos, briefs, tickets, CRM updates, PRDs, RFP responses, contracts, dashboards, spreadsheets, slides, project updates, KB articles — increasingly produced *inside* the document or system of record (Office Agent Mode, Notion Agents).
*Beacon opportunity:* treat generated work as editable artifacts; track citations and assumptions inside the artifact; export or write back to working tools.

### 4.3 From reactive assistant to proactive coworker
The newest movement is proactive work management: watch projects, commitments, meetings, blockers, changes, and requests, then prepare suggested work.
*Beacon opportunity:* daily work brief; detect open loops across meetings/chat/email/tickets/docs; draft updates and follow-ups before the user asks; let users tune monitored signals.

### 4.4 From prompt chains to governed agents
Reusable agents are replacing repeated prompt patterns. The difference is durability: configuration, tools, memory, triggers, sharing rules, version history, logs — plus reusable **Skills** and schedulable **Playbooks/Routines**.
*Beacon opportunity:* agent templates for common loops; NL creation with deterministic controls; require observability and approval policies before agents run unattended.

### 4.5 From single assistant to agent fleet
As organizations create many agents, they need an **AgentOps control plane**: inventory, ownership, status, permissions, costs, evaluations, incidents, audit logs, per-agent identity, runtime monitoring, deprecation.
*Beacon opportunity:* design for agent lifecycle from the start; every agent owned, scoped, versioned, observable, and disable-able; "who can run/build/share this agent" is a first-class setting.

### 4.6 From retrieval quality to knowledge trust
Good retrieval over bad knowledge still produces bad outcomes. Verification, Trust Scores, and knowledge maintenance are becoming part of AI operations.
*Beacon opportunity:* show verification state, owner, freshness, and conflict warnings; route stale/low-confidence answers to owners; let agents draft knowledge updates but require owner approval.

### 4.7 From standalone tools to consolidated platforms
ServiceNow→Moveworks, Workday→Sana, Automation Anywhere→Aisera signal that buyers increasingly want agentic AI inside platforms they already own. Independent products differentiate on **horizontal breadth, openness/interoperability, governance depth, or vertical depth**.
*Beacon consideration:* be deliberately horizontal and interoperable (open protocols, MCP-style access), or pick a defensible vertical wedge — but plan for a world where suite vendors bundle "good enough" agents.

---

## 5. Capability Deep-Dives (cross-vendor synthesis)

A capability-by-capability view, with the vendors that set the bar. These map directly to the epics in [user-stories.md](user-stories.md).

- **Unified search & answers** (Glean, Coveo, Dropbox Dash, Atlassian Rovo, Amazon Q, Notion) — hybrid lexical+semantic retrieval, personalization, people/expert search, org structure, advanced operators, inline previews, cited permission-aware answers, conflict surfacing, knowledge-gap feedback.
- **Assistant & generation** (Glean, Copilot, Gemini, frontier labs) — Q&A, summarization, cross-source reasoning, drafting, file analysis, memory/personalization, model choice, multimodal (image understanding + generation), voice, translation, prompt libraries; multi-surface (web, browser, chat, desktop, embedded).
- **Proactive work intelligence** (Glean "Proactive Intelligence," Copilot, Sana) — daily briefs, meeting prep, commitment/follow-up detection, risk cards, change monitoring, explainable personalization, notification batching.
- **Work execution & actions** (Glean Actions, Moveworks Agentic Automation, Q plugins, Agentforce, ServiceNow) — read/write actions across systems of record, in-app/database agency, action risk tiers, approvals, read-back verification, multi-step chained workflows; "iPaaS replacement" via NL→API.
- **Agent builder & reusable blocks** (Copilot Studio, Glean, Dust, Writer, Rovo Studio, watsonx) — no-code NL creation, scoping, branching/variables, per-step model selection, **Skills**, **Playbooks/Routines**, templates/marketplaces, preview/debug, versioning/rollback, ownership.
- **Autonomous/scheduled/event-driven** (Dust Triggers, Writer Routines+Event Triggers, Notion Custom Agents, Copilot autonomous agents, OpenAI Tasks) — schedules, event triggers, inbox/channel monitoring, policy-bounded auto write-back, exception escalation, multi-agent orchestration, per-agent autonomy levels, scope-change auto-pause.
- **Research & evidence** (Deep Research across the field; Hebbia Matrix/ISD; AlphaSense Generative Grid + premium corpus; MS Analyst code execution) — plan→search→synthesize cited reports; exhaustive evidence grids over thousands of docs; structured+narrative analysis; data-room diligence; assumptions/conflicts exposed; reusable research templates.
- **Artifacts & content** (Glean Canvas, OpenAI Canvas, Anthropic Artifacts, Perplexity Labs, Copilot Pages, Hebbia FlashDocs, Q Apps) — memos, PRDs, decks, spreadsheets, KB articles, brand/style enforcement, provenance, co-authoring canvases, chat→app.
- **Meetings & comms** (Notion no-bot capture, Sana meetings-as-knowledge, Copilot Facilitator, Agentforce Voice) — capture/transcribe/summarize/index, action-item extraction, thread summaries, stakeholder updates, decision logs, recurring comms agents, request tracking, voice.
- **Knowledge governance** (Guru Trust Score/Knowledge Triggers/Knowledge Agents, Glean verification) — verification with expiry, stale/duplicate/conflict detection, gap analytics, expert routing, canonical answers, proactive context-based surfacing, trust dashboards.
- **Computer use & browser/desktop** (OpenAI Operator/agent, Anthropic Cowork/Claude-in-Chrome, Perplexity Comet, Amazon AgentCore Browser, Writer Action Agent, MS computer use) — operate UIs without APIs, agentic browser, desktop general-computing agent, sandboxed virtual computer, self-correction. **Higher-risk; later-stage.**
- **Security & fleet governance** (MS Agent 365/Entra Agent ID/Purview, ServiceNow AI Control Tower, Salesforce Trust Layer/Command Center, Google Model Armor, Writer Supervise, Glean Protect, watsonx.governance) — permission inheritance, sensitive-data detection, prompt-injection defense, model governance, audit/SIEM, policy simulation, residency/isolation, control plane, per-agent identity, runtime monitoring, no-training/ZDR/BYOK, sovereign deployment.
- **Developer platform** (Glean APIs/Toolkit, OpenAI AgentKit, Anthropic MCP/Skills, Amazon AgentCore Gateway, Google ADK) — search/context APIs, connector SDKs, custom actions, MCP-style external access, events/webhooks, sandboxes, local dev, embedding, code-first agent toolkits ("define a tool once, reuse across frameworks").

---

## 6. Key Competitive Metrics (public claims, mid-2026)

Vendor-reported; treat as directional marketing, not audited fact.

| Vendor | Metric | Value |
|---|---|---|
| Glean | Hours saved per user/year | 110 |
| Glean | ROI (Forrester TEI, 3 yrs) | 141% |
| Glean | Enterprise adoption in < 2 yrs | 93% |
| Glean | Token reduction vs. off-the-shelf MCP | 30% |
| Glean | Time to ROI | < 6 months |
| Moveworks | Employees relying on platform | millions |
| Moveworks | AI agents built (in marketplace/customers) | 1,000+ |
| Moveworks | Typical time to value | ~8 weeks |
| Aisera | Ticket auto-resolution rate | 64–84% |
| Writer | RobustQA score (graph-RAG) | 86.31% (ranked #1) |
| Writer | Avg. ROI / productivity returned | ~9x / ~7.5 hrs per employee/week |
| Hebbia | Pages processed (platform) | 1B+ |
| AlphaSense | Premium-document corpus | 500M+ docs + expert transcripts |
| Notion AI | Custom Agents pricing | $10 per 1,000 credits |
| Microsoft | Copilot agent metering | per-credit (per-seat + consumption) |
| Salesforce | Agentforce action pricing | ~$0.10 per action (Flex Credits) |

---

## 7. Mental Models

### 7.1 The agentic stack (how capabilities layer)
1. **Connect & index** — connectors, knowledge graph, permission mirroring.
2. **Retrieve & ground** — hybrid/graph RAG, citations, web + premium content.
3. **Reason & plan** — agentic engines, deep research, sub-agents, code execution.
4. **Act** — read/write actions, computer use, MCP tools, iPaaS-replacement.
5. **Automate** — triggers, schedules, orchestration, write-back.
6. **Build** — no-code builders, Skills, Playbooks, templates, APIs/SDKs.
7. **Govern** — control plane, identity, guardrails, observability, evals, deployment/sovereignty.

### 7.2 Spectrum of autonomy (specify which rung a feature targets)
1. **Assistive** — suggests; human does the work.
2. **On-demand agent** — multi-step task when prompted (deep research, ChatGPT agent).
3. **Scheduled agent** — runs on a cadence unattended (Routines, Tasks, Custom Agents).
4. **Event-driven agent** — runs when a signal fires, writes back (Writer/Dust triggers).
5. **Orchestrated multi-agent** — a supervisor coordinates specialists across systems (ServiceNow Orchestrator, Salesforce Atlas, A2A).
6. **Governed autonomous fleet** — many agents run continuously under a control plane with identity, guardrails, and observability (Agent 365, AI Control Tower).

---

## 8. Candidate Product Shape For Beacon

Research-derived candidate, **not** a final decision (OD-1 stays open).

**Core promise.** Beacon helps knowledge workers turn scattered company context into verified answers, finished work artifacts, and governed automations.

**Core users.** Knowledge workers losing time finding context, drafting updates, and chasing follow-ups; managers/executives needing briefs and decision support; operations owners running repeatable processes; knowledge managers needing trusted/fresh knowledge; admins/security governing AI access and agents; builders/developers extending the system.

**Core jobs to be done.**
1. Find a trusted, cited answer across all permitted systems.
2. Draft the work artifact using current company context.
3. Act on the right system with the right approval trail.
4. Monitor open loops and surface what changed or needs attention.
5. Turn a repeating process into a governed reusable agent.
6. Govern what agents can see and do as AI is deployed broadly.

**Product pillars.**
1. Context foundation — connectors, indexing, permissions, entities, activity, provenance.
2. Search & answers — unified search, citations, trust signals, expert routing.
3. Copilot workspace — chat, briefs, artifact drafting, research, analysis.
4. Proactive work — daily brief, task extraction, blockers, changes, follow-up.
5. Work execution — actions, write-back, approvals, system-of-record updates.
6. Agent builder — templates, no-code flows, Skills, schedules, triggers, versioning.
7. Knowledge governance — verification, ownership, staleness, conflicts, curation.
8. Security & AgentOps — permissions, policies, logs, evaluations, fleet inventory, identity, runtime monitoring.
9. Developer platform — APIs, custom connectors, tools/actions, MCP-style interoperability.

---

## 9. Feature Taxonomy & Open Design Questions

**Context & connectors.** Which sources ship first? Read-only vs. write-capable? Permission-propagation latency? How is freshness shown? How are custom schemas mapped?

**Search & answering.** Support precision ("find this doc," "who owns this project," "what did we decide") and discovery ("summarize status," "what changed since last week"); inspectable citations; conflict flags; poor-answer feedback.

**Proactive work intelligence.** Opt-in and transparent. Signals: calendar, mentions/assignments, email/chat requests, ticket status, milestone movement, CRM stage, doc edits, repeated search failures, stale verified content.

**Work execution.** Any system-changing action — create ticket, update record, draft/send message, create doc, update knowledge, assign owner, schedule meeting, open PR, add risk/status. High-impact actions require preview, approval, and audit.

**Agent builder.** Minimum: name, description, intended users, allowed sources, tools/actions, triggers, output format, approval policy, owner, test cases, version history, logs, publish/share controls. Advanced: branching, loops, variables, per-step model selection, evaluation sets, reusable Skills, nested agents, fallback/error handling, rate/cost controls.

**Knowledge governance.** Owners, verification states, review cadence, duplicate/conflict detection, stale alerts, gap detection, usage analytics, expert review, canonical-answer promotion, proactive context surfacing.

**AgentOps & security.** Inventory; owner/version/status/scope; who can build/edit/publish/run; connected tools/data; run logs and traces; output-quality feedback; cost/latency; failure/retry logs; policy violations; per-agent identity; runtime monitoring; kill switch and rollback; evaluation results.

---

## 10. Prioritization Guidance

**Good early candidates (high value, clear safety boundary):**
- permission-aware search and cited answers;
- meeting/thread summaries with action-item extraction;
- daily brief and open-loop detection;
- drafting artifacts from trusted sources;
- read-only research agents;
- ticket/CRM update drafts requiring human approval;
- knowledge trust signals and verification workflow.

**Riskier later candidates (gate behind security invariants — OD-4 — and verification gates — OD-5):**
- unattended write-back to systems of record;
- financial/legal/HR/security recommendations without clear guardrails;
- autonomous external communication;
- **browser/computer-use automation**;
- agent-to-agent delegation across vendors;
- fully automated code changes or production operations.

---

## 11. Risks And Product Failure Modes

- **Hallucinated certainty** — confident answers from weak/stale/conflicting sources. *Mitigate:* citations by default, confidence/source-quality signals, conflict detection, verified-answer promotion, "insufficient evidence" behavior.
- **Permission leakage** — restricted content revealed via snippets, summaries, embeddings, logs, or artifacts. *Mitigate:* source-permission inheritance, retrieval-time checks, permission-aware logging/redaction, tenant isolation, sensitive-data detection, negative tests for unauthorized/wrong-role access once a test stack exists.
- **Automation overreach** — agents act when they should draft or ask. *Mitigate:* action risk tiers, human-in-the-loop, dry-run previews, argument-level approval, rollback, per-agent action scope.
- **Knowledge rot** — AI amplifies outdated docs and gaps. *Mitigate:* ownership/review cadence, stale detection, gap analytics, expert review, auto-draft updates with owner approval.
- **Agent sprawl** — many overlapping, unowned, unsafe, low-quality agents. *Mitigate:* inventory, owner requirement, versioning, publish workflow, certification, usage analytics, retirement policy.
- **Context overload** — too many sources make answers slow, noisy, hard to verify. *Mitigate:* scoped sources by task, source-quality ranking, explicit query plans, inspectable context, user-controlled narrowing.

---

## 12. Source Links

**Glean:** https://www.glean.com/ · /product/enterprise-graph · /product/work-execution · /ai-agent-builder · /security · https://docs.glean.com/administration/protect/overview · https://docs.glean.com/administration/protect/ai-security/
**Microsoft:** https://www.microsoft.com/en-us/microsoft-365-copilot/microsoft-copilot-studio · https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/agent-builder · https://learn.microsoft.com/en-us/copilot/microsoft-365/researcher-agent
**Google:** https://cloud.google.com/gemini-enterprise · https://cloud.google.com/blog/products/ai-machine-learning/introducing-gemini-enterprise · https://docs.cloud.google.com/gemini/enterprise/docs/agent-designer
**Amazon:** https://aws.amazon.com/q/business/ · https://aws.amazon.com/quick/ · https://aws.amazon.com/bedrock/agentcore/
**Atlassian:** https://www.atlassian.com/software/rovo · /software/rovo/features · /software/rovo-dev · https://support.atlassian.com/rovo/docs/agents/
**Dust:** https://dust.tt/home/product · https://docs.dust.tt/docs/skills · https://dust.tt/blog/introducing-triggers-your-agents-working-while-you-sleep
**Moveworks:** https://www.moveworks.com/ · /us/en/platform/reasoning-engine · /us/en/platform/enterprise-search
**Aisera:** https://aisera.com/ · https://aisera.com/platform/agent-studio/ · https://aisera.com/blog/llm-gateway-for-generative-ai/
**ServiceNow:** https://www.servicenow.com/products/ai-agents.html · newsroom press releases (Knowledge 2025/2026)
**Salesforce:** https://www.salesforce.com/agentforce/ · /agentforce/script/ · /agentforce/levels-of-determinism/
**Writer:** https://writer.com/product/ai-studio/ · https://dev.writer.com/agent-builder/agent-architecture · https://dev.writer.com/home/knowledge-graph-concepts
**Workday Sana:** https://www.workday.com/en-us/artificial-intelligence/workday-sana.html · https://sanalabs.com/
**Cohere North:** https://cohere.com/blog/north-ga · https://cohere.com/command
**Guru:** https://www.getguru.com/ · https://help.getguru.com/docs/intro-to-knowledge-agents · https://www.getguru.com/features/knowledge-triggers
**Notion:** https://www.notion.com/product/ai · /product/agents · /help/custom-agents
**OpenAI:** https://openai.com/index/introducing-chatgpt-agent/ · /index/introducing-company-knowledge/ · AgentKit (DevDay 2025)
**Anthropic:** https://claude.com/blog/claude-for-enterprise · https://www.anthropic.com/news/model-context-protocol · Agent Skills (agentskills.io)
**Perplexity:** https://www.perplexity.ai/enterprise · /hub/blog/introducing-perplexity-labs · Comet for Enterprise
**Hebbia:** https://www.hebbia.com/ · /product · /blog/introducing-matrix-the-interface-to-agi
**AlphaSense:** https://www.alpha-sense.com/ · Deep Research / Generative Grid / Enterprise Intelligence releases
**IBM watsonx Orchestrate:** https://www.ibm.com/products/watsonx-orchestrate · /new/announcements/new-agentic-workflows-and-domain-agents-in-ibm-watsonx-orchestrate
**Coveo:** https://www.coveo.com/en · /en/platform/generative-ai

---

*Caveats:* Vendor/product details and metrics are drawn from public marketing, docs, and reputable third-party coverage as of mid-2026; some figures are vendor-reported and directional. Pricing across this market is largely quote-based. Feature maturity shifts quickly — flag beta/region-locked capabilities when promoting any item into product scope.
