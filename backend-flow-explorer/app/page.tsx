"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
} from "react";
import { endpointCatalog, type EndpointDefinition } from "./generated-endpoints";

type Runtime = "browser" | "backend" | "postgres" | "redis" | "search" | "worker" | "storage" | "provider";
type Layer = "transport" | "api" | "service" | "adapter" | "external";

type FlowNode = {
  id: string;
  step: string;
  title: string;
  subtitle: string;
  x: number;
  y: number;
  runtime: Runtime;
  layer: Layer;
  symbol: string;
  file: string;
  method: string;
  responsibility: string;
  happens: string[];
  calls: string;
  returns: string;
  guarantee: string;
};

const chatNodes: FlowNode[] = [
  {
    id: "browser-post",
    step: "01",
    title: "Send message",
    subtitle: "POST …/messages",
    x: 70,
    y: 100,
    runtime: "browser",
    layer: "transport",
    symbol: "HTTP",
    file: "frontend/src/api/generated.ts",
    method: "POST /api/v1/chat/sessions/{session_id}/messages",
    responsibility: "Starts a turn and asks the backend to persist the user message.",
    happens: [
      "The browser sends content, an optional per-turn model, and optional collection/document scope.",
      "The access token identifies the user and tenant; IDs in the request never establish authority.",
    ],
    calls: "FastAPI request middleware and dependency injection",
    returns: "202 Accepted with the persisted user message and a streamId",
    guarantee: "The slow answer does not hold the HTTP request open.",
  },
  {
    id: "deps",
    step: "02",
    title: "Resolve request scope",
    subtitle: "FastAPI dependencies",
    x: 340,
    y: 100,
    runtime: "backend",
    layer: "api",
    symbol: "DI",
    file: "backend/app/api/deps.py",
    method: "get_current_user() · get_db_session() · get_backplane()",
    responsibility: "Builds the trusted request context before endpoint code runs.",
    happens: [
      "Validates the bearer token through the auth boundary.",
      "Opens a tenant-bound async SQLAlchemy session and injects shared settings/backplane adapters.",
    ],
    calls: "auth/ token verifier, db/session.py, realtime/backplane.py",
    returns: "CurrentUser, tenant_id, DbSession, Settings, Backplane",
    guarantee: "Identity and tenancy come from the verified token, not request fields.",
  },
  {
    id: "router",
    step: "03",
    title: "Route the use case",
    subtitle: "send_message()",
    x: 610,
    y: 100,
    runtime: "backend",
    layer: "api",
    symbol: "API",
    file: "backend/app/api/v1/chat.py:729",
    method: "send_message(...) -> SendMessageResponse",
    responsibility: "Validates the wire shape, invokes one service, commits, and schedules the answer.",
    happens: [
      "Builds ChatService from tenant-scoped repositories.",
      "Turns a non-visible session into the same 404 used for missing sessions.",
      "Commits the user-side transaction before the detached producer starts.",
    ],
    calls: "ChatService.send_message(); then _schedule_answer()",
    returns: "SendMessageResponse(message, stream_id)",
    guarantee: "The router coordinates only; model and retrieval logic stay out of api/.",
  },
  {
    id: "chat-service",
    step: "04",
    title: "Prepare the turn",
    subtitle: "ChatService.send_message",
    x: 880,
    y: 100,
    runtime: "backend",
    layer: "service",
    symbol: "SVC",
    file: "backend/app/services/chat_service.py:503",
    method: "ChatService.send_message(...) -> SendResult | None",
    responsibility: "Applies use-case rules and assembles everything the answer runtime needs.",
    happens: [
      "Checks tenant + owner visibility and validates the optional model override before writing.",
      "Loads assistant configuration, custom instructions, rolling summary, and uncovered history.",
      "Mints a stream ID and binds it to this user + tenant in the backplane.",
    ],
    calls: "ChatSessionRepository, MessageRepository, preferences/summary repositories, Backplane.bind_owner",
    returns: "SendResult with message, stream, model, history, assistant config and memory context",
    guarantee: "Cross-tenant/non-owner sessions disclose nothing and produce no write.",
  },
  {
    id: "persist-user",
    step: "05",
    title: "Persist user turn",
    subtitle: "One request transaction",
    x: 1150,
    y: 100,
    runtime: "postgres",
    layer: "adapter",
    symbol: "DB",
    file: "backend/app/db/repositories.py",
    method: "MessageRepository.add() · ChatSessionRepository.touch()",
    responsibility: "Writes the user message and updates session ordering under tenant scope.",
    happens: [
      "Repositories are the only SQL surface; services receive domain entities back.",
      "The router commits before returning 202, so the producer sees durable history in its fresh session.",
    ],
    calls: "PostgreSQL 16 through async SQLAlchemy",
    returns: "Persisted Message domain entity",
    guarantee: "RLS plus repository tenant predicates provide defense in depth.",
  },
  {
    id: "accepted",
    step: "06",
    title: "Return immediately",
    subtitle: "202 Accepted",
    x: 1420,
    y: 100,
    runtime: "browser",
    layer: "transport",
    symbol: "202",
    file: "backend/app/api/v1/chat.py:765",
    method: "return SendMessageResponse(...) ",
    responsibility: "Hands the browser a durable user turn and the capability-free stream identifier.",
    happens: [
      "The browser can render the user message without waiting for model latency.",
      "The stream ID alone grants no access; the WebSocket path re-authenticates and re-authorizes.",
    ],
    calls: "Frontend opens GET /ws/chat/{stream_id}?access_token=…",
    returns: "The HTTP request lifecycle ends",
    guarantee: "Model latency and tool loops are fully outside the REST response cycle.",
  },
  {
    id: "detached-task",
    step: "07",
    title: "Detach producer",
    subtitle: "asyncio.create_task",
    x: 1420,
    y: 355,
    runtime: "backend",
    layer: "service",
    symbol: "TASK",
    file: "backend/app/api/v1/chat.py:774",
    method: "_schedule_answer() → _produce()",
    responsibility: "Runs generation independently while keeping shutdown accountable.",
    happens: [
      "Registers the producer in app.state.answer_tasks instead of using a response BackgroundTask.",
      "The app lifespan cancels and drains tracked producers during shutdown.",
      "A successful answer may enqueue a best-effort rolling-summary Celery task afterward.",
    ],
    calls: "ChatRuntime.run() with a new sessionmaker",
    returns: "No HTTP return; publishes stream events",
    guarantee: "Detached does not mean orphaned: tasks are tracked and drained.",
  },
  {
    id: "runtime",
    step: "08",
    title: "Run grounded answer",
    subtitle: "ChatRuntime.run",
    x: 1150,
    y: 355,
    runtime: "backend",
    layer: "service",
    symbol: "RUN",
    file: "backend/app/services/chat_runtime.py:433",
    method: "ChatRuntime.run(...) -> bool",
    responsibility: "Owns the complete answer lifecycle and exactly-one-terminal stream contract.",
    happens: [
      "Publishes start, opens a fresh async DB session, and binds the tenant RLS context.",
      "Calls _answer() for the agentic loop, commits assistant data, then publishes done.",
      "Maps typed or unexpected failures to one sanitized error envelope.",
    ],
    calls: "_answer(); _persist(); RedisBackplane.publish()",
    returns: "true on committed + terminally published success; false on mapped failure",
    guarantee: "The producer has its own transaction because the request session is already closed.",
  },
  {
    id: "gateway",
    step: "09",
    title: "Stream a model turn",
    subtitle: "LLMGateway.stream_tools",
    x: 880,
    y: 355,
    runtime: "backend",
    layer: "adapter",
    symbol: "LLM",
    file: "backend/app/llm/gateway.py:487",
    method: "LLMGateway.stream_tools(...) -> AsyncIterator[StreamEvent]",
    responsibility: "Isolates LiteLLM/provider details behind domain stream events.",
    happens: [
      "Serializes domain messages and only the governed tool schemas offered for this run.",
      "Buffers provider tool-call fragments into complete ToolCall values.",
      "Maps provider exceptions to typed application errors and closes upstream streams on cancellation.",
    ],
    calls: "LiteLLM acompletion(stream=True) → OpenRouter or configured provider",
    returns: "Text chunks, tool calls, finish reason and token usage",
    guarantee: "No LiteLLM or vendor response type escapes llm/.",
  },
  {
    id: "tool-runner",
    step: "10",
    title: "Govern tool calls",
    subtitle: "ToolRunner.run",
    x: 610,
    y: 355,
    runtime: "backend",
    layer: "service",
    symbol: "TOOL",
    file: "backend/app/services/tools/runner.py",
    method: "ToolRunner.run(...) -> ToolResult",
    responsibility: "Is the single gate every model-requested tool must pass.",
    happens: [
      "Enforces the assistant allow-list, tenant policy, autonomy/approval tier, timeouts and bounded concurrency.",
      "Records tool invocation rows and audit events whether a call succeeds or is refused.",
      "Tool results are appended to the conversation and the model may take another bounded turn.",
    ],
    calls: "Native retrieval tools, optional MCP tools, optional sandbox seam",
    returns: "Provider-neutral ToolResult values for the next model turn",
    guarantee: "The model proposes calls; policy code decides whether they execute.",
  },
  {
    id: "retrieval",
    step: "11",
    title: "Retrieve permitted context",
    subtitle: "RetrievalService.search",
    x: 340,
    y: 355,
    runtime: "backend",
    layer: "adapter",
    symbol: "RAG",
    file: "backend/app/retrieval/service.py:125",
    method: "RetrievalService.search(...) -> list[RetrievedPassage]",
    responsibility: "Turns a query into ranked passages the current principal is allowed to read.",
    happens: [
      "Builds the owner-or-grant allow set from the verified principal.",
      "Embeds the query, executes hybrid BM25 + vector search, then re-checks permissions while hydrating SQL rows.",
      "Carries document names and character offsets forward for resolvable citations.",
    ],
    calls: "LLMGateway.embed(); OpenSearchStore.hybrid_search(); queries.load_passages()",
    returns: "Domain RetrievedPassage values, ranked and permission-trimmed",
    guarantee: "No unfiltered fallback exists; engine failure fails closed.",
  },
  {
    id: "provider",
    step: "12a",
    title: "Model provider",
    subtitle: "LiteLLM gateway route",
    x: 740,
    y: 635,
    runtime: "provider",
    layer: "external",
    symbol: "AI",
    file: "External boundary",
    method: "chat completion / embeddings HTTP",
    responsibility: "Produces model tokens, tool-call intent and query embeddings.",
    happens: [
      "OpenRouter is the first configured route; per-tenant provider routes can override it.",
      "Credentials are resolved once per answer and never logged or exposed above the gateway.",
    ],
    calls: "Provider model infrastructure",
    returns: "Provider response streamed back into the gateway adapter",
    guarantee: "Vendor behavior is quarantined behind llm/ and typed errors.",
  },
  {
    id: "opensearch",
    step: "12b",
    title: "Hybrid search",
    subtitle: "OpenSearchStore",
    x: 70,
    y: 635,
    runtime: "search",
    layer: "external",
    symbol: "OS",
    file: "backend/app/search/store.py:234",
    method: "ensure_index() · hybrid_search(...) ",
    responsibility: "Ranks allowed candidate chunks using lexical and vector signals in one retrieval store.",
    happens: [
      "Receives a precomputed embedding and an engine-side allow filter.",
      "Returns identifiers and scores, not authoritative citation text.",
    ],
    calls: "OpenSearch 2.19 HTTP API",
    returns: "Ranked chunk IDs + scores",
    guarantee: "OpenSearch is the candidate engine; PostgreSQL remains the authoritative row source.",
  },
  {
    id: "rehydrate",
    step: "12c",
    title: "Re-check & hydrate",
    subtitle: "queries.load_passages",
    x: 340,
    y: 635,
    runtime: "postgres",
    layer: "adapter",
    symbol: "DB",
    file: "backend/app/retrieval/queries.py",
    method: "load_passages(session, allow_set, chunk_ids)",
    responsibility: "Fetches authoritative passage text and repeats the SQL permission predicate.",
    happens: [
      "Drops stale, deleted, revoked or mismatched search hits.",
      "Hydrates source names and exact character offsets for citation integrity.",
    ],
    calls: "PostgreSQL through the runtime's tenant-bound session",
    returns: "Permitted relational passage rows",
    guarantee: "Permission is checked at retrieval and again at read-back.",
  },
  {
    id: "persist-answer",
    step: "13",
    title: "Persist grounded result",
    subtitle: "ChatRuntime._persist",
    x: 1010,
    y: 635,
    runtime: "postgres",
    layer: "adapter",
    symbol: "DB",
    file: "backend/app/services/chat_runtime.py:1943",
    method: "_persist(...) ",
    responsibility: "Commits the assistant message, validated citations, usage, tool trace and audit record together.",
    happens: [
      "Citation records are built only from passages actually returned by governed retrieval calls.",
      "The model actually used after any fallback is recorded with token/cache usage.",
      "The runtime transaction commits before the terminal done envelope is sent.",
    ],
    calls: "Message, Citation, LlmUsage, ToolInvocation and Audit repositories",
    returns: "Committed assistant message and citation count",
    guarantee: "A successful done event refers to data already committed.",
  },
  {
    id: "redis",
    step: "14",
    title: "Publish stream envelopes",
    subtitle: "RedisBackplane.publish",
    x: 1280,
    y: 635,
    runtime: "redis",
    layer: "adapter",
    symbol: "MQ",
    file: "backend/app/realtime/backplane.py:162",
    method: "publish(stream_id, envelope)",
    responsibility: "Decouples answer production from whichever backend process owns the WebSocket.",
    happens: [
      "Publishes ordered start, step/tool/citation/delta events, and exactly one terminal envelope.",
      "Keeps a bounded replay list so a WebSocket opened just after the 202 does not miss early events.",
      "Stores the user + tenant ownership binding separately from the random stream ID.",
    ],
    calls: "Redis pub/sub plus bounded replay/owner keys",
    returns: "Envelopes to live and late subscribers",
    guarantee: "Redis is transport state, not authorization; ownership is checked before relay.",
  },
  {
    id: "websocket",
    step: "15",
    title: "Authorize & relay",
    subtitle: "GET /ws/chat/{stream_id}",
    x: 1420,
    y: 850,
    runtime: "backend",
    layer: "transport",
    symbol: "WS",
    file: "backend/app/realtime/chat_ws.py:68",
    method: "chat_ws(websocket, stream_id, access_token)",
    responsibility: "Authenticates the socket, authorizes this stream, and relays envelopes verbatim.",
    happens: [
      "Validates the query-string access token before accepting the WebSocket handshake.",
      "Matches both user_id and tenant_id against Backplane.get_owner(stream_id).",
      "Subscribes only after authorization and closes after done/error or disconnect.",
    ],
    calls: "verify_access_token(); Backplane.get_owner(); Backplane.subscribe()",
    returns: "JSON WebSocket envelope sequence",
    guarantee: "Unknown and foreign stream IDs are denied identically with no envelope disclosure.",
  },
  {
    id: "browser-stream",
    step: "16",
    title: "Render the answer",
    subtitle: "WebSocket consumer",
    x: 1150,
    y: 850,
    runtime: "browser",
    layer: "transport",
    symbol: "UI",
    file: "frontend/src/api/ws.ts",
    method: "openChatStream(streamId, handlers)",
    responsibility: "Turns the envelope lifecycle into visible progress, answer text, tool trace and citations.",
    happens: [
      "The stream may replay events emitted before the socket subscribed.",
      "done finalizes the already-committed answer; error presents a typed retryable problem.",
    ],
    calls: "UI state/rendering",
    returns: "A completed grounded answer in the chat thread",
    guarantee: "The browser never talks directly to databases, search engines or model providers.",
  },
];

