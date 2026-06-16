# Glean — Product Research & User Stories

> A thorough, feature-by-feature catalog of what Glean (glean.com) does, expressed as user stories.
> Compiled June 2026 from glean.com, docs.glean.com, developers.glean.com, trust.glean.com, Glean's blog/press, and third-party sources.
>
> 📎 **Companion doc:** [knowledge-work-automation-user-stories.md](knowledge-work-automation-user-stories.md) — widens this to Glean's competitors (Microsoft 365 Copilot, Google Gemini Enterprise, Amazon Q/Quick Suite, Dropbox Dash, Atlassian Rovo, Dust, Moveworks, Aisera, ServiceNow, Salesforce Agentforce, Writer, Sana, Cohere North, Guru, Notion AI, OpenAI/Anthropic/Perplexity, Hebbia, AlphaSense, IBM watsonx Orchestrate) and adds **Epics 7–15** focused on knowledge-work automation.

---

## 1. What Glean Is (Context)

**Glean is an enterprise "Work AI" platform.** It connects to a company's existing applications, indexes their content (with permissions intact), and uses that to power three core pillars — **Search**, an **Assistant**, and **Agents** — all grounded in the company's own data and scoped to each individual user's access.

Glean's positioning lines (verbatim): *"Work AI that understands your company,"* *"Work AI for all,"* and *"the system of context for the enterprise."* It is deliberately **horizontal** (every department, every employee) rather than a single-use vertical tool.

