# Knowledge Work Automation — Competitive Research & Expanded User Stories

> **Consolidated:** This file is now **source material**. The canonical, comprehensive write-ups live in `docs/product/`:
> - Research (vendor names retained) → [docs/product/knowledge-work-automation-research.md](docs/product/knowledge-work-automation-research.md)
> - User stories (vendor-neutral, generator-parseable) → [docs/product/user-stories.md](docs/product/user-stories.md)
>
> Companion to [glean-user-stories.md](glean-user-stories.md). This document widens the lens from Glean alone to the broader **knowledge-work-automation** category, then adds new user stories (weighted toward *automation*: agents that take multi-step action, run unattended, and complete work end-to-end).
> Compiled June 2026 from primary product docs, engineering blogs, press, and reputable third-party coverage across ~22 products.

---

## 1. Why this exists

The original doc catalogs **what Glean does**. The user asked to research **similar companies** and fold the findings into the stories, **focusing more on knowledge work automation**. So this companion:

1. Maps the competitive landscape (Section 2).
2. Adds **Epics 7–15** of capability-level user stories synthesized across the whole field, each tagged with the vendors that exemplify it (Sections 4+).
3. Notes where competitors **extend or update** the existing Glean epics (Section 5).

Stories here are written **product-agnostically** — they describe capabilities a best-in-class knowledge-work-automation platform should have — with an **Exemplars** tag showing who does it today. That makes this usable both as a competitive feature map and as a product backlog.

---

## 2. Competitive landscape (the field researched)

| Product | Category | KW-automation signature | Most unique differentiator |
|---|---|---|---|
| **Glean** | Work AI platform | Search + Assistant + Agents; Agent Library | Enterprise Graph + horizontal, permissions-aware |
| **Microsoft 365 Copilot + Copilot Studio** | Productivity-suite AI | **Agent Mode** in Word/Excel/PPT; autonomous agents | Microsoft Graph grounding; **Agent 365** fleet governance; 1,400+ connectors |
| **Google Gemini Enterprise** (ex-Agentspace) | "Front door for enterprise AI" | **Workspace Flows**; Deep Research & Idea agents | **A2A protocol**; NotebookLM Enterprise |
| **Amazon Q Business / Quick Suite** | AWS Work AI | **Q Apps**; Quick Automate; Quick Research | **Q index** shared to ISVs; AgentCore (browser, gateway) |
| **Dropbox Dash** | Universal-search Work AI | Search + content governance; MCP context layer | Document-level access governance depth |
| **Atlassian Rovo** | Enterprise search + agents | **Rovo Studio**, **Max Mode**, **Rovo Dev** | Teamwork Graph; bundled into Atlassian plans |
| **Dust** | "Multiplayer OS for enterprise AI" | No-code agents, **Skills**, **Triggers** | Multiplayer shared agents; non-engineers as builders |
| **Moveworks** (ServiceNow) | Employee-support agentic AI | **Agentic Automation** (iPaaS replacement); 1000+ agents | Slack/Teams self-service depth (6M+ employees); **acquired by ServiceNow** |
| **Aisera** (Automation Anywhere) | Agentic AI service experience | **Universal Bot**; 64–84% auto-resolution | **LLM Gateway** (model choice + cost cuts); AIOps |
| **ServiceNow** (Now Assist + AI Agents) | Workflow agentic platform | **AI Agent Studio / Orchestrator**; autonomous change agents | System of *action*; **AI Control Tower**; RaptorDB |
| **Salesforce Agentforce** | "Digital labor" on CRM | **Atlas Reasoning** + **Agent Script**; SDR/Service agents | Slack as "agentic OS"; Agent Script determinism |
| **Writer** | Full-stack enterprise AI | **Action Agent**; **Playbooks/Routines**; event triggers | Self-built **Palmyra** models + **graph-based RAG** |
| **Sana AI** (Workday) | Agentic knowledge assistant | Find · Act · Build · Automate; meetings-as-knowledge | Meeting + learning heritage; Workday orchestration |
| **Cohere North** | Secure agentic workspace | No-code agents + workflow automations | **Sovereign/air-gapped** deploy (2 GPUs); own models |
| **Guru** | Governed knowledge layer | Knowledge Agents; auto-verify; deep research | **Verification + Trust Score + Knowledge Triggers**; Knowledge Agents that *improve* info, not just find it |
| **Notion AI** | Workspace-native AI | **Agents that build/edit pages & DBs**; Custom Agents | Agents act *inside the system of record*; credit-based pricing for Custom Agents |
| **OpenAI ChatGPT Enterprise** | Frontier assistant | **ChatGPT agent**, Deep Research, **AgentKit** | All-in-one research+act agent (browser+terminal) |
| **Anthropic Claude Enterprise** | Frontier assistant | **Claude Code**, **Cowork**, **Agent Skills** | **MCP + Agent Skills** open standards; 500K context |
| **Perplexity Enterprise** | Answer engine | **Labs** (build apps/dashboards); **Comet** browser | Agentic browser; always-cited internal+web |
| **Hebbia** | AI for knowledge work | **Matrix** grid over thousands of docs (**ISD**) | Exhaustive document reasoning + sentence citations |
| **AlphaSense** | Market intelligence | **Deep Research**, **Generative Grid** | Premium licensed corpus + expert-call transcripts |
| **IBM watsonx Orchestrate** | Agentic process automation | **Agent Builder + Catalog**; multi-agent supervisor | "Any agent, any framework" + watsonx.governance |