type Edge = {
  from: string;
  to: string;
  label?: string;
  direction: "right" | "left" | "down" | "up";
  x: number;
  y: number;
  length: number;
  tone?: "normal" | "async" | "loop" | "return";
};

const chatEdges: Edge[] = [
  { from: "browser-post", to: "deps", direction: "right", x: 277, y: 166, length: 63 },
  { from: "deps", to: "router", direction: "right", x: 547, y: 166, length: 63 },
  { from: "router", to: "chat-service", direction: "right", x: 817, y: 166, length: 63 },
  { from: "chat-service", to: "persist-user", direction: "right", x: 1087, y: 166, length: 63 },
  { from: "persist-user", to: "accepted", direction: "right", x: 1357, y: 166, length: 63 },
  { from: "accepted", to: "detached-task", direction: "down", x: 1523, y: 232, length: 123, tone: "async", label: "off request" },
  { from: "detached-task", to: "runtime", direction: "left", x: 1357, y: 421, length: 63, tone: "async" },
  { from: "runtime", to: "gateway", direction: "left", x: 1087, y: 421, length: 63 },
  { from: "gateway", to: "tool-runner", direction: "left", x: 817, y: 421, length: 63, tone: "loop", label: "tool intent" },
  { from: "tool-runner", to: "retrieval", direction: "left", x: 547, y: 421, length: 63, tone: "loop" },
  { from: "gateway", to: "provider", direction: "down", x: 983, y: 487, length: 148, label: "model HTTP" },
  { from: "retrieval", to: "opensearch", direction: "left", x: 277, y: 701, length: 63 },
  { from: "retrieval", to: "rehydrate", direction: "down", x: 443, y: 487, length: 148 },
  { from: "opensearch", to: "rehydrate", direction: "right", x: 277, y: 701, length: 63 },
  { from: "rehydrate", to: "tool-runner", direction: "up", x: 443, y: 487, length: 148, tone: "return", label: "passages" },
  { from: "tool-runner", to: "gateway", direction: "right", x: 817, y: 453, length: 63, tone: "return", label: "tool results" },
  { from: "runtime", to: "persist-answer", direction: "down", x: 1125, y: 487, length: 148 },
  { from: "persist-answer", to: "redis", direction: "right", x: 1217, y: 701, length: 63, tone: "return" },
  { from: "redis", to: "websocket", direction: "down", x: 1383, y: 767, length: 83, tone: "return" },
  { from: "websocket", to: "browser-stream", direction: "left", x: 1357, y: 916, length: 63, tone: "return" },
];

