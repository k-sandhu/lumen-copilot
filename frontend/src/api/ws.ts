/**
 * Typed WebSocket client — part of the api/ boundary (ADR-0004). The ONLY place
 * the SPA opens a socket to the backend. Features consume the
 * `useHealthStream` hook (features/system-status), never this class directly.
 *
 * Implements the envelope contract (contracts/websocket-envelopes.schema.json):
 * messages are envelopes discriminated by `type` (start | delta | event | done
 * | error) with `streamId` + monotonic `seq`. Lifecycle:
 *   start -> (delta | event)* -> done | error
 *
 * Connection lifecycle (connecting | open | closing | closed) is surfaced so the
 * UI can render it. Reconnect uses exponential backoff with jitter and is
 * cancellable (close() stops the socket AND any pending reconnect).
 */
import { WS_BASE_URL } from './env';
import { getAccessToken } from './token';
import type { WsEnvelope } from './types';

export type WsConnectionState = 'connecting' | 'open' | 'closing' | 'closed';

export interface WsClientOptions {
  /**
   * Path appended to VITE_WS_BASE_URL (e.g. "/health" -> ".../ws/health").
   */
  path: string;
  /** Called for every well-formed envelope received. */
  onEnvelope?: (envelope: WsEnvelope) => void;
  /** Called whenever the connection state changes. */
  onStateChange?: (state: WsConnectionState) => void;
  /** Called when a frame can't be parsed into an envelope (observability). */
  onMalformed?: (raw: string, error: unknown) => void;
  /** Backoff base in ms (default 500). */
  backoffBaseMs?: number;
  /** Backoff ceiling in ms (default 10_000). */
  backoffMaxMs?: number;
  /** Override base URL resolution (tests). Defaults to WS_BASE_URL. */
  baseUrl?: string;
  /**
   * Opt out of carrying the bearer access token on the handshake (for public
   * streams). Authenticated streams (the default) append `?access_token=…`.
   */
  tokenless?: boolean;
}

const ENVELOPE_TYPES = new Set(['start', 'delta', 'event', 'done', 'error']);

/** Narrow unknown parsed JSON to a WsEnvelope, validating the discriminator. */
export function parseEnvelope(raw: string): WsEnvelope {
  const data: unknown = JSON.parse(raw);
  if (typeof data !== 'object' || data === null) {
    throw new Error('Envelope is not an object');
  }
  const obj = data as Record<string, unknown>;
  if (typeof obj.type !== 'string' || !ENVELOPE_TYPES.has(obj.type)) {
    throw new Error(`Unknown envelope type: ${String(obj.type)}`);
  }
  if (typeof obj.streamId !== 'string' || typeof obj.seq !== 'number') {
    throw new Error('Envelope missing streamId/seq');
  }
  // The discriminated union is validated structurally above; the per-type
  // payload shapes are feature-defined (kept as unknown in types.ts).
  return data as WsEnvelope;
}

/**
 * Resolve a ws:// or wss:// URL from a same-origin base path. In the browser we
 * derive scheme + host from `location`; tests can inject an absolute base.
 *
 * AUTH (issue #48): the browser WebSocket API cannot set an `Authorization`
 * header on the handshake, so the bearer access token is appended as an
 * `access_token` query param when present — the backend validates it in
 * `auth/` exactly as it validates the REST bearer (spec 0004 §2.3 / INV-4).
 */
export function resolveWsUrl(base: string, path: string, token?: string | null): string {
  const suffix = path.startsWith('/') ? path : `/${path}`;
  const query = token ? `?access_token=${encodeURIComponent(token)}` : '';

  // Already absolute (e.g. ws://host/...): just append the path.
  if (base.startsWith('ws://') || base.startsWith('wss://')) {
    return `${base}${suffix}${query}`;
  }

  // Same-origin path like "/ws": derive from the page location.
  const loc = typeof location !== 'undefined' ? location : undefined;
  const scheme = loc && loc.protocol === 'https:' ? 'wss' : 'ws';
  const host = loc?.host ?? 'localhost:5173';
  const normalizedBase = base.startsWith('/') ? base : `/${base}`;
  return `${scheme}://${host}${normalizedBase}${suffix}${query}`;
}

export class WsClient {
  private socket: WebSocket | null = null;
  private state: WsConnectionState = 'closed';
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private manuallyClosed = false;

  private readonly opts: Required<Pick<WsClientOptions, 'backoffBaseMs' | 'backoffMaxMs'>> &
    WsClientOptions;

  constructor(options: WsClientOptions) {
    this.opts = {
      backoffBaseMs: 500,
      backoffMaxMs: 10_000,
      ...options,
    };
  }

  /**
   * Build the handshake URL at connect time so the CURRENT access token is used
   * (a silent refresh may have replaced it since construction). A per-client
   * `tokenless` flag opts public streams out of carrying a token.
   */
  private buildUrl(): string {
    const token = this.opts.tokenless ? null : getAccessToken();
    return resolveWsUrl(this.opts.baseUrl ?? WS_BASE_URL, this.opts.path, token);
  }

  getState(): WsConnectionState {
    return this.state;
  }

  /** Open the socket. Idempotent while already connecting/open. */
  connect(): void {
    if (this.state === 'connecting' || this.state === 'open') return;
    this.manuallyClosed = false;
    this.open();
  }

  /** Close the socket and CANCEL any pending reconnect (quality bar: cancellable). */
  close(): void {
    this.manuallyClosed = true;
    this.clearReconnect();
    if (this.socket) {
      this.setState('closing');
      this.socket.close();
      this.socket = null;
    } else {
      this.setState('closed');
    }
  }

  /** Send a client envelope (e.g. a ping/control message). No-op if not open. */
  send(envelope: WsEnvelope): void {
    if (this.socket && this.state === 'open') {
      this.socket.send(JSON.stringify(envelope));
    }
  }

  private open(): void {
    this.setState('connecting');
    let socket: WebSocket;
    try {
      socket = new WebSocket(this.buildUrl());
    } catch {
      // Construction can throw on a malformed URL — treat as a closed cycle.
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.reconnectAttempts = 0;
      this.setState('open');
    };

    socket.onmessage = (event: MessageEvent) => {
      const raw = typeof event.data === 'string' ? event.data : '';
      try {
        const envelope = parseEnvelope(raw);
        this.opts.onEnvelope?.(envelope);
      } catch (error) {
        this.opts.onMalformed?.(raw, error);
      }
    };

    socket.onerror = () => {
      // onerror is followed by onclose; reconnect is scheduled there.
    };

    socket.onclose = () => {
      this.socket = null;
      if (this.manuallyClosed) {
        this.setState('closed');
        return;
      }
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    this.setState('closed');
    if (this.manuallyClosed) return;

    const { backoffBaseMs, backoffMaxMs } = this.opts;
    // Exponential backoff with full jitter.
    const exp = Math.min(backoffMaxMs, backoffBaseMs * 2 ** this.reconnectAttempts);
    const delay = Math.random() * exp;
    this.reconnectAttempts += 1;

    this.clearReconnect();
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.manuallyClosed) this.open();
    }, delay);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private setState(next: WsConnectionState): void {
    if (this.state === next) return;
    this.state = next;
    this.opts.onStateChange?.(next);
  }
}