**Three category-wide shifts (2024→2026), all toward automation:**
1. **From answer → act.** Every serious player shipped an agent that *takes multi-step action*, not just chat (ChatGPT agent, Claude Cowork, Comet, Copilot Agent Mode, Agentforce, Glean Agents).
2. **From prompted → autonomous.** Agents now run on **schedules and event triggers** with **write-back** to systems (Dust Triggers, Writer Routines/Event Triggers, Notion Custom Agents, Copilot autonomous agents).
3. **From single agent → governed fleets.** **Control planes, per-agent identity, runtime guardrails, and observability** became table stakes (Agent 365, AI Control Tower, Command Center, watsonx.governance, Model Armor, Glean Protect).
4. **From standalone → platform consolidation.** Acquisitions are accelerating: **ServiceNow acquired Moveworks**, **Workday acquired Sana AI**, **Automation Anywhere acquired Aisera** — signaling that agentic AI is being folded into larger platform plays rather than remaining independent.

---

## 3. Personas (delta from the Glean doc)

Reuse all personas from [glean-user-stories.md](glean-user-stories.md) §2 (KW, NH, ENG, SALES, SUP, IT, HR, MKT, PM, LEGAL, FIN, DATA, EXEC, BLD, DEV, ADM, SEC, KM). Add:

| Tag | Persona | Description |
|---|---|---|
| **OPS** | Operations / Process Owner | Owns a repeatable business process to automate (RevOps, FinOps, support ops) |
| **ANALYST** | Research Analyst | Finance / consulting / market-research; document-heavy deep analysis |
| **AGENTOPS** | AI Platform / AgentOps Owner | Runs and governs the fleet of agents in production |

**Story format (unchanged):** *As a [persona], I want to [capability], so that [benefit].* New stories carry an **Exemplars** tag (vendors shipping the capability) and **AC** on anchor stories.

---

# EPIC 7 — Autonomous & Event-Driven Agents

*The heart of knowledge-work automation: agents that initiate and complete work without a human typing each prompt.*

**E7-1.** As an **OPS** owner, I want to schedule an agent to run on a recurring cadence (e.g., a Monday 8am pipeline-risk report), so that routine deliverables are produced automatically without anyone remembering to run them.
- **Exemplars:** Dust Triggers (cron), Writer Routines, Notion Custom Agents, OpenAI Tasks, Copilot scheduled prompts, AlphaSense schedulable Workflow Agents, Glean scheduled agents.
- **AC:** A schedule (time/interval) can be set on any agent; the run executes unattended; output is delivered to a destination (email/Slack/doc) with sources/citations.

**E7-2.** As an **OPS** owner, I want an agent to trigger automatically when a real-world business signal fires — a new Gong call, an inbound email, a Jira status change, a file landing in a folder — so that work starts the moment the event happens.
- **Exemplars:** Writer Event-Based Triggers (Gmail/Gong/Calendar/Drive/SharePoint/Slack), Dust webhook triggers, Notion event-based Custom Agents, Copilot Studio autonomous triggers, Moveworks Ambient Agents, Glean content triggers.
- **AC:** Trigger sources include app/data-state changes; the agent receives the event payload as input; supports both event and schedule triggers.

**E7-3.** As an **OPS** owner, I want agents to write their results back into the systems of record (post to Slack, update a CRM field, create a Linear ticket), so that automation closes the loop instead of producing a dead-end summary.
- **Exemplars:** Dust write-back, Writer Connectors (read+write+trigger), Amazon Q plugins, Glean write Actions.

**E7-4.** As a **KW**, I want agents to keep working while I'm offline and hand me finished output when I return, so that long-running work happens overnight rather than blocking my day.
- **Exemplars:** Dust ("your agents, working while you sleep"), Notion Agents (run while offline), Writer Routines, ChatGPT agent long-running tasks.

**E7-5.** As a **SEC** owner, I want consequential agent actions (sending email, spending money, deleting data) to pause for human approval — ideally at the argument level (approve *these* recipients) — so that autonomy never crosses into unsafe territory.
- **Exemplars:** Copilot HITL, Dust graduated tool approval (low/medium/high; argument-level), Glean tool permissions (Always allow / Needs approval), ChatGPT agent confirmations, Comet sensitive-action approval.
- **AC:** Per-tool policy of auto-allow vs. require-approval; high-stakes tools can require explicit per-action confirmation; medium-stakes can require approval of specific arguments.

**E7-6.** As a **BLD**, I want a supervisor/orchestrator agent that decomposes a request, routes sub-tasks to specialist agents, and merges their results, so that complex work spans multiple expert agents automatically.
- **Exemplars:** ServiceNow AI Agent Orchestrator, Salesforce Atlas + Subagents, IBM agent supervisor, Glean Agent Orchestration, Dust "Run Agent" hierarchies, Sana A-4, Hebbia 7-agent system.