const runtimeMeta: Record<Runtime, { label: string; sub: string; color: string }> = {
  browser: { label: "Browser", sub: "React SPA", color: "#9cf0d0" },
  backend: { label: "Backend", sub: "FastAPI · Python 3.12", color: "#9eb8ff" },
  postgres: { label: "PostgreSQL", sub: "relational truth + RLS", color: "#d9b2ff" },
  redis: { label: "Redis", sub: "stream backplane", color: "#ff9b8e" },
  search: { label: "OpenSearch", sub: "hybrid retrieval", color: "#ffc96a" },
  worker: { label: "Sandbox / worker", sub: "isolated execution container", color: "#ffb08f" },
  storage: { label: "Object storage", sub: "MinIO through the S3 adapter", color: "#70dfc1" },
  provider: { label: "Model provider", sub: "via LiteLLM", color: "#7dd9ff" },
};

const layerLabels: Record<Layer, string> = {
  transport: "Transport",
  api: "API layer",
  service: "Service orchestration",
  adapter: "Adapter boundary",
  external: "External system",
};

const chatTraceOrder = [
  "browser-post",
  "deps",
  "router",
  "chat-service",
  "persist-user",
  "accepted",
  "detached-task",
  "runtime",
  "gateway",
  "tool-runner",
  "retrieval",
  "provider",
  "opensearch",
  "rehydrate",
  "tool-runner",
  "gateway",
  "runtime",
  "persist-answer",
  "redis",
  "websocket",
  "browser-stream",
];