**The foundation under everything** is the **Enterprise Graph** (an automatically-built knowledge graph of people, content, activity, and relationships across all company apps) plus a per-user **Personal Graph** (each employee's tasks, projects, collaborators, and work style). Together Glean calls this the **"System of Context."**

**The three pillars + supporting layers:**
- **Glean Search** — permission-aware enterprise search across 100+ (catalog: 275+) connected apps, returning synthesized, cited answers — not just document lists.
- **Glean Assistant** — a company-grounded AI chat assistant (a "ChatGPT for your business") that answers, summarizes, drafts, researches, analyzes, and executes work with citations.
- **Glean Agents** — a no-/low-code environment to build, deploy, orchestrate, and govern AI agents that reason and take action across systems.
- **Glean Apps / Connectors & Actions** — custom AI apps + the integration and write-action layer.
- **Model Hub** — model-agnostic access to 15–35+ commercial and open LLMs.
- **Glean Protect / Protect+** — the security & governance layer.
- **Developer Platform** — Client API, Indexing (Push) API, Agents API, SDKs, Agent Toolkit, MCP.

---

## 2. Personas (Legend)

| Tag | Persona | Description |
|---|---|---|
| **KW** | Knowledge Worker | Any employee / general end user |
| **NH** | New Hire | Employee being onboarded |
| **ENG** | Software Engineer | Developer working in code, GitHub, IDE |
| **SALES** | Sales Rep / AE | Account executive, SDR, sales |
| **SUP** | Support Agent | Customer support / service / CSM |
| **IT** | IT / Help Desk | IT support, ITSM, help-desk agent |
| **HR** | HR / People Ops | HR business partner, recruiter |
| **MKT** | Marketer | Content, campaigns, demand gen |
| **PM** | Product Manager | Product / program management |
| **LEGAL** | Legal Counsel | Legal, compliance, contracts |
| **FIN** | Finance Analyst | Finance, FP&A, operations |
| **DATA** | Data Analyst | Analytics / BI / data teams |
| **EXEC** | Executive / Leader | Manager, director, C-suite |
| **BLD** | Citizen Builder | Non-technical person building agents/apps |
| **DEV** | Developer / Platform Eng | Technical builder using APIs/SDK/MCP |
| **ADM** | Glean Admin | Platform owner / administrator |
| **SEC** | Security / Compliance | Security, governance, compliance owner |
| **KM** | Knowledge Manager | Content curator / knowledge owner |

**Story format:** *As a [persona], I want to [capability], so that [benefit].* Key stories include **AC** (acceptance criteria) and a **Feature** tag mapping to the real Glean capability.

---

# EPIC 1 — Enterprise Search (Glean Search)

## 1.1 Universal / Unified Search

**E1-1.** As a **KW**, I want to search across all my company's apps from a single search box, so that I don't have to log into and search each tool separately.
- **Feature:** Universal search across 100+ (275+ catalog) connectors — Google Drive, Slack, Jira, Confluence, GitHub, Salesforce, Zendesk, ServiceNow, Notion, Box, SharePoint, Gmail, etc.
- **AC:** One query returns a unified, ranked result set spanning every connected source the user can access; results show source app, title, snippet, owner, and last-modified date.

**E1-2.** As a **KW**, I want search results to appear within minutes of content being created or changed in a source app, so that I'm working from current information.
- **Feature:** Continuous indexing / change monitoring (webhooks + periodic diffs); tracks new content, deletions, and permission changes.

**E1-3.** As a **KW**, I want to broaden a search across every Glean instance and connector I'm permitted to use, so that I find content even when I don't know which system it lives in.
- **Feature:** Federated `*` (asterisk) wildcard operator across all permitted instances.

**E1-4.** As a **KW**, I want to preview a document's contents inline in the results without opening the source app, so that I can confirm relevance before clicking through.
- **Feature:** In-line document previews / knowledge cards.

## 1.2 Knowledge Graph & Personalization

**E1-5.** As a **KW**, I want results ranked by my role, team, projects, and the people I work with, so that the most relevant items for *me* surface first even on an ambiguous query.
- **Feature:** Enterprise Graph + Personal Graph personalization; ranking signals include role/department, team, reporting structure, location, past activity, click signals, document popularity, collaboration patterns, freshness.
- **AC:** Two users with different roles/permissions can issue the identical query and receive different, individually-relevant results.

**E1-6.** As a **KW**, I want Glean to understand company-specific entities (projects, products, customers, acronyms) and how they relate, so that a query like "Where do I file feature requests for [Customer]?" resolves across multiple systems.
- **Feature:** Enterprise Graph entity extraction + multi-hop relational reasoning; entity disambiguation (e.g., "Reddit" the customer vs. an ad line item).

**E1-7.** As a **KW**, I want search quality to improve the longer my company uses Glean, so that relevance keeps getting better without manual tuning.
- **Feature:** Self-tuning ranking models ("Scholastic") that adapt language models to each customer's corpus and clicks.

**E1-8.** As an **EXEC**, I want to run deterministic, exhaustive structured queries like "List all account executives in APAC," so that I get a complete enumerated answer rather than a probabilistic guess.
- **Feature:** Knowledge-Graph structured/deterministic queries (triplet model: subject–predicate–object).

## 1.3 Natural-Language, Semantic & Hybrid Search

**E1-9.** As a **KW**, I want to ask questions in plain natural language ("How much can I expense on a hotel?") instead of guessing keywords, so that I find answers the way I'd ask a colleague.
- **Feature:** Vector/semantic search + query-understanding pipeline (intent classification, synonymy, acronym expansion).

**E1-10.** As a **KW**, I want search to combine exact keyword matching with semantic meaning, so that I get both precise string matches and conceptually-relevant results.
- **Feature:** Hybrid Search (lexical BM25 + vector) fused via Reciprocal Rank Fusion (RRF) with score calibration; ~30% reduction in irrelevant results.

**E1-11.** As a **KW**, I want search to tolerate typos and expand company-specific abbreviations, so that I still find the right content and people despite spelling mistakes.
- **Feature:** Enterprise spell-check / fuzzy search with personalized corrections derived from the company corpus.

**E1-12.** As a **KW**, I want an immediate generated answer with citations at the top of my search results, so that I get the answer itself, not just a list of documents to read.
- **Feature:** AI Answers — permission-aware, deterministic, citation-backed generated answers in the search experience.
- **AC:** Each answer cites specific source documents the user is authorized to see; identical queries yield consistent answers.

## 1.4 People Search, Expertise & Org Chart

**E1-13.** As a **KW**, I want to search for people and see enriched profiles (role, team, tenure, location, reporting line, recent projects), so that I know who someone is and what they work on.
- **Feature:** People-aware search + Unified Identity profiles built from HRIS + activity across tools.

**E1-14.** As a **NH**, I want to find the internal expert on a topic ("who knows about X"), so that I can ask the right person instead of guessing.
- **Feature:** Expert Search (`expert in [[domain]]`) — expertise inferred from authorship, ticket resolution, and contributions, not static profile fields.

**E1-15.** As a **KW**, I want a single query to return relevant documents *and* their owners/experts together, so that I get both the content and the human context in one view.
- **Feature:** Blended people + content results.

**E1-16.** As an **EXEC**, I want an explorable org chart with reporting lines and responsibilities, so that I can quickly locate the right stakeholders.
- **Feature:** Org Chart (explorable; admins can export data).

## 1.5 Filters, Operators & Faceting

**E1-17.** As a **KW**, I want command-line-style filters (`app:`, `type:`, `from:`, `updated:`, `owner:`, `status:`), so that I can precisely narrow results.
- **Feature:** Full search operator grammar — app/type/person/date/container/status/priority filters with `-` negation, OR/AND logic, and exact-phrase quoting.

**E1-18.** As a **KW**, I want to filter by time with natural-language ranges (`updated:past_week`, `updated:2024-Q1`), so that I can find the most recent or period-specific content fast.
- **Feature:** Date/time filters (natural-language + specific dates/quarters).

**E1-19.** As a **KW**, I want to layer filters incrementally (keywords → app → type → person → time), so that I can drill down to exactly what I need.
- **Feature:** Faceted search + recommended incremental filtering.

## 1.6 Verification, Trust & Knowledge Curation

**E1-20.** As a **KW**, I want to see a green "Verified" badge on trusted documents (with who verified it and when), so that I know which content is accurate and current.
- **Feature:** Verification / Verified badge with hover provenance.

**E1-21.** As a **KM**, I want to request verification of a document from its owner and set re-verification reminders, so that important content stays fresh and trustworthy.
- **Feature:** Verification Tasks, request-verification, refresh reminders, deprecation marking.

**E1-22.** As a **KM**, I want to author short "Answer" cards for common questions, so that authoritative answers surface at the top of search and in Slack.
- **Feature:** Answers (curated knowledge cards) — created by anyone, with audience targeting, Collections grouping, co-editors, markdown.

**E1-23.** As a **KM**, I want to build curated Collections of links/docs across apps, so that I can package a topic (e.g., "Benefits," "Onboarding") into one shareable, searchable resource.
- **Feature:** Collections — curated cross-app link sets, org-shareable, appear in search, attachable to Answer cards.

**E1-24.** As an **ADM**, I want to pin key items to the top of results, so that critical information is highlighted for everyone.
- **Feature:** Pins.

## 1.7 Home / Feed / Recommendations / Mentions

**E1-25.** As a **KW**, I want a personalized home page combining search, chat, and a feed of suggested/recent content, so that I land on what matters without searching.
- **Feature:** Work Hub home page — Suggested, Trending, Calendar, Mentions, Go Links, Collections, Announcements, recents.

**E1-26.** As a **KW**, I want to see content trending across my team and company, so that I stay aware of what's relevant right now.
- **Feature:** Trending content card.

**E1-27.** As a **KW**, I want a feed of recent activity where I was @mentioned or assigned across apps, so that I never miss something that needs my attention.
- **Feature:** Mentions Feed (Confluence, Google Drive, GitHub, Jira) with mark-read, dismiss, copy-link, summarize; 10-day window.

**E1-28.** As a **KW**, I want to customize my home page layout, theme, default composer (Search vs Chat), and set Glean as my new-tab page, so that it fits how I work.
- **Feature:** Home customization (Informational/Simple layouts, themes, backgrounds, language, default composer).

## 1.8 Browser Extension, Sidebar, New Tab & Go Links

**E1-29.** As a **KW**, I want a browser sidebar (Cmd/Alt+J) that searches and chats using the current page's context plus company knowledge, so that I get help without leaving the page I'm on.
- **Feature:** Browser extension Sidebar (Chat + Search tabs), works on indexed and unindexed pages.

**E1-30.** As a **KW**, I want to highlight text on any web page and instantly Explain / Summarize / Translate / Find related docs / Find experts / Improve writing, so that I can act on what I'm reading in place.
- **Feature:** Glean Companion floating widget (6 actions; translate into 13 languages; only reads explicitly-highlighted text).

**E1-31.** As a **KW**, I want a screenshot tool in the sidebar to capture non-selectable content, so that I can ask Glean about images or un-highlightable text.
- **Feature:** Sidebar screenshot capture.

**E1-32.** As a **KW**, I want every new browser tab to surface recommended docs, recent items, calendar events, notifications, and trending content, so that my browser becomes a gateway to company knowledge.
- **Feature:** New Tab Page.

**E1-33.** As a **KW**, I want memorable `go/` shortcuts (e.g., `go/benefits`, `go/it-help`) that redirect to the right destination, so that I and my teammates can reach key resources instantly.
- **Feature:** Go Links — 5 creation paths, edit-restriction option, Slack auto-unfurl, `has:golink` filter, variable Go Links, department-level visit analytics.

## 1.9 Code Search

**E1-34.** As an **ENG**, I want to search across all our repositories with code-aware tokenization (camelCase/snake_case) and semantic chunking, so that I can find implementations by name or by intent.
- **Feature:** Code Search — dual lexical (OpenSearch) + semantic (AST-chunked vector) indexes; operators like `extension:java`; ~50ms P95; permission-mirrored.

**E1-35.** As an **ENG**, I want code search to respect repo/team ACLs at query time, so that I only see code I'm authorized to access.
- **Feature:** Code permissions crawl mirroring source ACLs, enforced at query time; code stays in the customer's Glean VPC.

## 1.10 Permissions in Search

**E1-36.** As a **SEC**, I want search to enforce each source app's existing document-level permissions in real time, so that employees can never find content they aren't authorized to see.
- **Feature:** Permission-aware search; identity data in the Knowledge Graph governs visibility; permission changes reflected within minutes.
- **AC:** A private Slack channel or restricted Drive file the user can't access never appears in their results; revoking access in the source removes it from Glean promptly.

---

# EPIC 2 — Glean Assistant (AI Chat)

## 2.1 Company-Grounded Chat & Citations

**E2-1.** As a **KW**, I want to ask the Assistant natural-language questions and get answers synthesized from across my company's tools, so that I get a direct answer instead of hunting through documents.
- **Feature:** Glean Assistant — RAG over the Enterprise Graph + Personal Graph; permission-aware.

**E2-2.** As a **KW**, I want every answer to include citations to the exact source snippets, so that I can verify the answer and trust it.
- **Feature:** Deep-Linked Citations — text-level (not just document-level) citations with exact snippets, page numbers, and highlight ranges (with GPT/Claude models).
- **AC:** Statements grounded in company content show clickable citations to the supporting passages; pure world-knowledge answers are clearly distinguished.

**E2-3.** As a **KW**, I want the Assistant to automatically blend internal company knowledge, the public web, and the model's own knowledge per question, so that I get the most complete answer without choosing a "mode."
- **Feature:** All Knowledge mode + "Search the web" / "Use company sources" toggles.

**E2-4.** As a **KW**, I want a choice between a fast answer and a deeper reasoning pass, so that I can trade speed for analytical thoroughness when it matters.
- **Feature:** Fast Mode vs. Thinking/Extended mode (Agentic Engine 2; default agentic engine since Oct 2025).

## 2.2 Surfaces (Where the Assistant Lives)

**E2-5.** As a **KW**, I want to use the Assistant in a full web app, so that I have a dedicated workspace for chat, research, and drafting.
- **Feature:** Glean web app (home of Deep Research, Canvas, file upload, model choice).

**E2-6.** As a **KW**, I want to chat with the Assistant inside Slack (DM and a persistent sidebar) with context-aware suggested prompts and thread history, so that I get help without leaving my conversations.
- **Feature:** Glean for Slack (DM, sidebar, suggested prompts, History tab, `/glean`).

**E2-7.** As a **KW**, I want to invoke Glean agents in Microsoft Teams via 1:1 DM, sidebar pinning, or `@mention` in channels, and execute in-chat actions, so that Glean is embedded in my Teams workflow.
- **Feature:** Glean Agents in MS Teams (in-chat actions for Jira SM, ServiceNow, Salesforce, Zendesk, Snowflake, Databricks; max 3 agents).

**E2-8.** As a **KW**, I want a native desktop app (Cmd-Shift-J) with an always-on-top Quick Chat and cross-window context awareness, so that Glean can see what I'm working on across multiple apps and help side-by-side.
- **Feature:** Glean desktop app (macOS/Windows) — Quick Chat, full cross-window context, Screenshot-to-Chat (macOS).

**E2-9.** As a **KW**, I want the same prompts, citations, and feedback controls everywhere I use the Assistant, so that my experience is consistent across web, extension, Slack, Teams, and desktop.
- **Feature:** Consistent Assistant across surfaces.

## 2.3 Proactive Intelligence

**E2-9a.** As a **KW**, I want personalized activity cards surfaced proactively based on my work patterns, so that I see what needs my attention before I ask — upcoming meetings, recent changes to my projects, and shifting priorities.
- **Feature:** Proactive Intelligence — personalized suggestions, activity cards, and recommendations grounded in Personal Graph + Enterprise Graph.
- **AC:** Cards appear on the home page and in-app without a query; they reflect the user's calendar, projects, collaborators, and recent activity.

**E2-9b.** As a **KW**, I want to be alerted when key documents, projects, or deals I'm tracking change significantly, so that I catch blockers and updates without monitoring every tool.
- **Feature:** Proactive change detection and surfacing (updates, blockers, priority shifts).

**E2-9c.** As an **EXEC**, I want a daily/weekly briefing synthesized from across my company's systems, so that I start each day oriented on what matters most.
- **Feature:** Proactive Intelligence briefing cards; scheduled agent runs.

## 2.4 Capabilities

**E2-10.** As a **KW**, I want the Assistant to summarize long documents, threads, channels, or meetings, so that I grasp the key points in seconds.
- **Feature:** Summarization (incl. Companion "Summarize this").

**E2-11.** As a **KW**, I want the Assistant to draft and refine content — emails, docs, slides, messages — grounded in company context and my writing style, so that I produce on-brand work faster.
- **Feature:** Content creation + Adaptive/Personalized Writing (learns tone across 100+ apps).

**E2-12.** As a **KW**, I want to compare and analyze multiple documents at once, so that I can reconcile or synthesize information across sources.
- **Feature:** Cross-document analysis / research.

**E2-13.** As a **KW**, I want the Assistant to translate text into many languages, so that I can work across a multilingual organization.
- **Feature:** Translation (13 languages via Companion; conversational translation in chat).

**E2-14.** As a **KW**, I want the Assistant to understand images I share and generate images/visuals, so that I can work with and produce visual content in-line.
- **Feature:** Multimodal image understanding + image generation (PNG download; Nano Banana Pro initial provider).

**E2-15.** As a **KW**, I want to talk to the Assistant by voice, so that I can capture notes and ask questions hands-free.
- **Feature:** Real-time voice.

**E2-16.** As a **KW**, I want the Assistant to handle complex multi-step tasks by planning, orchestrating sub-agents, and refining until complete, so that I can delegate open-ended work by just describing the outcome.
- **Feature:** Agentic Engine 2 — adaptive planning, sub-agent orchestration, "scouts," clarifying questions, zero prompt engineering.

## 2.5 Prompt Library

**E2-17.** As a **KW**, I want a browsable library of curated prompt templates organized by department and category, so that I can start from proven prompts instead of a blank box.
- **Feature:** Prompt Library (by team: Eng, Marketing, Sales, etc.; by category: Search, Create, Summarize, Analyze, Onboard, Explore, Research).

**E2-18.** As a **KW**, I want to create, test, save, and share my own simple and multi-step prompts, so that I can standardize repeatable workflows for myself and my team.
- **Feature:** Prompt builder + advanced (multi-step) prompts; "Request a prompt."

## 2.6 File Upload & Data Analysis

**E2-19.** As a **KW**, I want to upload files (PDF, DOCX, XLSX, CSV, images, code, archives) and immediately ask questions about them, so that I can analyze my own documents alongside company knowledge.
- **Feature:** File Upload (up to 5 files / 64MB on 128K models; broad file-type support; malware-scanned; sandboxed archives).

**E2-20.** As a **DATA**, I want to ask analytical questions about an uploaded or tagged spreadsheet (column stats, distributions, time-series), so that I can explore data conversationally.
- **Feature:** Data Analysis (sandboxed code interpreter; .xlsx/.csv/JSON).

**E2-21.** As a **SEC**, I want uploaded files to be private to the uploader and shared chat participants and never used for training, so that sensitive documents stay protected.
- **Feature:** Upload sandboxing, scoped access, zero-retention.

## 2.7 Model Choice (Model Hub)

**E2-22.** As a **KW**, I want to pick which LLM powers a specific chat (GPT, Claude, Gemini, etc.), so that I can match the model to the task.
- **Feature:** Model Hub — 15–35+ models; per-chat selection.

**E2-23.** As a **KW**, I want a "Best model" default that auto-selects the optimal model per chat based on quality/latency/cost, so that I get great results without thinking about models.
- **Feature:** "Best model" auto-selection.

**E2-24.** As an **ADM**, I want to curate which models employees can use, set the org default, and keep some models in limited rollout, so that I control cost, compliance, and change management.
- **Feature:** Admin model governance; Universal Model Key vs. BYO key; zero-retention vendor agreements.

## 2.8 Personalization, Memory & History

**E2-25.** As a **KW**, I want the Assistant to know my role, team, and active projects, so that answers are tailored to me without me re-explaining context every time.
- **Feature:** Personal Graph personalization.

**E2-26.** As a **KW**, I want the Assistant to remember facts I tell it and infer my preferences over time, so that it stays consistent across conversations.
- **Feature:** Memory — Saved memories (explicit) + Extracted memories (implicit, 30-day, weekly-regenerated); 5 categories; manageable in Settings.

**E2-27.** As a **KW**, I want my chat history saved and searchable, with control over retention and privacy, so that I can revisit past work and manage my data.
- **Feature:** Chat History (admin retention 30/90/180/365 days; per-user privacy controls; searchable thread titles).

**E2-28.** As a **KW**, I want to disable memory for a specific sensitive chat, so that I control what the Assistant retains.
- **Feature:** "Don't use memory in this chat."

## 2.9 Deep Research & Canvas

**E2-29.** As a **KW**, I want to launch a Deep Research task that produces a multi-page, citation-rich report by synthesizing internal systems and the web, so that I get decision-ready analysis without doing the legwork.
- **Feature:** Deep Research — multi-agent (lead + parallel sub-agents); 5–10 page reports; clarifying questions; 5–30 min; 100+ systems + web.

**E2-30.** As a **KW**, I want a co-authoring Canvas next to chat to draft, refine, and export documents, slides, emails, spreadsheets, and interactive artifacts, so that I can iterate on a deliverable with AI assistance.
- **Feature:** Canvas — direct/conversational/targeted/queued editing, version history, sources, export to Google/Microsoft/Slack/Teams/CSV.

**E2-31.** As a **KW**, I want document suggestions and search results to appear live beneath the chat bar as I type, so that relevant files surface before I even finish my question.
- **Feature:** Search in Chat + autocomplete (permission-aware).

---

# EPIC 3 — Glean Agents, Apps & Builder

## 3.1 Agent Fundamentals

**E3-1.** As a **BLD**, I want to build deterministic workflow agents that follow the exact same steps every time, so that rules-based processes run reliably and predictably.
- **Feature:** Workflow agents (visual builder, strict determinism, branching, looping).

**E3-2.** As a **BLD**, I want to build autonomous agents that interpret plain-language instructions and choose their own approach, so that open-ended tasks (research, analysis, drafting) get solved without me scripting every step.
- **Feature:** Autonomous (Auto-mode) agents powered by the agentic engine.

**E3-3.** As a **BLD**, I want agents that plan adaptively, reflect, call sub-agents, and ask clarifying questions, so that they can complete genuinely complex, multi-step work.
- **Feature:** Agentic Reasoning Engine / Agentic Engine 2 — adaptive planning, reflection loop, sub-agent supervision, LLM judges, session-isolated sandbox (filesystem + code runtime).

## 3.2 Agent Builder (No-Code / Low-Code)

**E3-4.** As a **BLD**, I want to create an agent just by describing what I want in natural language, so that I can build automation without technical skills.
- **Feature:** Agent Builder — natural-language creation; Builder Assistant copilot (clarifying questions, multi-dimensional edits).

**E3-5.** As a **BLD**, I want to start from a prebuilt template, so that I don't have to design common agents from scratch.
- **Feature:** Agent templates (General, Engineering, HR, IT, Marketing, Sales, Support).

**E3-6.** As a **BLD**, I want a drag-and-drop visual canvas with steps, decision nodes, branching, and loops, so that I can shape precise multi-step logic.
- **Feature:** Visual workflow builder; "agentic looping"; Skills (reusable instructions) and sub-agents.

**E3-7.** As a **BLD**, I want to connect agents to specific knowledge sources and grant app-level tool access, so that each agent has exactly the context and capabilities it needs.
- **Feature:** Resources (docs/folders/Collections) + Tools/Actions (app-level grants).

**E3-8.** As a **BLD**, I want to pick a model and creativity (temperature) per agent — even per step — so that I can tune behavior (factual for legal, creative for marketing).
- **Feature:** Per-agent and per-step model selection + factual/balanced/creative presets.

**E3-9.** As a **BLD**, I want to preview and debug an agent with realistic scenarios and see its execution trace, tool calls, and context usage, so that I can validate it before deploying.
- **Feature:** Preview + Debug (run traces, step-by-step visibility).

**E3-10.** As a **BLD**, I want automatic versioning with drafts and rollback, so that I can iterate without breaking a live agent.
- **Feature:** Versioning, drafts, controlled deployment.

## 3.3 Glean Apps

**E3-11.** As a **BLD**, I want to build a scoped "AI topic expert" app constrained to chosen knowledge sources, so that (e.g.) an HR-policy bot only answers from HR content.
- **Feature:** Glean Apps — no-code custom assistants/copilots/chatbots scoped to specific sources.

**E3-12.** As a **BLD**, I want to publish my app to a company App Library, so that other employees can discover and use it.
- **Feature:** Glean App Library.

## 3.4 Triggers, Scheduling & Orchestration

**E3-13.** As a **BLD**, I want agents to run on a schedule (e.g., a daily summary), so that recurring work happens automatically.
- **Feature:** Scheduled-run triggers.

**E3-14.** As a **BLD**, I want agents to run automatically when content changes (a new Gong call, a Jira update, a new email), so that work is triggered by real events.
- **Feature:** Content/event-based triggers (Jira, Salesforce, Gong, Gmail, Google Calendar).

**E3-15.** As a **BLD**, I want the Assistant to route a question to the right specialist agent automatically, so that users always reach the best agent without picking one.
- **Feature:** Query-based triggers + intelligent routing (up to 15 conversational agents; routing rules in builder).

**E3-16.** As a **BLD**, I want multiple agents to coordinate, pass context, and activate in sequence across systems, so that complex multi-step processes complete end-to-end.
- **Feature:** Agent Orchestration — multi-agent coordination, context sharing, agents-as-tools, bidirectional A2A.

## 3.5 Actions & Tools (Taking Action)

**E3-17.** As a **KW**, I want agents to take real actions in other systems — create a Jira ticket, update a Salesforce opportunity, send a Slack message, run a Snowflake query — so that Glean does the work, not just finds information.
- **Feature:** Actions (100+ native) — Read (retrieval) and Write (execution) actions; "read → reason → write" pattern; Execution vs. Redirect.

**E3-18.** As a **DEV**, I want to define a custom action from an OpenAPI-style spec (endpoint, parameters, auth, triggers), so that agents can call our internal systems.
- **Feature:** Custom Actions (YAML/JSON spec, typed params, auth setup, example queries, built-in testing).

**E3-19.** As a **KW**, I want to approve write actions before they execute (or auto-allow trusted ones), so that I stay in control of changes to real systems.
- **Feature:** Tool permissions — "Always allow" vs. "Needs approval"; human-in-the-loop confirmation.

**E3-20.** As a **SEC**, I want actions to inherit the user's existing permissions and authentication, so that an agent can never do something the user couldn't do themselves.
- **Feature:** Action permission inheritance + admin controls over which apps/actions are enabled.

## 3.6 Agent Library & Prebuilt Agents

**E3-21.** As a **KW**, I want a library of prebuilt, ready-to-use agents (30+ Quickstart agents), so that I get value immediately without building anything.
- **Feature:** Agent Library — discover, deploy, share, certify; in-app agent suggestions; Deep Research, Structured Query (Snowflake Cortex), meeting-prep, code-review agents, etc.

**E3-22.** As a **BLD**, I want to customize any prebuilt agent in the builder, so that I can adapt it to my team's exact process.
- **Feature:** Prebuilt agents as editable templates.

## 3.7 MCP Gateway & Interoperability

**E3-22a.** As a **BLD**, I want to "vibe code" an agent by describing it conversationally and having the builder generate the full workflow — then refine each step interactively — so that complex agents are built in minutes through dialogue, not drag-and-drop.
- **Feature:** Conversational Agent Builder ("vibe coding") — describe → generate → refine conversationally; manual override available at every step.

**E3-23.** As a **DEV**, I want Glean to act as an MCP host so users can invoke third-party tools (Notion, Asana, Linear, Box, Atlassian) from inside Glean, so that external tools are available in one governed place.
- **Feature:** Glean as MCP host (remote MCP servers; centralized admin toggles; permission-enforced).

**E3-23a.** As a **DEV**, I want a centralized MCP Gateway that manages all MCP tool connections with permission enforcement, token optimization, and observability, so that MCP tool usage is governed and efficient at scale.
- **Feature:** MCP Gateway — centralized tool management, permission-aware routing, ~30% token reduction vs. off-the-shelf MCP tools.

**E3-24.** As a **DEV**, I want Glean to expose a hosted MCP server so external agents and IDEs (Claude Code, Cursor, ChatGPT, VS Code, JetBrains, Copilot) can securely use Glean Search/Assistant, so that our enterprise context powers any AI tool.
- **Feature:** Glean as MCP server (remote/local; 20+ connected hosts; centralized OAuth; official Claude Code & Cursor plugins).

**E3-25.** As a **BLD**, I want to expose a (read-only) Glean agent as an MCP tool to other hosts, so that other AI systems can call our agents.
- **Feature:** Agents-as-tools (no write actions / no human-in-the-loop steps allowed when exposed; ~40 tools/server max).

## 3.8 Developer Platform

**E3-26.** As a **DEV**, I want REST Client APIs (Search, Chat, Agents, Documents, Collections, Insights, Governance), so that I can embed Glean's capabilities into our own applications.
- **Feature:** Client API (OpenAPI spec).

**E3-27.** As a **DEV**, I want an Indexing (Push) API to bring custom/behind-the-firewall data into Glean with document-level permissions, so that internal sources become searchable alongside native connectors.
- **Feature:** Indexing/Push API (`/indexuser`, `/indexgroup`, `/indexmembership`, `/checkdocumentaccess`; permission fields like `allowedUsers`, `allowedGroups`).

**E3-28.** As a **DEV**, I want to attach structured metadata to documents without re-indexing, so that I can enrich content efficiently.
- **Feature:** Custom Metadata API (GA).

**E3-29.** As a **DEV**, I want official SDKs (Python, TypeScript, Java, Go) plus a Web SDK to embed Glean's UI, so that I can build in my language and surface Glean in our apps.
- **Feature:** SDKs + Web SDK + langchain-glean.

**E3-30.** As a **DEV**, I want an Agent Toolkit that exposes Glean tools (`glean_search`, `employee_search`, `code_search`, etc.) across frameworks (OpenAI, LangChain, CrewAI, Google ADK) and lets me define a tool once with `@tool_spec`, so that I can build agents in code and reuse tools anywhere.
- **Feature:** Glean Agent Toolkit (Agents API follows LangChain Agent Protocol).

**E3-31.** As a **DEV**, I want to build custom connectors on a connector framework, so that I can ingest sources Glean doesn't natively support and share that index across all agents.
- **Feature:** Custom connectors (`BaseDatasourceConnector.transform()`; reference repos).

## 3.9 Agent Governance & Observability

**E3-32.** As an **ADM**, I want step-by-step observability and evaluation of agent behavior, so that I can diagnose, measure quality, and improve agents.
- **Feature:** Agent Observability & Evals (run traces, LLM judges, upvote/downvote feedback, ROI metrics).

**E3-33.** As a **SEC**, I want guardrails that constrain agents to defined data, operations, and destinations, block prompt injection, and pause agents when scope changes, so that autonomous agents stay safe.
- **Feature:** Glean Protect agent safeguards (build/data-access/action-execution phases; agent alignment models in beta).

**E3-34.** As an **ADM**, I want to control who can build, view, edit, and run each agent, and certify/moderate agents in the Library, so that agent sprawl stays governed.
- **Feature:** Agent Governance / Guardrails + Library certification & moderation.

---

# EPIC 4 — Connectors, Integrations & Indexing

**E4-1.** As an **ADM**, I want 275+ out-of-the-box connectors spanning storage, messaging, project, code, CRM, support, HR, and design tools, so that I can connect our whole stack quickly.
- **Feature:** Connector catalog (native, push/indexing, partner, custom, MCP, web-history types).

**E4-2.** As an **ADM**, I want each connector to sync content, identities/permissions, and activity signals, so that search is both complete and correctly access-controlled.
- **Feature:** Per-connector content + identity + activity crawls; mirrored ACL snapshots.

**E4-3.** As a **KW**, I want a private Web History connector that indexes my own browsing history just for me, so that I can quickly re-find pages I've visited without exposing them to others.
- **Feature:** Web History connector (results private to the individual).

**E4-4.** As a **DEV**, I want connectors to be deployable as Glean-managed containers or as my own push jobs, so that I can choose the integration model that fits our security posture.
- **Feature:** Managed containerized vs. self-pushed custom connectors.

---

# EPIC 5 — Security, Governance & Admin

## 5.1 Permissions-Aware AI

**E5-1.** As a **SEC**, I want Glean to enforce source-system permissions at the moment of query (retrieval-time, least-privilege), so that no AI feature ever exposes content a user shouldn't see.
- **Feature:** Permissions-aware AI across search, chat, and agents; ABAC + ACL-mirrored index; real-time updates.
- **AC:** Removing a user from a Salesforce account team or marking a Jira project confidential removes that content from their Glean results at next sync (minutes).

## 5.2 Security & Compliance

**E5-2.** As a **SEC**, I want SOC 2 Type II, ISO 27001, ISO 42001 (AI management), HIPAA, GDPR, and CCPA compliance, so that Glean meets our regulatory obligations.
- **Feature:** Certifications + Trust Center (trust.glean.com).

**E5-3.** As a **SEC**, I want AES-256 encryption at rest (FIPS 140-2 module) and TLS 1.2+ in transit, with optional customer-managed keys, so that data is protected to our standard.
- **Feature:** Encryption + CMEK (in customer-hosted deployments).

**E5-4.** As a **SEC**, I want a single-tenant architecture isolated from every other customer, so that our data is never co-mingled.
- **Feature:** Single-tenant deployment (incl. single-tenant connectors).

**E5-5.** As a **SEC**, I want the option to host Glean in our own AWS/Azure/GCP account (cloud-prem) so data never leaves our cloud, so that we retain full control.
- **Feature:** Glean-Hosted vs. Customer-Hosted (cloud-prem); private VPC connectivity.

**E5-6.** As a **SEC**, I want to choose data residency (AMER/EMEA/APAC), so that we satisfy data-sovereignty requirements.
- **Feature:** Regional deployment.

**E5-7.** As a **SEC**, I want guarantees that customer data is never stored or used to train models, so that our IP stays ours.
- **Feature:** Zero-retention agreements with model providers.

## 5.3 Glean Protect / Protect+ & AI Guardrails

**E5-8.** As a **SEC**, I want continuous sensitive-content scanning across 100+ sources with severity classifiers for secrets, PCI, PII, and medical data, so that sensitive content is detected and kept out of AI surfaces.
- **Feature:** Glean Protect+ (continuous scanning, dashboards/API, automated remediation).

**E5-9.** As a **SEC**, I want AI guardrails that block prompt injection, malicious code, toxic content, and restricted topics (e.g., compensation, financial advice), so that AI outputs stay safe and compliant.
- **Feature:** AI security guardrails + Restricted Topics Policies + agent alignment models (beta).

**E5-10.** As a **SEC**, I want a governance framework for agentic security (actor intent, work context, autonomous guardrails, real-time risk scoring, ecosystem observability), so that I can govern autonomous agents end-to-end.
- **Feature:** AWARE framework + partner ecosystem (Palo Alto Prisma AIRS, BigID, Cisco, Rubrik, Tines, SIEM/SOAR).

## 5.4 Identity & Access

**E5-11.** As an **ADM**, I want SSO via OIDC/SAML with Okta, Entra ID, Google, OneLogin, JumpCloud, so that employees sign in securely with our IdP.
- **Feature:** SSO (OIDC/SAML 2.0).

**E5-12.** As an **ADM**, I want SCIM-based deprovisioning so removed users are immediately logged out, plus RBAC roles (Super Admin, Sensitive Content Moderator, etc.), so that access stays tightly controlled.
- **Feature:** SCIM 2.0 + RBAC user roles.

## 5.5 Admin Console & Knowledge Management

**E5-13.** As an **ADM**, I want a central admin console covering identity, search/content, assistant, agents, actions, governance, analytics, and knowledge, so that I can manage the whole platform in one place.
- **Feature:** Admin Console.

**E5-14.** As an **ADM**, I want natural-language admin help (Admin Chat) and NL analytics (Insights Chat), so that I can manage and report on Glean by just asking.
- **Feature:** Admin Chat (grounded in docs + community) + Insights Chat (NL → SQL/Python → charts/reports).

**E5-15.** As a **KM**, I want verification, deprecation, Answers, Collections, Go Links, and Announcements, so that I can curate and keep company knowledge trustworthy and discoverable.
- **Feature:** Knowledge-management suite.

## 5.6 Analytics, Audit & ROI

**E5-16.** As an **ADM**, I want adoption and usage dashboards (MAU, power users by department, connector usage, feedback, top agents), so that I can measure and drive adoption and prove ROI.
- **Feature:** Insights (Overview, Assistant Insights, Agent Insights, MCP analytics) + Insights API.

**E5-17.** As a **KM**, I want search-satisfaction (SSAT) metrics that reveal content/knowledge gaps by team, so that I can fix documentation where it's failing users.
- **Feature:** SSAT + knowledge-gap reporting.

**E5-18.** As a **SEC**, I want audit logs of every query, response, and document-access path, plus streaming to our SIEM/SOAR, so that I have full traceability for security and compliance.
- **Feature:** Admin Audit Logs + Customer Event Logs + SIEM/SOAR streaming (Splunk, Datadog, Elastic); customer-defined retention.

---

# EPIC 6 — Departmental & Role-Based Use Cases

> These map Glean's primitives (Search, Assistant, Agents, Actions) onto real workflows. Each department has named prebuilt agents and verbatim example prompts.

## 6.1 Engineering

**E6-1.** As an **ENG**, I want to ask how a system is implemented and get explanations grounded in our code, design docs, PRs, and discussions, so that I can ramp on unfamiliar areas without interrupting teammates.
- **Feature:** Code Understanding & Onboarding; Code Search.

**E6-2.** As an **ENG**, I want to investigate an issue by pulling together related tickets, incidents, code changes, logs, and prior discussions, so that I get from symptom to root cause faster.
- **Feature:** Issue Investigation & Debugging.

**E6-3.** As an **ENG**, I want to resolve production incidents in Slack with full debugging context, so that I fix escalations without tab-switching across systems.
- **Feature:** Production Incident Response.

**E6-4.** As an **ENG**, I want to `@glean` inside GitHub Copilot Chat/VS Code to query internal knowledge while coding, so that I stay in my flow.
- **Feature:** Glean in GitHub / IDE; MCP endpoint + agent toolkit for Cursor/Claude Code/Copilot/Codex.

**E6-5.** As an **ENG**, I want auto-generated PR descriptions and automated PR review against our standards, so that code review is faster and more consistent.
- **Feature:** AI PR descriptions + PR Review Automation (beta).

**E6-6.** As an **ENG**, I want an agent to turn a spec or Jira ticket into a review-ready draft PR, so that routine implementation is accelerated.
- **Feature:** "Spec to Implementation PR" / "Resolve Jira Ticket" / Code Writer agents.

**E6-7.** As an **ENG**, I want agents for standups, self-evaluations, release docs, and project onboarding, so that recurring engineering admin is automated.
- **Feature:** Engineering Standup, Self-Evaluation, Launch Documentation, Project Onboarding agents.

## 6.2 Sales

**E6-8.** As a **SALES**, I want an account snapshot unifying open opportunities, recent support tickets, Slack threads, emails, and product usage with red flags highlighted, so that I walk into every account conversation fully prepared.
- **Feature:** Account Snapshot agent (Salesforce Actions + Zendesk + Slack + email + usage).

**E6-9.** As a **SALES**, I want an AI-generated meeting/deal-prep doc before each call, so that I show up as an expert with the right context and assets.
- **Feature:** Deal/meeting prep; Deal Strategy agent.

**E6-10.** As a **SALES**, I want a one-page competitive brief and counter-strategies pulled from battlecards, Gong calls, and win/loss notes, so that I win competitive deals.
- **Feature:** Competitive Brief agent.

**E6-11.** As a **SALES**, I want personalized prospecting emails grounded in trusted research, so that my outreach converts better.
- **Feature:** Prospect Outreach Email agent.

**E6-12.** As a **SALES**, I want to draft RFP/proposal responses by reusing past proposals, specs, references, and compliance certs, so that I respond faster and more accurately.
- **Feature:** RFP response workflow.

**E6-13.** As a **SALES**, I want closed-lost analysis and a smooth account handoff to CS, so that we learn from losses and onboard customers cleanly.
- **Feature:** Deal Loss Insights + Account Handoff agents.

## 6.3 Customer Support / Service

**E6-14.** As a **SUP**, I want recommended next steps and polished draft replies grounded in our policies and prior resolutions, surfaced inside Zendesk/ServiceNow, so that I resolve tickets faster on first contact.
- **Feature:** Glean in Zendesk/ServiceNow; Support Ticket Next Steps + Support Follow-Up Email agents.

**E6-15.** As a **SUP**, I want a full timeline of every interaction on a ticket and a real-time customer sentiment score, so that I understand context and health instantly.
- **Feature:** Detailed Support Ticket Timeline + Customer Sentiment Score agents.

**E6-16.** As a **SUP** lead, I want to spot recurring questions and root causes across Zendesk/Jira/Gong/Slack, so that we prevent repeat tickets and improve docs.
- **Feature:** Issue-mix insight; Deep Research support pattern.

## 6.4 IT / ITSM

**E6-17.** As an **IT** owner, I want Glean to deflect tickets by giving employees self-service answers (password resets, VPN, software installs) before a ticket is filed, so that L1 volume drops.
- **Feature:** "Shift Left" deflection across Glean, Slack, Teams, ServiceNow, Jira.

**E6-18.** As an **IT** agent, I want an IT Help Desk agent that troubleshoots, files tickets, and grants access for me, so that I resolve issues faster.
- **Feature:** IT Help Desk agent (accounts/access, devices, software, network, provisioning).

**E6-19.** As an **IT** owner, I want to turn resolved tickets into publish-ready help docs and spot weak documentation, so that the knowledge base continuously improves.
- **Feature:** Support Documentation from Ticket agent; ITSM iteration insights.

## 6.5 HR / People

**E6-20.** As a **NH**, I want instant access to policies, acronyms, teammates, projects, and past decisions, plus an auto-generated onboarding guide, so that I contribute sooner.
- **Feature:** Onboarding & ramp.

**E6-21.** As a **KW**, I want an HR agent that answers benefits, PTO/leave, payroll, and policy questions (and initiates HR workflows), so that I get help without waiting on HR.
- **Feature:** HR agent with auto-routing + ServiceNow HR Case Management.

**E6-22.** As an **HR** partner, I want to draft job descriptions, interview questions, candidate summaries, surveys, and constructive feedback, so that I produce people-content faster.
- **Feature:** HR Prompt Library.

## 6.6 Marketing

**E6-23.** As a **MKT**, I want to create on-brand content across channels with built-in context from past work, so that messaging stays consistent and I move faster.
- **Feature:** Content creation; LinkedIn Post Draft, Marketing Event Description agents.

**E6-24.** As a **MKT**, I want competitive research (SWOT, recent competitor launches) via Deep Research, so that I can craft differentiation strategies.
- **Feature:** Deep Research marketing pattern.

**E6-25.** As a **MKT**, I want agents for customer testimonials from calls, SEO article audits, persona-based event messaging, and keyword research from sales calls, so that repeatable marketing work is automated.
- **Feature:** Customer Testimonials from Calls, SEO Article Evaluation, Persona-Based Event Messaging, SEO Keyword Research agents.

## 6.7 Product Management

**E6-26.** As a **PM**, I want to draft a PRD that pulls persona pain points from research, jobs-to-be-done from sales calls, constraints from engineering Slack, and patterns from past PRDs, so that I start from synthesized evidence.
- **Feature:** PRD Generation (MCP for PM).

**E6-27.** As a **PM**, I want to compile and prioritize feature requests across Zendesk, Slack, Gong, and Jira, so that I build what customers actually need.
- **Feature:** Feature Request Analysis / Feature Prioritization agent.

**E6-28.** As a **PM**, I want agents for sprint planning, competitor analysis, activation/adoption analysis, launch updates, and usage metrics, so that I stay on top of the product lifecycle.
- **Feature:** Six PM agents (Sprint Planning, Competitor Analysis, Activation & Adoption, Launch Update, Usage Metrics, Feature Prioritization).

## 6.8 Legal / Compliance

**E6-29.** As **LEGAL** counsel, I want to review and redline contracts faster by surfacing relevant precedents and fallback language, so that I speed up review without losing rigor.
- **Feature:** Contract review/redlining.

**E6-30.** As **LEGAL** counsel, I want to draft grounded responses to questionnaires using approved answers and source-linked docs, so that repeat work is consistent and fast.
- **Feature:** Questionnaire response.

**E6-31.** As a **KW**, I want permission-aware, cited self-service answers to common legal questions, so that I don't flood the legal inbox.
- **Feature:** Legal self-service.

## 6.9 Finance / Operations

**E6-32.** As a **FIN** analyst, I want to forecast revenue by combining account health, contract terms, seller signals, and pipeline movement, so that I flag churn/downsell risk earlier.
- **Feature:** Revenue forecasting.

**E6-33.** As a **FIN** analyst, I want to detect working-capital pressure and spend issues (unpaid invoices, open POs, duplicates) before reviews, so that I avoid surprises.
- **Feature:** Working-capital + spend analysis.

**E6-34.** As a **DATA**/FIN user, I want to query Snowflake data in natural language, so that I get insights without writing SQL.
- **Feature:** Structured Query Agent for Snowflake Cortex Analyst.

## 6.10 Executive / General Knowledge Worker

**E6-35.** As a **KW**, I want personal-productivity agents (Plan my day, Meeting Recap, Intelligent Reminders, Delegation Tracker, Ghostwriter, Weekly work report), so that I stay organized and on top of my commitments.
- **Feature:** General/personal-productivity agents.

**E6-36.** As an **EXEC**, I want decision-ready, citation-rich research synthesized across internal systems and the web, so that every decision is better-informed.
- **Feature:** Deep Research agent.

**E6-37.** As a **KW**, I want to find any answer, draft, or next step from company context regardless of which tool it lives in, so that I stop wasting time on "knowledge scavenger hunts."
- **Feature:** Knowledge management / self-service (the core horizontal value).

## 6.11 Industry Solutions

**E6-38.** As a **FIN-services** employee (banking, PE/VC, asset mgmt, insurance), I want decision-ready outputs grounded in firm knowledge with regulated-workflow governance, so that I move faster while staying compliant.
- **Feature:** Financial Services industry solution.

**E6-39.** As a **healthcare** worker (provider/payer/life-sciences), I want instant access to payer policies, prior authorizations, SOPs, and protocols with HIPAA-grade permissions, so that I handle claims, support, onboarding, and compliance accurately.
- **Feature:** Healthcare industry solution.

**E6-40.** As an employee in **retail, higher-ed, government, industrials, or professional services**, I want Glean's horizontal search/self-service/onboarding/agents tuned to my industry, so that my team gets the same productivity gains.
- **Feature:** Additional industry solutions.

---

# Appendix A — Cross-Cutting Acceptance Criteria

Patterns Glean applies as near-universal acceptance criteria across features:

1. **Permission-aware:** every result, answer, and action respects the user's existing source-system permissions, enforced at retrieval time.
2. **Cited & grounded:** answers grounded in company content carry clickable citations to exact source passages.
3. **Personalized:** outputs reflect the user's role, team, projects, and history (Personal Graph).
4. **Multi-surface:** the capability behaves consistently across web, browser extension, Slack, Teams, and desktop.
5. **Governed:** admins can control availability, models, actions, and data exposure; everything is audited.
6. **No-training guarantee:** customer data is never used to train third-party models.

# Appendix B — The Dominant Workflow Shape

Most departmental use cases reduce to one repeatable pattern, useful when writing new stories:

> **Unify N fragmented sources → produce a cited deliverable (draft reply / prep doc / PR / summary / report) → optionally take an action (file ticket, update CRM, open PR) — all permission-aware and on the surface where the user already works.**

# Appendix C — Maturity / Caveats (as of mid-2026)

- **Beta features** to flag in stories: PR Review Automation, agent alignment models, remote MCP servers, some Protect+ capabilities, conversational agent builder, agent looping.
- **Deployment dependencies:** Memory and image generation are GCP-only; deep-linked citations require GPT/Claude (not Gemini); the agentic engine requires GPT-5 or Claude Sonnet; Deep Research and per-chat model choice are web-first.
- **Connector counts:** "100+" = native/scanned set; "275+" = full catalog (native + MCP + push/partner).
- **Pricing** is quote-based and enterprise-sold (no public list); third-party estimates only.

# Appendix D — Key Metrics (from Glean's public claims, mid-2026)

- **35+** unique LLM models supported (and growing)
- **30%** reduction in token usage vs. off-the-shelf MCP tools (via MCP Gateway)
- **110 hours** saved per user per year
- **20%** fewer internal support tickets
- **93%** enterprise adoption in < 2 years
- **< 6 months** to ROI
- **141%** ROI in 3 years (Forrester Total Economic Impact study)
- **1.5+ hours** saved weekly per user (Zillow case study)
- **2–3 hours** saved weekly per user (GCash case study)
- **3,400+** agents built at Zillow
- **2,700+** agents built at Ericsson
- **14,000** employees at Booking.com (first AI platform adopted company-wide)
- **500K+** monthly survey responses at Booking.com
- **100+** years of archives centralized at TIME
- **20,000+** employees trained on AI at Ericsson