**E7-7.** As a **DEV**, I want agents built on different platforms/vendors to interoperate over open protocols, so that I'm not locked into one ecosystem and agents can call each other.
- **Exemplars:** Google **A2A**, ServiceNow **AI Agent Fabric** (A2A+MCP), IBM **Agent Connect**, Anthropic **MCP** (donated to Linux Foundation), Aisera **Unify** (A2A/MCP/AGNTCY), Glean MCP host/server + agents-as-tools.

**E7-8.** As an **OPS** owner, I want to automate ambiguous, natural-language multi-system processes without brittle integration scripts, so that AI agents replace fragile iPaaS/RPA flows.
- **Exemplars:** Moveworks **Agentic Automation** (explicit iPaaS replacement: Manifest Generator / Slot Resolvers / Policy Validators / Action Orchestrator), Amazon **Quick Automate**, MuleSoft Agent Fabric, Glean Actions.
- **AC:** Natural-language intent is translated into precise API calls across systems; business rules (policy validators) can block unauthorized actions mid-flow.

**E7-9.** As a **BLD**, I want to choose between a **deterministic** workflow (same steps every time) and an **autonomous** one (the agent picks the path), and mix them, so that I get reliability where I need it and flexibility where I don't.
- **Exemplars:** Salesforce **Agent Script** / hybrid reasoning, Glean Workflow vs. Auto mode, Copilot Studio agent flows vs. generative orchestration, Aisera Hyperflows.

**E7-10.** As an **AGENTOPS** owner, I want agents to detect when their scope changes and auto-pause for review, so that drifting or out-of-policy agents stop themselves.
- **Exemplars:** Glean Protect (auto-pause on scope change), ServiceNow guardrails, Writer Supervise.

---

# EPIC 8 — Computer Use, Browser & Desktop Automation

*Agents that operate software the way a person does — clicking, typing, navigating — when there's no clean API.*

**E8-1.** As an **OPS** owner, I want an agent to operate websites and apps through their UI (navigate, click, fill forms, extract data) when no API exists, so that I can automate the long tail of systems that integrations don't cover.
- **Exemplars:** OpenAI Operator / ChatGPT agent (visual browser), Anthropic computer use, Microsoft computer use (Copilot Studio + Researcher), Amazon **AgentCore Browser**, Writer Action Agent.
- **AC:** Runs in a secure/sandboxed browser; can read page state, click, type, and complete a multi-step task; asks for confirmation on consequential steps.

**E8-2.** As a **KW**, I want an agentic browser that understands the page I'm on and can run multi-step tasks across sites (research, compare, book, draft+send), so that the browser itself becomes my automation surface.
- **Exemplars:** Perplexity **Comet** (+ Comet Enterprise), Anthropic **Claude in Chrome**.

**E8-3.** As a **KW** (non-engineer), I want a desktop/general-computing agent — a "Claude Code for everyday work" — that automates file and app tasks on my machine, so that I can offload computer chores without writing code.
- **Exemplars:** Anthropic **Cowork** (GA Apr 2026; also powers Microsoft Copilot Cowork), Writer Action Agent.

**E8-4.** As a **BLD**, I want each agent to run in an isolated virtual computer with its own filesystem, shell, and code runtime, so that it can process data, run code, and hold working memory without blowing the LLM context window.
- **Exemplars:** Glean session-isolated sandbox, OpenAI ChatGPT agent terminal, Amazon **AgentCore Code Interpreter** (+ Runtime session isolation), Dust Run Code (client-side), Writer isolated VM, Hebbia.

**E8-5.** As an **OPS** owner, I want the agent to self-correct when a page won't load or an API errors (retry, recover, find another path), so that automations don't silently fail halfway.
- **Exemplars:** Writer Action Agent (explicit self-correction/recovery), ChatGPT agent, Aisera.

---

# EPIC 9 — Deep Research & Analysis Agents

*Automating the read → synthesize → draft loop that consumes analysts' days.*

**E9-1.** As an **ANALYST**, I want a deep-research agent that explores a topic across internal systems and the web and returns a multi-page, citation-rich report from a single prompt, so that hours of manual research compress to minutes.
- **Exemplars:** Glean Deep Research, Microsoft **Researcher**, Google **Deep Research agent**, Amazon **Quick Research**, OpenAI / Anthropic / Perplexity Deep Research, AlphaSense **Deep Research**, Hebbia "Deeper."
- **AC:** Plans the research, searches multiple sources, synthesizes, and emits a structured report with linked citations; may ask clarifying questions before starting.

**E9-2.** As an **ANALYST**, I want the research agent to spin up parallel specialist sub-agents (and "scouts") that divide the work, so that broad questions are answered faster and more completely.
- **Exemplars:** Glean (lead + parallel sub-agents + scouts), Hebbia 7-agent system (Orchestrator/Planning/Retrieval/Analysis/Distillation/Reasoning/Output), Anthropic multi-agent research.

**E9-3.** As a **DATA**/FIN analyst, I want a reasoning "analyst" agent that writes and runs code (Python) over messy multi-source data and shows its work, so that I get verifiable forecasts, charts, and analyses without a data scientist.
- **Exemplars:** Microsoft **Analyst** (o3-mini + Python), Amazon **Quick Sight** (agentic BI), OpenAI Advanced Data Analysis, Writer Action Agent.