const serviceByTag: Record<string, string> = {
  health: "Readiness probes", auth: "AuthService", collections: "CollectionService",
  documents: "DocumentService", artifacts: "ArtifactService", chat: "ChatService",
  models: "ModelCatalogService", search: "SearchService", audit: "AuditQueryService",
  admin: "AdminGovernanceService", sources: "SourceService", preferences: "PreferencesService",
  user: "UserProfileService", "saved-searches": "SavedSearchService", "mcp-servers": "McpServerService",
  assistants: "AssistantService", "code-runs": "CodeRunService", schedules: "ScheduleService",
  runs: "RunControlService", "run-deliveries": "RunDeliveryService", realtime: "Realtime gateway",
};

function genericNodes(endpoint: EndpointDefinition): FlowNode[] {
  const isPublic = endpoint.auth === "Public";
  const isWrite = ["POST", "PUT", "PATCH", "DELETE"].includes(endpoint.method);
  const isSearch = endpoint.tag === "search" || endpoint.operationId === "getDocumentText";
  const isStorage = endpoint.operationId === "uploadDocument" || endpoint.operationId === "updateAvatar" || endpoint.path.endsWith("/content");
  const isJob = endpoint.tag === "schedules" || endpoint.tag === "runs" || endpoint.operationId === "syncSource";
  const isSandbox = endpoint.tag === "code-runs" || endpoint.path.includes("/sandbox");
  const isOAuth = endpoint.operationId === "connectSource" || endpoint.operationId === "oauthCallback";
  const serviceName = isSandbox ? "SandboxSessionService" : serviceByTag[endpoint.tag] ?? "Application service";
  const serviceFile = isSandbox ? "backend/app/sandbox/service.py" : "backend/app/services/";
  const serviceMethod = isSandbox
    ? `SandboxSessionService.${endpoint.operationId === "closeSandboxSession" ? "close" : endpoint.handler.replace("_sandbox_session", "")}(...)`
    : `${serviceName}.${endpoint.handler}(...)`;
  const boundary = isSearch
    ? { title: "Permissioned retrieval", subtitle: "retrieval/ chokepoint", runtime: "search" as Runtime, symbol: "IDX", file: "backend/app/retrieval/", method: "retrieve_authorized(...)", responsibility: "Queries through the single retrieval boundary and removes anything the caller cannot access.", calls: "search/ → OpenSearch", returns: "Permission-trimmed hits with provenance", guarantee: "Unauthorized hits are excluded at retrieval time." }
    : isStorage
      ? { title: "Object storage", subtitle: "storage/ adapter", runtime: "storage" as Runtime, symbol: "S3", file: "backend/app/storage/", method: "ObjectStore.get_or_put(...)", responsibility: "Keeps S3/MinIO mechanics behind Lumen's object-storage interface.", calls: "MinIO container", returns: "Object metadata or byte stream", guarantee: "Only storage/ knows provider-specific object APIs." }
      : isJob || isSandbox
        ? { title: isSandbox ? "Sandbox runner" : "Background task", subtitle: isSandbox ? "isolated container boundary" : "Celery worker", runtime: "worker" as Runtime, symbol: isSandbox ? "BOX" : "JOB", file: isSandbox ? "backend/app/sandbox/runner.py" : "backend/app/tasks/", method: isSandbox ? "HttpSandboxRunner.close_session(...)" : "task.delay(...)", responsibility: isSandbox ? "Destroys the reusable sandbox through the isolated runner boundary." : "Moves durable, retryable work out of the request lifecycle.", calls: isSandbox ? "Sandbox runner container over HTTP" : "Redis broker → Celery worker", returns: isSandbox ? "Confirmed container teardown" : "Task id and durable run state", guarantee: isSandbox ? "Sandbox lifecycle operations never run user containers inside the API process." : "A disconnect does not cancel committed work." }
        : isOAuth
          ? { title: "Connector provider", subtitle: "OAuth boundary", runtime: "provider" as Runtime, symbol: "OA", file: "backend/app/connectors/", method: "ConnectorOAuthAdapter.exchange(...)", responsibility: "Validates OAuth state and maps provider data into Lumen domain types.", calls: "External OAuth provider", returns: "Connector identity and protected credentials", guarantee: "Provider types and tokens remain inside connectors/." }
          : { title: endpoint.tag === "admin" ? "Apply governance" : "Repository operation", subtitle: endpoint.tag === "admin" ? "role + policy checks" : "tenant-scoped SQL", runtime: "postgres" as Runtime, symbol: endpoint.tag === "admin" ? "GOV" : "DB", file: endpoint.tag === "admin" ? "backend/app/services/" : "backend/app/db/repositories.py", method: endpoint.tag === "admin" ? "PolicyService.apply(...)" : "Repository.execute(...)", responsibility: endpoint.tag === "admin" ? "Checks role and risk policy before governed state is read or changed." : "Loads or changes relational state through the database boundary.", calls: "PostgreSQL through async SQLAlchemy", returns: "Domain entities or non-disclosing not found", guarantee: "Cross-tenant or unauthorized direct fetches resolve to 404." };

  const common = (partial: Partial<FlowNode> & Pick<FlowNode, "id" | "step" | "title" | "subtitle" | "x" | "y" | "runtime" | "layer" | "symbol">): FlowNode => ({
    file: endpoint.source, method: `${endpoint.handler}(...)`, responsibility: endpoint.summary,
    happens: ["Receives domain-safe inputs from the preceding boundary.", "Returns an explicit result to the next named boundary."],
    calls: "Next named component", returns: "Typed result or mapped error", guarantee: "Tenant and permission scope stay attached to the request.", ...partial,
  });

  if (endpoint.transport === "WebSocket") {
    const chat = endpoint.path.includes("chat");
    return [
      common({ id: "ws-open", step: "01", title: "Open socket", subtitle: endpoint.path, x: 70, y: 110, runtime: "browser", layer: "transport", symbol: "WS", file: "frontend/src/api/ws.ts", method: `new WebSocket(${endpoint.path})`, responsibility: "Starts the long-lived realtime connection.", happens: [chat ? "Includes the token and stream id returned by sendMessage." : "Uses the public health channel.", "Negotiates the WebSocket upgrade."], calls: endpoint.source, returns: "Open socket or close code", guarantee: "Realtime transport stays separate from REST." }),
      common({ id: "ws-handler", step: "02", title: endpoint.handler, subtitle: "WebSocket route", x: 340, y: 110, runtime: "backend", layer: "api", symbol: "API", responsibility: "Accepts the transport and establishes connection scope.", happens: [chat ? "Validates the access token before subscribing." : "Accepts the minimal health protocol.", "Closes deliberately on invalid input or disconnect."], calls: chat ? "auth/ + ownership check" : "health loop", returns: "Accepted connection", guarantee: chat ? "Authentication precedes subscription." : "No tenant data crosses this route." }),
      common({ id: "ws-owner", step: "03", title: chat ? "Verify stream owner" : "Handle keepalive", subtitle: chat ? "user + tenant binding" : "ping / pong", x: 610, y: 110, runtime: chat ? "redis" : "backend", layer: chat ? "adapter" : "service", symbol: chat ? "ACL" : "PING", file: chat ? "backend/app/realtime/backplane.py" : endpoint.source, method: chat ? "assert_owner(...)" : "receive_text(...) ", responsibility: chat ? "Confirms this principal owns the requested answer stream." : "Maintains a minimal liveness conversation.", happens: [chat ? "Loads the binding minted by ChatService." : "Receives a health ping.", chat ? "Rejects expired or different-owner bindings." : "Returns a pong."], calls: chat ? "Redis ownership record" : "Socket transport", returns: chat ? "Authorized subscription" : "Health response", guarantee: chat ? "Guessing a stream id grants no access." : "No application data is exposed." }),
      common({ id: "ws-sub", step: "04", title: chat ? "Subscribe to events" : "Keep connection alive", subtitle: chat ? "Redis pub/sub" : "socket loop", x: 880, y: 110, runtime: chat ? "redis" : "backend", layer: "adapter", symbol: chat ? "SUB" : "LOOP", responsibility: chat ? "Receives owned producer events from the shared backplane." : "Waits for the next probe.", happens: [chat ? "Subscribes only after ownership succeeds." : "Uses no database or model resources.", "Cleans up on disconnect."], calls: chat ? "Redis pub/sub" : "WebSocket receive", returns: "Versioned envelopes", guarantee: "Connection cleanup is deterministic." }),
      common({ id: "ws-ui", step: "05", title: chat ? "Render streamed answer" : "Observe health", subtitle: "browser consumer", x: 880, y: 375, runtime: "browser", layer: "transport", symbol: "UI", file: "frontend/src/", method: "onmessage(event)", responsibility: chat ? "Updates the visible cited answer as events arrive." : "Reports socket liveness.", happens: [chat ? "Applies delta, citation, usage, done, and error envelopes." : "Reads the pong envelope.", "Handles close/reconnect in the transport layer."], calls: "React state", returns: chat ? "Progressive answer" : "Realtime health", guarantee: "The UI consumes versioned envelope shapes only." }),
    ];
  }

  return [
    common({ id: "request", step: "01", title: endpoint.method === "GET" ? "Start request" : "Submit command", subtitle: `${endpoint.method} ${endpoint.path}`, x: 70, y: 110, runtime: "browser", layer: "transport", symbol: endpoint.method, file: "frontend/src/api/generated.ts", method: `${endpoint.method} ${endpoint.path}`, responsibility: "Starts this contracted operation from the generated client or another API caller.", happens: ["Serializes fields defined by contracts/openapi.yaml.", isPublic ? "Calls a deliberately public route." : "Attaches a bearer token; IDs do not establish authority."], calls: "FastAPI middleware", returns: "HTTP response or operation handle", guarantee: "OpenAPI is the wire source of truth." }),
    common({ id: "scope", step: "02", title: isPublic ? "Build public scope" : "Resolve trusted scope", subtitle: isPublic ? "request dependencies" : "auth + tenant dependencies", x: 340, y: 110, runtime: "backend", layer: "api", symbol: isPublic ? "DI" : "AUTH", file: isPublic ? "backend/app/api/deps.py" : "backend/app/auth/", method: isPublic ? "FastAPI dependencies" : "get_current_user(...) ", responsibility: isPublic ? "Provides shared dependencies without requiring identity." : "Verifies identity and derives tenant, roles, and principal scope.", happens: [isPublic ? "Opens only the adapters this public route needs." : "Validates the token before endpoint code runs.", "Injects request-scoped adapters."], calls: "Config, auth, db factories", returns: isPublic ? "Request dependencies" : "CurrentUser + tenant scope", guarantee: isPublic ? "Public exposure is explicit." : "tenant_id comes from verified identity." }),
    common({ id: "handler", step: "03", title: endpoint.handler, subtitle: "FastAPI route handler", x: 610, y: 110, runtime: "backend", layer: "api", symbol: "API", responsibility: "Validates the wire shape, invokes one service, and maps its result to the contract.", happens: ["FastAPI/Pydantic rejects malformed input first.", "The router contains no SQL, retrieval, or provider logic."], calls: serviceName, returns: "Contract response or mapped error", guarantee: "The api/ → services/ → domain/ direction stays one-way." }),
    common({ id: "service", step: "04", title: serviceName, subtitle: endpoint.operationId, x: 880, y: 110, runtime: "backend", layer: "service", symbol: "SVC", file: serviceFile, method: serviceMethod, responsibility: `Owns the ${endpoint.domain.toLowerCase()} use case and coordinates rules with named adapters.`, happens: ["Applies visibility, role, transition, and domain checks.", "Passes domain types across infrastructure boundaries."], calls: boundary.title, returns: "Domain result", guarantee: "Business behavior stays out of routers." }),
    common({ id: "boundary", step: "05", x: 880, y: 375, layer: "adapter", happens: [boundary.responsibility, isWrite ? "Records the state transition before success is returned." : "Maps infrastructure output back to domain types."], ...boundary }),
    common({ id: "audit", step: "06", title: isWrite ? "Commit + audit" : "Emit audit event", subtitle: "durability + provenance", x: 610, y: 375, runtime: "postgres", layer: "adapter", symbol: "AUD", file: "backend/app/audit/", method: "AuditSink.record(...) ", responsibility: "Makes the operation explainable and aligns responses with durable state.", happens: ["Records actor, tenant, target, outcome, and correlation.", isWrite ? "Commits before reporting success." : "Captures read provenance without exposing scope."], calls: "Audit repository", returns: "Committed state + audit event", guarantee: "A required operation without its audit event fails verification." }),
    common({ id: "response", step: "07", title: "Return contract result", subtitle: "HTTP response", x: 340, y: 375, runtime: "browser", layer: "transport", symbol: "OUT", file: "contracts/openapi.yaml", method: `${endpoint.operationId} response`, responsibility: "Returns only fields and status codes defined by the contract.", happens: ["Hidden resources map to the same 404 as missing resources.", "The generated client deserializes frontend types."], calls: "React state", returns: endpoint.summary, guarantee: "Adapter/provider types cannot leak into the response." }),
  ];
}

