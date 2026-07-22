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

type Runtime = "browser" | "backend" | "postgres" | "redis" | "search" | "provider";
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

const nodes: FlowNode[] = [
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

const edges: Edge[] = [
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
  provider: { label: "Model provider", sub: "via LiteLLM", color: "#7dd9ff" },
};

const layerLabels: Record<Layer, string> = {
  transport: "Transport",
  api: "API layer",
  service: "Service orchestration",
  adapter: "Adapter boundary",
  external: "External system",
};

const traceOrder = [
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
    </button>
  );
}

export default function Home() {
  const [selectedId, setSelectedId] = useState("router");
  const [scale, setScale] = useState(0.76);
  const [pan, setPan] = useState({ x: 16, y: 20 });
  const [dragging, setDragging] = useState(false);
  const [traceIndex, setTraceIndex] = useState(-1);
  const [playing, setPlaying] = useState(false);
  const [focus, setFocus] = useState<"all" | "backend" | "data">("all");
  const dragRef = useRef({ x: 0, y: 0, panX: 0, panY: 0 });
  const viewportRef = useRef<HTMLDivElement>(null);

  const selected = useMemo(
    () => nodes.find((node) => node.id === selectedId) ?? nodes[2],
    [selectedId],
  );

  const fit = useCallback(() => {
    const width = viewportRef.current?.clientWidth ?? 1200;
    const nextScale = clamp((width - 48) / 1660, 0.52, 0.88);
    setScale(nextScale);
    setPan({ x: 22, y: 22 });
  }, []);

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
  }, [playing]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select")) return;
      if (event.key === "ArrowRight" || event.key.toLowerCase() === "j") {
        event.preventDefault();
        setTraceIndex((current) => {
          const next = Math.min(traceOrder.length - 1, current + 1);
          setSelectedId(traceOrder[next]);
          return next;
        });
      }
      if (event.key === "ArrowLeft" || event.key.toLowerCase() === "k") {
        event.preventDefault();
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
  }, []);

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
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
          <span className="method-badge">POST</span>
          <span>/api/v1/chat/sessions/:id/messages</span>
          <span className="version-chip">first slice</span>
        </div>
        <div className="topbar__meta">
          <span className="status-dot" />
          traced from main · 38d4dcc
        </div>
      </header>

      <section className="workspace">
        <div className="canvas-panel">
          <div className="canvas-heading">
            <div>
              <p className="eyebrow">Endpoint flow 01</p>
              <h1>What happens after “Send”?</h1>
              <p>Follow one chat turn from the browser, through the Python call stack, into retrieval and back over WebSocket.</p>
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
              <span style={{ top: 351 }}>ANSWER TASK</span>
              <span style={{ top: 631 }}>ADAPTERS & DATA</span>
              <span style={{ top: 846 }}>STREAM RETURN</span>
            </div>
            <div
              className="flow-canvas"
              style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})` }}
              onClick={() => setSelectedId("router")}
            >
              <div className="boundary boundary--request"><span>FastAPI request lifecycle</span></div>
              <div className="boundary boundary--answer"><span>Tracked async answer producer</span></div>
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
                    }}
                  />
                );
              })}
              <div className="loop-note">
                <strong>Bounded agent loop</strong>
                <span>model → tool intent → governed result → model</span>
              </div>
              <div className="commit-note">
                <strong>Commit before done</strong>
                <span>The final stream event points at durable data.</span>
              </div>
            </div>
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