**E9-4.** As an **ANALYST**, I want to run the same set of questions across **thousands of documents at once** in a spreadsheet-style grid, with every cell traceable to its source sentence, so that I can do exhaustive analysis (not just top-k retrieval) over a whole data room.
- **Exemplars:** Hebbia **Matrix** (Iterative Source Decomposition; exhaustive, sentence-level citations), AlphaSense **Generative Grid**.
- **AC:** Ingest thousands of files; ask N questions × M documents in a table; each answer hyperlinks to the exact source passage; reasoning runs over full documents without chunking limits.

**E9-5.** As an **ANALYST**, I want research to draw on premium/licensed content and expert-call transcripts alongside my internal docs and the web, so that the synthesis includes high-value sources a generic tool can't reach.
- **Exemplars:** AlphaSense (500M+ premium docs + Tegus expert calls + Enterprise Intelligence), Hebbia (FactSet/PitchBook/S&P Capital IQ integrations).

**E9-6.** As a **PM**/strategist, I want an agent that autonomously generates *and* evaluates novel ideas (tournament-style) for a problem, so that ideation is both expansive and pre-filtered.
- **Exemplars:** Google **Idea Generation agent** (scientific-method evaluation).

**E9-7.** As an **ANALYST**, I want the agent to ask clarifying questions before a big research run, so that it scopes the work correctly instead of wasting a long run on the wrong framing.
- **Exemplars:** Microsoft Researcher, Glean, Deep Research (broadly), Amazon Q Business Agentic RAG (disambiguation).

---

# EPIC 10 — In-App & Document-Native Automation

*Agents that don't just describe work — they produce the actual document, spreadsheet, deck, or database.*

**E10-1.** As a **KW**, I want an agent mode *inside* my documents that performs real multi-step edits in the app's object model (write formulas, build tables, restructure a deck, apply templates), so that I go from blank to polished without leaving the app.
- **Exemplars:** Microsoft **Agent Mode** in Word/Excel/PowerPoint (GA), PowerPoint Narrative Builder.
- **AC:** The agent plans→executes→validates changes directly in the file (real formulas/slides), not as a copy-paste suggestion.

**E10-2.** As a **KW**, I want an agent that autonomously creates and edits pages and databases across my workspace — even hundreds at once — so that structural busywork (reorganizing, templating, back-filling) is automated.
- **Exemplars:** Notion **Agents** (20+ min multi-step, create/edit hundreds of pages/DBs, memory), Sana **Build** (generate dashboards/docs).

**E10-3.** As a **KW**, I want to generate a complete deck, spreadsheet, or document from a prompt, on-brand and grounded in company context, so that first drafts of deliverables are produced instantly.
- **Exemplars:** Glean Canvas, Microsoft Office agents in chat, Hebbia **FlashDocs** (branded PowerPoint/Slides via API), AlphaSense **Slide Agent**, Writer.

**E10-4.** As a **KW**, I want to describe a multi-step process in plain language and have the platform build a logic-driven flow that researches, analyzes, drafts, and routes — no coding, with AI "steps" as reasoning units, so that I automate workflows without IT.
- **Exemplars:** Google **Workspace Flows** (Gems as steps), Writer Playbooks, Copilot agent flows.

**E10-5.** As a **KW**, I want a co-authoring canvas where AI output becomes a durable, editable, exportable artifact (doc/app/dashboard/chart) — not disposable chat text — so that I can iterate on the deliverable itself.
- **Exemplars:** Glean Canvas, OpenAI **Canvas**, Anthropic **Artifacts**, Perplexity **Labs** (reports/dashboards/web apps), Dust **Interactive Content**, Google Canvas, Microsoft **Copilot Pages** (multiplayer).

**E10-6.** As a **KW**, I want to turn a useful chat conversation into a reusable, shareable app at the click of a button, so that a one-off prompt chain becomes a tool my whole team can run.
- **Exemplars:** Amazon **Q Apps** (cards incl. action-taking plugin cards; org app library).

**E10-7.** As a **KW**, I want a multiplayer canvas where teammates and the AI co-edit in real time, so that "human-to-AI-to-human" collaboration replaces ephemeral solo chats.
- **Exemplars:** Microsoft **Copilot Pages**, Glean Canvas, Dust multiplayer (@mention humans into an agent thread).

---

# EPIC 11 — Reusable Building Blocks & No-Code Building

*The toolkit that lets non-engineers build, reuse, and scale automations.*

**E11-1.** As a **BLD** (non-technical), I want to build an agent by describing it in natural language (and refining conversationally), so that I don't need engineering skills to automate my own work.
- **Exemplars:** All — Glean Builder Assistant, Copilot Studio, Dust ("Instructions" not "system prompt"), Writer no-code, ServiceNow AI Agent Studio, Aisera Agent Composer, IBM AI Agent Builder ("agent in under 5 minutes"), Rovo Studio.

**E11-2.** As a **BLD**, I want to package reusable expertise as a **Skill** (instructions + knowledge + tools) that any agent can load and that updates propagate from, so that I maintain capabilities once instead of per-agent.
- **Exemplars:** Anthropic **Agent Skills** (open `SKILL.md` standard, portable across web/Code/API), Dust **Skills** (nested, versioned, propagating), Writer **Agent Skills** creator, Guru Skills, Glean Skills.
- **AC:** A skill is reusable across agents; updating it updates every agent using it; skills can be discovered/shared org-wide and inherit permissions.