const genericEdges: Edge[] = [
  { from: "request", to: "scope", direction: "right", x: 277, y: 176, length: 63 },
  { from: "scope", to: "handler", direction: "right", x: 547, y: 176, length: 63 },
  { from: "handler", to: "service", direction: "right", x: 817, y: 176, length: 63 },
  { from: "service", to: "boundary", direction: "down", x: 983, y: 242, length: 133 },
  { from: "boundary", to: "audit", direction: "left", x: 817, y: 441, length: 63 },
  { from: "audit", to: "response", direction: "left", x: 547, y: 441, length: 63, tone: "return" },
];

const websocketEdges: Edge[] = [
  { from: "ws-open", to: "ws-handler", direction: "right", x: 277, y: 176, length: 63 },
  { from: "ws-handler", to: "ws-owner", direction: "right", x: 547, y: 176, length: 63 },
  { from: "ws-owner", to: "ws-sub", direction: "right", x: 817, y: 176, length: 63 },
  { from: "ws-sub", to: "ws-ui", direction: "down", x: 983, y: 242, length: 133, tone: "return" },
];

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

function EdgeLine({ edge, active }: { edge: Edge; active: boolean }) {
  const vertical = edge.direction === "down" || edge.direction === "up";
  const style: CSSProperties = vertical
    ? { left: edge.x, top: edge.y, height: edge.length }
    : { left: edge.x, top: edge.y, width: edge.length };
  return (
    <div
      aria-hidden="true"
      className={`edge edge--${edge.direction} edge--${edge.tone ?? "normal"} ${active ? "is-active" : ""}`}
      style={style}
    >
      {edge.label ? <span className="edge__label">{edge.label}</span> : null}
    </div>
  );
}