**E11-3.** As a **KW**, I want to capture a recurring workflow as a natural-language **Playbook** and optionally schedule it as a **Routine**, so that complex knowledge work runs repeatably and on autopilot.
- **Exemplars:** Writer **Playbooks** + **Routines**, Google **Gems**, Glean advanced/multi-step prompts.

**E11-4.** As a **KW**, I want a marketplace/library of prebuilt agents and templates spanning every department, so that I can deploy proven automations instantly and customize them.
- **Exemplars:** Microsoft **Agent Store**, Salesforce **AgentExchange** (100+ industry actions), IBM **Agent Catalog** (150+ agents / 500+ tools), Moveworks **AI Agent Marketplace** (1000+ agents across business initiatives), Aisera, Glean Agent Library (30+), ServiceNow (thousands).

**E11-5.** As a **BLD**, I want a visual canvas with branching, looping, sub-agents, and decision nodes, so that I can express precise multi-step logic when I need determinism.
- **Exemplars:** Glean workflow builder, Writer **Agent Builder / Blueprint** blocks, Copilot Studio, Aisera **Hyperflow Studio**, Rovo Studio.

**E11-6.** As a **BLD**, I want to choose the model and creativity level per agent — even per step — so that I can optimize cost/quality for each part of a workflow.
- **Exemplars:** Glean (per-agent/per-step + factual/balanced/creative), Writer Agent Builder, Microsoft per-task model choice, Salesforce per-agent models.

**E11-7.** As a **BLD**, I want to preview, debug (step traces, tool calls, context usage), version, and roll back agents, so that I can iterate safely without breaking live automations.
- **Exemplars:** Glean Preview/Debug + versioning, Writer (logs/traces/execution paths), Copilot Studio, Rovo Studio (versioning/approvals/audit).

**E11-8.** As an **AGENTOPS** owner, I want automated evaluation of agent outputs (LLM judges, test sets, performance vs. real traffic), so that I can measure and improve reliability before and after deployment.
- **Exemplars:** Glean LLM judges, Amazon **AgentCore Evaluations**, IBM watsonx.governance evaluation nodes, OpenAI Evals, Writer observability.

**E11-9.** As a **DEV**, I want APIs, SDKs, and a code-first agent toolkit (define a tool once, reuse across frameworks) plus the ability to build custom connectors, so that engineers can extend the platform and bring custom data/tools.
- **Exemplars:** Glean Agent Toolkit (`@tool_spec`), Writer AI Studio (Applications API, Agent Builder blocks), OpenAI **AgentKit/Agents SDK**, Google **ADK**, IBM **ADK**, Amazon **AgentCore Gateway** (API→MCP tools), Dust Apps.

---

# EPIC 12 — Meeting & Communication Intelligence

*Turning conversations and meetings into searchable knowledge and automated follow-through.*

**E12-1.** As a **KW**, I want meetings captured, transcribed, and summarized automatically — ideally without a bot joining the call — so that I never lose decisions and action items.
- **Exemplars:** Notion **AI Meeting Notes** (captures system audio, no bot), Sana (record/transcribe/summarize), Microsoft **Facilitator**, Google "**take notes for me**."

**E12-2.** As a **KW**, I want every meeting indexed as a first-class, searchable knowledge asset, so that I can later ask questions across what was said in meetings just like any document.
- **Exemplars:** Sana (meetings indexed/retrievable/analyzable as knowledge), Glean calendar search.

**E12-3.** As a **KW**, I want action items, recaps, and follow-ups generated and routed to the right tool/owner automatically, so that meeting outcomes turn into tracked work.
- **Exemplars:** Microsoft Facilitator → Planner, Glean meeting/action-item agents, Sana, Daily Meeting Action Summary.

**E12-4.** As a **KW**, I want real-time, in-meeting catch-up and Q&A ("what did I miss?", "what was decided?"), so that I stay oriented even when I join late or multitask.
- **Exemplars:** Microsoft Teams Copilot (real-time transcript Q&A, "what did I miss"), Google **Gemini in Meet**.

**E12-5.** As a **SALES**/manager, I want call recordings analyzed for coaching, objections, and next steps, so that reps improve and deals move without manual call review.
- **Exemplars:** Glean / Dust sales-call-coaching agents, Salesforce Sales Coach, Gong-grounded agents.

**E12-6.** As a **KW**, I want to talk to the assistant by voice and have voice agents handle phone/web conversations, so that automation works hands-free and in spoken channels.
- **Exemplars:** Salesforce **Agentforce Voice**, Glean real-time voice, Microsoft voice Copilot, Perplexity Comet voice, Aisera voice.

---

# EPIC 13 — Knowledge Governance, Trust & Proactive Surfacing

*Making the knowledge that feeds automation trustworthy, fresh, and pushed to the right moment.*

**E13-1.** As a **KM**, I want trust to be a measurable, engineered property of knowledge — content carries a verification state and verifier, and each collection has a trust score — so that humans and agents can tell what's reliable.
- **Exemplars:** Guru **Verification + Trust Score** (per-collection metric, verifier + expiry), Glean Verified badges.
- **AC:** Each item is Verified/Unverified with an assigned SME and a verification frequency; stale items trigger re-verification; a measurable trust score is tracked per collection.

**E13-2.** As a **KM**, I want agents to automatically verify *and* unverify content based on usage and AI analysis, so that knowledge hygiene scales without manual upkeep.
- **Exemplars:** Guru auto-verify/unverify by Knowledge Agents, Glean refresh reminders/deprecation.

**E13-2a.** As a **KM**, I want Knowledge Agents that actively *improve* content — detecting conflicts, merging duplicates, drafting updates from Slack threads, flagging stale pages, and propagating corrections — so that the knowledge base gets better automatically, not just verified.
- **Exemplars:** Guru Knowledge Agents (conflict detection, duplicate merging, doc drafting from Slack, stale content flagging, correction propagation, usage-spike detection, auto-archiving).
- **AC:** Agents detect conflicting versions across sources; merge duplicates; draft updates from conversational threads; flag stale content; propagate corrections to all surfaces; route to experts for review when needed.

**E13-3.** As a **KW**, I want the right verified answer proactively pushed to me based on what's on my screen (e.g., a deal in "Negotiation" vs. a specific competitor), so that I get knowledge without searching.
- **Exemplars:** Guru **Knowledge Triggers** (rule-based, on-screen-context push), Glean Companion (highlight-based).

**E13-4.** As a **KM**, I want automated detection of duplicates, knowledge gaps, and stale content, so that the knowledge base self-heals.
- **Exemplars:** Guru (duplicate detection/merging, gap ID, auto-archive), Glean SSAT/knowledge-gap reporting.

**E13-5.** As a **NH**/KW, I want to be routed to the right human expert (and have experts review AI answers), so that AI and people reinforce each other.
- **Exemplars:** Guru expert detection + Expert Review, Glean Expert Search.

**E13-6.** As an **AGENTOPS** owner, I want our governed, permission-aware knowledge layer exposed to *other* AI tools (IDEs, ChatGPT, Claude) via MCP, so that every tool draws from one trusted, governed source instead of rebuilding governance per tool.
- **Exemplars:** Guru MCP Server, Dropbox **Dash MCP**, Glean MCP server, Atlassian Teamwork Graph via MCP.

---

# EPIC 14 — Agent Governance, Security, Models & Deployment (fleet-level)

*Running a fleet of autonomous agents safely — the enterprise prerequisite for scaling automation.*

**E14-1.** As an **AGENTOPS** owner, I want a single control plane that inventories every agent (who built it, where it runs, how often, what it can access), so that I can govern agent sprawl from one place.
- **Exemplars:** Microsoft **Agent 365**, ServiceNow **AI Control Tower**, Salesforce **Command Center**, Google agent governance, IBM agentic control plane, Atlassian org-wide agent inventory, Glean.

**E14-2.** As a **SEC** owner, I want each agent to have its own managed identity (for authN/authZ and audit), so that agents are governed like employees, not anonymous scripts.
- **Exemplars:** Microsoft **Entra Agent ID**, Google **Agent Identity** (cryptographic per-agent ID).

**E14-3.** As a **SEC** owner, I want runtime monitoring that detects hallucinations, bias, toxic content, data leakage, and drift while agents run, so that problems are caught live, not in postmortems.
- **Exemplars:** ServiceNow AI Control Tower, Google **Model Armor**, IBM watsonx.governance (real-time faithfulness/relevance), Writer **Supervise**.

**E14-4.** As a **SEC** owner, I want guardrails that block prompt injection, malicious code, and restricted topics (comp, financial advice, PHI), and pre-scan write actions for misalignment, so that agents stay within policy.
- **Exemplars:** Glean Protect/Protect+ (+ AWARE), Salesforce **Einstein Trust Layer**, Writer Guardrails (PII/Prompt Shields), Aisera **TRAPS**, Google Model Armor, Microsoft Purview.

**E14-5.** As an **AGENTOPS** owner, I want end-to-end observability (step traces, tool calls, latency, success/feedback) exportable to my existing tooling (OpenTelemetry → Datadog/Splunk), so that agents are operable like any production system.
- **Exemplars:** Salesforce Command Center (OpenTelemetry), Amazon AgentCore Observability, Writer (Datadog/Noma/Lakera), IBM AgentOps, Glean observability + SIEM streaming.

**E14-6.** As a **SEC** owner, I want every agent action and answer to inherit the requesting user's existing permissions (least privilege), so that automation can never exceed what the human could do.
- **Exemplars:** Universal — Glean, Microsoft (Graph trimming), Amazon Q (IAM Identity Center), Google (ACL-preserving), Dust (Spaces), Cohere (IdP-aware), Notion (row-level).

**E14-7.** As a **SEC** owner, I want guarantees that customer data is never used to train third-party models, with zero-retention and bring-your-own-key options, so that sensitive data stays controlled.
- **Exemplars:** Universal — Writer (no-train/ZDR/BYOK), Hebbia (ZDR default), Glean (zero-retention), Anthropic/OpenAI enterprise (no-train), Salesforce (Anthropic in-VPC).

**E14-8.** As a **SEC** owner in a regulated/sovereign environment, I want to deploy the platform fully inside my own boundary — on-prem, VPC, or air-gapped, on minimal hardware — so that data never leaves my environment.
- **Exemplars:** Cohere **North** (on-prem/VPC/air-gapped, runs on 2 GPUs), Glean cloud-prem single-tenant, AlphaSense Private Cloud/BYOB, Writer BYOK/private endpoints.