function AppNode({ node, selected, active, faded, onSelect }: {
  node: FlowNode;
  selected: boolean;
  active: boolean;
  faded: boolean;
  onSelect: () => void;
}) {
  const meta = runtimeMeta[node.runtime];
  return (
    <button
      type="button"
      className={`flow-node ${selected ? "is-selected" : ""} ${active ? "is-active" : ""} ${faded ? "is-faded" : ""}`}
      style={{ left: node.x, top: node.y, "--node-color": meta.color } as CSSProperties}
      onClick={(event) => {
        event.stopPropagation();
        onSelect();
      }}
      aria-label={`${node.step}. ${node.title}. ${node.subtitle}`}
    >
      <span className="flow-node__topline">
        <span className="flow-node__step">{node.step}</span>
        <span className="flow-node__runtime">{meta.label}</span>
      </span>
      <span className="flow-node__body">
        <span className="flow-node__symbol">{node.symbol}</span>
        <span>
          <strong>{node.title}</strong>
          <small>{node.subtitle}</small>
        </span>
      </span>
      <span className="flow-node__port flow-node__port--in" />
      <span className="flow-node__port flow-node__port--out" />
      <span className="flow-node__port flow-node__port--top" />
      <span className="flow-node__port flow-node__port--bottom" />
    </button>
  );
}