**E14-9.** As an **ADM**, I want to curate which models are available, set a smart default ("best model"), route by task, and optimize token cost, so that I control quality, compliance, and spend across models.
- **Exemplars:** Glean Model Hub + "Best model", Aisera **LLM Gateway** (BYO + 30–60% token savings), Microsoft per-task multi-model, Salesforce Models API + LLM Open Connector, Sana model-agnostic.

**E14-10.** As an **ANALYST** in a regulated domain, I want access to domain-specialized models tuned for my field, so that accuracy meets professional/regulatory bars.
- **Exemplars:** Writer **Palmyra Med/Fin** (Fin passed CFA III; Med 90.9% clinical), AlphaSense **ASLLM**, ServiceNow **Now LLM**, Cohere Command, Moveworks domain models.

**E14-11.** As an **AGENTOPS** owner, I want grounding via a knowledge graph (not just vector RAG) with provenance/citations, so that retrieval is more accurate and auditable.
- **Exemplars:** Writer **Knowledge Graph** (graph-RAG, #1 RobustQA 86.31%), Glean Enterprise Graph, ServiceNow Knowledge Graph, Atlassian Teamwork Graph, Google enterprise knowledge graph.

**E14-12.** As a **SEC** owner, I want compliance tooling — a compliance API, audit logs, and prebuilt regulatory frameworks (EU AI Act, ISO 42001, NIST AI RMF) — so that audits and AI-governance obligations are manageable.
- **Exemplars:** Anthropic **Compliance API**, IBM **Compliance Accelerators** (12 frameworks) + factsheets, Glean audit/SIEM, certifications (SOC 2, ISO 27001/42001, HIPAA) across the field.

---

# EPIC 15 — Deepened Departmental Automation

*High-value, role-specific automations the competitive set makes concrete (extends Glean doc Epic 6).*

**E15-1.** As an **IT**/HR service owner, I want autonomous self-service that resolves a large share of tickets end-to-end (troubleshoot, file, grant access) with measurable deflection, so that agents handle routine requests before a human ever sees them.
- **Exemplars:** Moveworks (Slack/Teams self-service; 6M+ employees rely on it; 10K+ AI agents built; 8-week typical time to value; 90% enterprise-wide deployment), Aisera (64–84% auto-resolution, AIOps), ServiceNow Now Assist, Microsoft Employee Self-Service agent, Glean IT/HR agents.
- **AC:** Multi-domain routing (IT/HR/Finance/Facilities); auto-resolution where possible; clean escalation with context where not; deflection-rate analytics.

**E15-2.** As a **FIN**/research analyst, I want an autonomous research desk that pulls data daily, analyzes it, and drafts client-ready outputs (earnings summaries, portfolio reports), so that recurring financial knowledge work runs itself.
- **Exemplars:** Writer Agentic Research Desk / portfolio reporting, AlphaSense (earnings/primer/Bull-Bear agents), Hebbia (investment memos).

**E15-3.** As an **ANALYST** in PE/legal/consulting, I want to run due diligence across an entire data room — extract terms, flag risks, build comparison tables with citations — so that diligence that took weeks takes hours.
- **Exemplars:** Hebbia Matrix / Due Diligence, AlphaSense Due Diligence Workspaces.

**E15-4.** As a **SUP** leader, I want an autonomous service agent that resolves a wide range of customer issues without scripted flows (and escalates with full context), so that support scales without proportional headcount.
- **Exemplars:** Salesforce **Agentforce Service Agent**, ServiceNow, Aisera, Glean support agents.

**E15-5.** As a **SALES** leader, I want an autonomous SDR agent that nurtures inbound leads 24/7 (answers questions, handles objections, books meetings), so that pipeline is worked even when reps are offline.
- **Exemplars:** Salesforce **Einstein SDR Agent**, Glean prospecting agents.

**E15-6.** As an **ENG**, I want coding agents that turn a spec or ticket into a reviewed PR, review others' PRs, and run from my CLI/IDE, so that routine implementation and review are automated.
- **Exemplars:** Atlassian **Rovo Dev** (CLI/VS Code/Bitbucket), Anthropic **Claude Code**, Glean Code Writer / Spec-to-PR, GitHub Copilot, OpenAI Codex.

**E15-7.** As a **SALES**/LEGAL/SEC user, I want RFP and security-questionnaire responses drafted from approved past answers and policy docs, so that high-volume repetitive responses are automated and consistent.
- **Exemplars:** Glean RFP workflow, Dust "securitySam", AlphaSense, Writer.

**E15-8.** As an **OPS** owner of a global product, I want a localization agent that translates and adapts content at scale, so that go-to-market in every market is faster.
- **Exemplars:** Dust **Tolki** (Qonto: 70% faster localization), Writer, Sana (60+ languages).

---

# 5. Updates to the existing Glean-specific epics

How the competitive research **extends or reframes** the original [glean-user-stories.md](glean-user-stories.md):

- **Epic 2 (Assistant) → add computer use.** Glean's Assistant is strong on retrieval+reasoning but the field has moved to **agents that operate browsers/desktops** (Epic 8). A complete assistant story set now includes UI automation and self-correction.
- **Epic 2 (Deep Research) → now category-standard.** Deep Research is no longer a differentiator; it's table stakes (E9-1). The differentiator has shifted to **grid/exhaustive analysis over thousands of docs** (E9-4) and **research over premium content** (E9-5).
- **Epic 3 (Agents) → autonomy is the frontier.** Glean has triggers/orchestration, but stories should foreground **event-driven + scheduled + write-back unattended operation** (Epic 7) and **deterministic-vs-autonomous reliability controls** (E7-9), where Salesforce Agent Script and Copilot agent flows are ahead.
- **Epic 3 (Builder) → add Skills + Playbooks/Routines.** Reusable, propagating **Skills** (E11-2) and schedulable **Playbooks/Routines** (E11-3) are now expected building blocks (Anthropic open standard, Dust, Writer).
- **Epic 1/2 (Search/Assistant) → in-app document agency.** Competitors act *inside* documents and the system of record (Office Agent Mode, Notion Agents) — Epic 10 captures automation Glean's canvas only partially addresses.
- **Epic 5 (Security/Governance) → fleet-level control plane.** Glean Protect is strong, but the bar is now a **named control plane + per-agent identity + runtime drift detection** (Epic 14) — Agent 365, AI Control Tower, Command Center, watsonx.governance.
- **Epic 1 (Knowledge mgmt) → trust as a metric + proactive triggers.** Guru's **Trust Score** and **Knowledge Triggers** (Epic 13) push beyond Glean's verification badges toward measurable trust and screen-context surfacing.
- **New cross-cutting:** **meeting intelligence as indexed knowledge** (Epic 12) and **sovereign/air-gapped deployment** (E14-8) are areas where Sana, Notion, and Cohere set a bar worth matching.

---

# Appendix A — The agentic stack (mental model for the stories)

Most platforms can be read as layers; user stories map cleanly onto them:

1. **Connect & index** — connectors, knowledge/Enterprise graph, permissions mirroring (Glean, Dash, Q index, Teamwork Graph).
2. **Retrieve & ground** — hybrid/graph RAG, citations, web + premium content (Writer KG, AlphaSense, all).
3. **Reason & plan** — agentic engines, deep research, sub-agents, code execution (Glean Agentic Engine, Atlas, Hebbia ISD).
4. **Act** — read/write actions, computer use, MCP tools, iPaaS-replacement (Moveworks, Q plugins, ChatGPT agent).
5. **Automate** — triggers, schedules, orchestration, write-back (Dust Triggers, Writer Routines).
6. **Build** — no-code builders, Skills, Playbooks, templates, APIs/SDKs (everyone).
7. **Govern** — control plane, identity, guardrails, observability, evals, deployment/sovereignty (Agent 365, AI Control Tower, Glean Protect).

# Appendix B — Spectrum of autonomy (useful for acceptance criteria)

A single capability often spans a maturity ladder — specify which rung a story targets:

1. **Assistive** — suggests; human does the work (early Copilot, autocomplete).
2. **On-demand agent** — does a multi-step task when prompted (Deep Research, ChatGPT agent).
3. **Scheduled agent** — runs on a cadence unattended (Routines, Tasks, Custom Agents).
4. **Event-driven agent** — runs when a signal fires, writes back (Writer Event Triggers, Dust webhooks).
5. **Orchestrated multi-agent** — a supervisor coordinates specialists across systems (Orchestrator, Atlas, A2A).
6. **Governed autonomous fleet** — many agents run continuously under a control plane with identity, guardrails, and observability (Agent 365, AI Control Tower).

# Appendix C — Cross-cutting acceptance criteria for automation stories

Apply these as default ACs to any Epic 7–15 story (in addition to the §A criteria in the Glean doc):

1. **Permission-inherited** — the agent acts only within the triggering user's access.
2. **Cited & auditable** — outputs ground in sources; actions are logged with full traces.
3. **Approval-gated where consequential** — write/spend/send actions support human-in-the-loop (ideally argument-level).
4. **Observable** — step traces, success/feedback, latency exportable to existing tooling.
5. **Reversible/versioned** — agents and their changes are versioned; rollback is possible.
6. **Governed at the fleet level** — registered in an inventory with an identity, guardrails, and runtime monitoring.
7. **Model-flexible** — model is selectable/route-able (per task/step) under admin policy, with no-training guarantees.

# Appendix D — Key competitive metrics (from public claims, mid-2026)

| Vendor | Metric | Value |
|---|---|---|
| Glean | Hours saved per user/year | 110 |
| Glean | ROI (Forrester TEI, 3 years) | 141% |
| Glean | Enterprise adoption in < 2 years | 93% |
| Glean | Token reduction vs. off-the-shelf MCP | 30% |
| Glean | Time to ROI | < 6 months |
| Moveworks | Employees relying on platform | 6M+ |
| Moveworks | AI agents built | 10K+ |
| Moveworks | Typical time to value | 8 weeks |
| Moveworks | Enterprise-wide deployment rate | 90% |
| Aisera | Auto-resolution rate | 64–84% |
| Writer | RobustQA score (graph-RAG) | 86.31% (#1) |
| Notion AI | Custom Agents pricing | $10 per 1,000 credits |
| Procore (Moveworks customer) | Operator hours saved per quarter | ~4,000 |
| BambooHR (Moveworks customer) | Ticket volume reduction | 20–30% |