export default function Home() {
  const defaultEndpoint = endpointCatalog.find((item) => item.operationId === "sendMessage") ?? endpointCatalog[0];
  const [endpointId, setEndpointId] = useState(defaultEndpoint.id);
  const [query, setQuery] = useState("");
  const [transport, setTransport] = useState<"all" | "HTTP" | "WebSocket">("all");
  const [selectedId, setSelectedId] = useState("router");
  const [hasNodeSelection, setHasNodeSelection] = useState(false);
  const [scale, setScale] = useState(0.76);
  const [pan, setPan] = useState({ x: 16, y: 20 });
  const [dragging, setDragging] = useState(false);
  const [traceIndex, setTraceIndex] = useState(-1);
  const [playing, setPlaying] = useState(false);
  const [focus, setFocus] = useState<"all" | "backend" | "data">("all");
  const dragRef = useRef({ x: 0, y: 0, panX: 0, panY: 0 });
  const viewportRef = useRef<HTMLDivElement>(null);

  const endpoint = endpointCatalog.find((item) => item.id === endpointId) ?? defaultEndpoint;
  const isDetailedChat = endpoint.operationId === "sendMessage";
  const nodes = useMemo(() => isDetailedChat ? chatNodes : genericNodes(endpoint), [endpoint, isDetailedChat]);
  const edges = isDetailedChat ? chatEdges : endpoint.transport === "WebSocket" ? websocketEdges : genericEdges;
  const traceOrder = useMemo(() => isDetailedChat ? chatTraceOrder : nodes.map((node) => node.id), [isDetailedChat, nodes]);
  const filteredEndpoints = useMemo(() => {
    const normalizeSearch = (value: string) => value.toLowerCase().replace(/[_-]/g, " ");
    const needle = normalizeSearch(query.trim());
    return endpointCatalog.filter((item) =>
      (transport === "all" || item.transport === transport) &&
      (!needle || normalizeSearch(`${item.method} ${item.path} ${item.handler} ${item.domain} ${item.summary}`).includes(needle)),
    );
  }, [query, transport]);
  const endpointGroups = useMemo(() => {
    const result = new Map<string, EndpointDefinition[]>();
    for (const item of filteredEndpoints) result.set(item.domain, [...(result.get(item.domain) ?? []), item]);
    return [...result.entries()];
  }, [filteredEndpoints]);

  const selected = useMemo(
    () => nodes.find((node) => node.id === selectedId) ?? nodes[Math.min(2, nodes.length - 1)],
    [nodes, selectedId],
  );

  const fit = useCallback(() => {
    const width = viewportRef.current?.clientWidth ?? 1200;
    const contentWidth = isDetailedChat ? 1660 : 1180;
    const nextScale = clamp((width - 48) / contentWidth, 0.42, 0.88);
    setScale(nextScale);
    setPan({ x: 22, y: 22 });
  }, [isDetailedChat]);

  useEffect(() => {
    fit();
  }, [fit]);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      setTraceIndex((current) => {
        const next = current + 1;
        if (next >= traceOrder.length) {
          setPlaying(false);
          return traceOrder.length - 1;
        }
        setSelectedId(traceOrder[next]);
        return next;
      });
    }, 860);
    return () => window.clearInterval(timer);
  }, [nodes, playing, traceOrder]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select")) return;
      if (event.key === "ArrowRight" || event.key.toLowerCase() === "j") {
        event.preventDefault();
        setHasNodeSelection(true);
        setTraceIndex((current) => {
          const next = Math.min(traceOrder.length - 1, current + 1);
          setSelectedId(traceOrder[next]);
          return next;
        });
      }
      if (event.key === "ArrowLeft" || event.key.toLowerCase() === "k") {
        event.preventDefault();
        setHasNodeSelection(true);
        setTraceIndex((current) => {
          const next = Math.max(0, current - 1);
          setSelectedId(traceOrder[next]);
          return next;
        });
      }
      if (event.key === " ") {
        event.preventDefault();
        setPlaying((value) => !value);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [nodes, traceOrder]);

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    if ((event.target as HTMLElement).closest("button")) return;
    setDragging(true);
    dragRef.current = { x: event.clientX, y: event.clientY, panX: pan.x, panY: pan.y };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    setPan({
      x: dragRef.current.panX + event.clientX - dragRef.current.x,
      y: dragRef.current.panY + event.clientY - dragRef.current.y,
    });
  };

  const onWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      setScale((value) => clamp(value - event.deltaY * 0.001, 0.45, 1.3));
      return;
    }
    setPan((value) => ({ x: value.x - event.deltaX, y: value.y - event.deltaY }));
  };

  const activeId = traceIndex >= 0 ? traceOrder[traceIndex] : null;
  const activeNodeIndex = activeId ? nodes.findIndex((node) => node.id === activeId) : -1;

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand__mark">LC</span>
          <span>
            <strong>Lumen Backend Explorer</strong>
            <small>code-to-runtime map</small>
          </span>
        </div>
        <div className="endpoint-picker" aria-label="Selected endpoint">
          <span className={`method-badge method-badge--${endpoint.method.toLowerCase()}`}>{endpoint.method}</span>
          <span>{endpoint.path.replace(/\{[^}]+Id\}/g, ":id").replace(/\{([^}]+)\}/g, ":$1")}</span>
          <span className="version-chip">{endpoint.transport}</span>
        </div>
        <div className="topbar__meta">
          <span className="status-dot" />
          114 endpoints · code-grounded
        </div>
      </header>

      <section className="workspace">
        <aside className="catalog">
          <div className="catalog__header">
            <p className="eyebrow">Endpoint catalog</p>
            <strong>114 endpoints</strong>
            <span>112 HTTP · 2 WebSocket</span>
          </div>
          <label className="search-box">
            <span aria-hidden="true">⌕</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search endpoints" aria-label="Search endpoints" />
          </label>
          <div className="transport-filter" role="group" aria-label="Filter transport">
            {(["all", "HTTP", "WebSocket"] as const).map((value) => (
              <button key={value} type="button" className={transport === value ? "is-active" : ""} onClick={() => setTransport(value)}>
                {value === "all" ? "All" : value === "WebSocket" ? "WS" : value}
              </button>
            ))}
          </div>
          <div className="catalog__list">
            {endpointGroups.map(([domain, items]) => (
              <section className="endpoint-group" key={domain}>
                <h2>{domain}<span>{items.length}</span></h2>
                {items.map((item) => (
                  <button key={item.id} type="button" className={`endpoint-row ${item.id === endpoint.id ? "is-selected" : ""}`} onClick={() => {
                    setEndpointId(item.id);
                    setSelectedId(item.operationId === "sendMessage" ? "router" : item.transport === "WebSocket" ? "ws-handler" : "handler");
                    setHasNodeSelection(false);
                    setTraceIndex(-1);
                    setPlaying(false);
                  }}>
                    <span className={`mini-method mini-method--${item.method.toLowerCase()}`}>{item.method}</span>
                    <span><strong>{item.path}</strong><small>{item.handler}</small></span>
                  </button>
                ))}
              </section>
            ))}
          </div>
        </aside>
        <div className="canvas-panel">
          <div className="canvas-heading">
            <div>
              <p className="eyebrow">{endpoint.domain} · {endpoint.transport}</p>
              <h1>{isDetailedChat ? "What happens after “Send”?" : endpoint.summary}</h1>
              <p>{isDetailedChat ? "Follow one chat turn from the browser, through the Python call stack, into retrieval and back over WebSocket." : <>Trace the call stack, encapsulation boundaries, and runtime containers for <code>{endpoint.operationId}</code>.</>}</p>
            </div>
            <div className="view-switch" role="group" aria-label="Focus the diagram">
              {(["all", "backend", "data"] as const).map((value) => (
                <button key={value} type="button" className={focus === value ? "is-active" : ""} onClick={() => setFocus(value)}>
                  {value === "all" ? "Full flow" : value === "backend" ? "Call stack" : "Containers"}
                </button>
              ))}
            </div>
          </div>

          <div className="canvas-toolbar" aria-label="Flow controls">
            <div className="play-controls">
              <button
                type="button"
                className="primary-control"
                onClick={() => {
                  if (traceIndex >= traceOrder.length - 1) setTraceIndex(-1);
                  setPlaying((value) => !value);
                }}
              >
                <span aria-hidden="true">{playing ? "Ⅱ" : "▶"}</span>
                {playing ? "Pause trace" : traceIndex >= 0 ? "Resume trace" : "Play trace"}
              </button>
              <button type="button" aria-label="Previous step" title="Previous step (K / Left arrow)" onClick={() => {
                setPlaying(false);
                setTraceIndex((current) => {
                  const next = Math.max(0, current - 1);
                  setSelectedId(traceOrder[next]);
                  return next;
                });
              }}>←</button>
              <button type="button" aria-label="Next step" title="Next step (J / Right arrow)" onClick={() => {
                setPlaying(false);
                setTraceIndex((current) => {
                  const next = Math.min(traceOrder.length - 1, current + 1);
                  setSelectedId(traceOrder[next]);
                  return next;
                });
              }}>→</button>
              <span className="trace-counter">{traceIndex < 0 ? "ready" : `${traceIndex + 1} / ${traceOrder.length}`}</span>
            </div>
            <div className="zoom-controls">
              <button type="button" aria-label="Zoom out" title="Zoom out" onClick={() => setScale((value) => clamp(value - 0.1, 0.45, 1.3))}>−</button>
              <span>{Math.round(scale * 100)}%</span>
              <button type="button" aria-label="Zoom in" title="Zoom in" onClick={() => setScale((value) => clamp(value + 0.1, 0.45, 1.3))}>+</button>
              <button type="button" className="fit-control" onClick={fit}>Fit</button>
            </div>
          </div>

          <div
            ref={viewportRef}
            className={`canvas-viewport ${dragging ? "is-dragging" : ""}`}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={() => setDragging(false)}
            onPointerCancel={() => setDragging(false)}
            onWheel={onWheel}
            aria-label="Interactive backend data-flow canvas. Drag to pan; hold Control and scroll to zoom."
          >
            <div className="lane-labels" aria-hidden="true">
              <span style={{ top: 96 }}>REQUEST PATH</span>
              {isDetailedChat ? <>
                <span style={{ top: 351 }}>ANSWER TASK</span>
                <span style={{ top: 631 }}>ADAPTERS & DATA</span>
                <span style={{ top: 846 }}>STREAM RETURN</span>
              </> : <span style={{ top: 366 }}>INFRASTRUCTURE & RETURN</span>}
            </div>
            <div
              className={`flow-canvas ${isDetailedChat ? "flow-canvas--chat" : "flow-canvas--compact"}`}
              style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})` }}
              onClick={() => {
                setSelectedId(nodes[0].id);
                setHasNodeSelection(false);
              }}
            >
              <div className="boundary boundary--request"><span>FastAPI request lifecycle</span></div>
              {isDetailedChat ? <div className="boundary boundary--answer"><span>Tracked async answer producer</span></div> : null}
              <div className="boundary boundary--data"><span>External & persistence boundaries</span></div>
              {edges.map((edge, index) => (
                <EdgeLine
                  key={`${edge.from}-${edge.to}-${index}`}
                  edge={edge}
                  active={activeId === edge.from || activeId === edge.to}
                />
              ))}
              {nodes.map((node, index) => {
                const faded =
                  (focus === "backend" && !["api", "service"].includes(node.layer)) ||
                  (focus === "data" && !["adapter", "external"].includes(node.layer));
                return (
                  <AppNode
                    key={node.id}
                    node={node}
                    selected={selectedId === node.id}
                    active={activeId === node.id || (activeNodeIndex === index && traceIndex >= 0)}
                    faded={faded}
                    onSelect={() => {
                      setPlaying(false);
                      setSelectedId(node.id);
                      setHasNodeSelection(true);
                    }}
                  />
                );
              })}
              {isDetailedChat ? <div className="loop-note">
                <strong>Bounded agent loop</strong>
                <span>model → tool intent → governed result → model</span>
              </div> : null}
              {isDetailedChat ? <div className="commit-note">
                <strong>Commit before done</strong>
                <span>The final stream event points at durable data.</span>
              </div> : null}
            </div>
            {hasNodeSelection ? <section className="node-quicklook" aria-live="polite">
              <div className="node-quicklook__header">
                <span>STEP {selected.step} · {runtimeMeta[selected.runtime].label}</span>
                <button type="button" aria-label="Close node details" onClick={() => setHasNodeSelection(false)}>×</button>
              </div>
              <h3>{selected.title}</h3>
              <p>{selected.responsibility}</p>
              <code>{selected.method}</code>
              <small>Full inputs, outputs, guarantees, and source are in the inspector.</small>
            </section> : null}
            <div className="canvas-hint">Drag canvas · Ctrl + scroll to zoom · J/K to step · Space to play</div>
          </div>
        </div>

        <aside className="inspector" aria-live="polite">
          <div className="inspector__header">
            <div className="inspector__step">STEP {selected.step}</div>
            <span className="runtime-pill" style={{ "--pill-color": runtimeMeta[selected.runtime].color } as CSSProperties}>
              {runtimeMeta[selected.runtime].label}
            </span>
          </div>
          <h2>{selected.title}</h2>
          <p className="inspector__lede">{selected.responsibility}</p>

          <div className="code-card">
            <span>Source</span>
            <code>{selected.file}</code>
            <span>Call</span>
            <code>{selected.method}</code>
          </div>

          <div className="detail-section">
            <h3>What happens here</h3>
            <ol>
              {selected.happens.map((item) => <li key={item}>{item}</li>)}
            </ol>
          </div>

          <div className="io-grid">
            <div>
              <span>Calls</span>
              <p>{selected.calls}</p>
            </div>
            <div>
              <span>Returns</span>
              <p>{selected.returns}</p>
            </div>
          </div>

          <div className="guarantee-card">
            <span className="guarantee-card__icon">✓</span>
            <div>
              <span>Boundary guarantee</span>
              <p>{selected.guarantee}</p>
            </div>
          </div>

          <div className="encapsulation-card">
            <span>Encapsulation</span>
            <strong>{layerLabels[selected.layer]}</strong>
            <p>{runtimeMeta[selected.runtime].sub}</p>
          </div>

          <div className="legend">
            <h3>Runtime key</h3>
            <div className="legend__grid">
              {(Object.entries(runtimeMeta) as [Runtime, (typeof runtimeMeta)[Runtime]][]).map(([key, meta]) => (
                <button key={key} type="button" onClick={() => {
                  const first = nodes.find((node) => node.runtime === key);
                  if (first) setSelectedId(first.id);
                }}>
                  <span style={{ background: meta.color }} />
                  {meta.label}
                </button>
              ))}
            </div>
          </div>
        </aside>
      </section>
    </main>
  );
}
