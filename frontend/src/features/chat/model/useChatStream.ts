/**
 * React hook that subscribes to one chat answer stream over the typed WS client
 * (api/ boundary — features never touch the raw socket; frontend/AGENTS.md) and
 * folds its envelopes through the pure `streamReducer`.
 *
 * Given a `stream_id` (returned by POST .../messages), it opens the WS at
 * `/chat/<streamId>`, accumulates delta tokens + citations + tool activity, and
 * settles on the terminal `done`/`error`. An unexpected disconnect (socket
 * closes mid-stream with no terminal envelope), or an open stream that exceeds
 * its first-token/idle watchdog, is itself treated as a terminal error with a
 * retry affordance (AC-5). Generation is cancellable via `cancel()`
 * (frontend/AGENTS.md "Streaming UX: cancellable").
 *
 * The WsClient is injectable (`makeClient`) so component/hook tests drive a fake
 * socket without a network (ADR-0006 Phase 1: test against the client/mocks).
 */
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { WsClient } from '@/api';
import type { ChatDoneData, WsClientOptions, WsConnectionState, WsEnvelope } from '@/api';
import {
  initialStreamState,
  reduceStream,
  terminateWithDisconnect,
  type StreamState,
} from './streamReducer';

/** Minimal surface the hook needs from a WS client (lets tests fake it). */
export interface StreamSocket {
  connect(): void;
  close(): void;
  getState(): WsConnectionState;
}

export type ClientFactory = (options: WsClientOptions) => StreamSocket;

let defaultFactory: ClientFactory = (options) => new WsClient(options);
const DEFAULT_IDLE_TIMEOUT_MS = 20_000;
// Fallback for how long the socket stays open AFTER a `done(pendingSuggestions=true)`
// to receive the one post-terminal `event:suggestions` (#489). Used ONLY when the
// server does not declare its own grace on the terminal (`done.suggestionsGraceMs`,
// #489/BE-5) — the server value is the source of truth, since a server grace larger
// than this default would otherwise cut off a slow-but-arriving suggestion. A
// client-side backstop regardless: the server drops the subscription (and thus the
// socket) at its own grace, so this just bounds the wait if that close never arrives.
const DEFAULT_SUGGESTIONS_GRACE_MS = 15_000;

/**
 * Test seam: swap the WS client factory used when a caller does NOT pass an
 * explicit `makeClient` (e.g. the integration test driving the whole ChatView).
 * Returns a restore function. No effect on production (real `WsClient`).
 */
export function setDefaultStreamClientFactory(factory: ClientFactory): () => void {
  const previous = defaultFactory;
  defaultFactory = factory;
  return () => {
    defaultFactory = previous;
  };
}

export interface UseChatStreamOptions {
  /** The stream to subscribe to; null = idle (no socket). */
  streamId: string | null;
  /** Override the WS client factory (tests inject a fake). */
  makeClient?: ClientFactory;
  /** Override the first-token/idle watchdog deadline (tests inject a short timeout). */
  idleTimeoutMs?: number;
  /**
   * Override the post-terminal suggestions grace (#489; tests inject a short one).
   * How long the socket is held open after a `done(pendingSuggestions=true)` for
   * the one trailing `event:suggestions`.
   */
  suggestionsGraceMs?: number;
  /** Called once when the stream reaches `done` (caller reloads history). */
  onDone?: () => void;
}

type Action =
  | { kind: 'reset' }
  | { kind: 'envelope'; envelope: WsEnvelope }
  | { kind: 'disconnect' };

function reducer(state: StreamState, action: Action): StreamState {
  switch (action.kind) {
    case 'reset':
      return initialStreamState;
    case 'envelope':
      return reduceStream(state, action.envelope);
    case 'disconnect':
      return terminateWithDisconnect(state);
  }
}

export interface UseChatStreamResult extends StreamState {
  connection: WsConnectionState;
  /** Cancel the in-flight stream (stop button / navigation). */
  cancel: () => void;
}

export function useChatStream({
  streamId,
  makeClient = defaultFactory,
  idleTimeoutMs = DEFAULT_IDLE_TIMEOUT_MS,
  suggestionsGraceMs = DEFAULT_SUGGESTIONS_GRACE_MS,
  onDone,
}: UseChatStreamOptions): UseChatStreamResult {
  const [state, dispatch] = useReducer(reducer, initialStreamState);
  const [connection, setConnection] = useState<WsConnectionState>('closed');
  const clientRef = useRef<StreamSocket | null>(null);
  const watchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The post-terminal suggestions grace timer (#489): armed on a
  // `done(pendingSuggestions)`, cleared when the suggestions arrive / the socket
  // closes / on unmount.
  const graceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Track terminal-ness across the socket's onclose without re-subscribing.
  const terminalRef = useRef(false);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current !== null) {
      clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
  }, []);

  const clearGrace = useCallback(() => {
    if (graceRef.current !== null) {
      clearTimeout(graceRef.current);
      graceRef.current = null;
    }
  }, []);

  const cancel = useCallback(() => {
    terminalRef.current = true;
    clearWatchdog();
    clearGrace();
    clientRef.current?.close();
    clientRef.current = null;
  }, [clearWatchdog, clearGrace]);

  useEffect(() => {
    if (!streamId) {
      clearWatchdog();
      setConnection('closed');
      return;
    }

    dispatch({ kind: 'reset' });
    terminalRef.current = false;

    const armWatchdog = () => {
      clearWatchdog();
      watchdogRef.current = setTimeout(() => {
        watchdogRef.current = null;
        if (terminalRef.current) return;
        terminalRef.current = true;
        dispatch({ kind: 'disconnect' });
        clientRef.current?.close();
      }, idleTimeoutMs);
    };

    const armSuggestionsGrace = (graceMs: number) => {
      // The stream is already terminal (UI settled on `done`); hold the socket
      // open a bounded while for the one post-terminal `event:suggestions` (#489).
      // If it never comes, close the socket — the terminal stands. `graceMs` is the
      // server's declared grace when present (#489/BE-5), else the client default.
      clearGrace();
      graceRef.current = setTimeout(() => {
        graceRef.current = null;
        clientRef.current?.close();
      }, graceMs);
    };

    const client = makeClient({
      path: `/chat/${streamId}`,
      onStateChange: (next) => {
        setConnection(next);
        if (next === 'open') {
          armWatchdog();
        }
        // Socket dropped mid-stream with no terminal envelope → terminal error.
        // Close the client too (like every other terminal path: done/error/
        // watchdog/cancel). Without this, WsClient.manuallyClosed stays false and
        // its pending reconnect keeps firing behind a terminal "Connection lost"
        // UI — a zombie socket re-authenticating every ~10s until unmount/Retry
        // (#273). AC-5 treats any mid-stream disconnect as terminal. A close while
        // ALREADY terminal (e.g. the server ending the post-terminal grace, #489)
        // is expected — do NOT synthesize a disconnect; just drop the grace timer.
        if (next === 'closed') {
          if (terminalRef.current) {
            clearGrace();
          } else {
            clearWatchdog();
            terminalRef.current = true;
            dispatch({ kind: 'disconnect' });
            clientRef.current?.close();
          }
        }
      },
      onEnvelope: (envelope) => {
        // Only the matching stream's envelopes (defensive — one socket per id).
        if (envelope.streamId !== streamId) return;
        clearWatchdog();
        dispatch({ kind: 'envelope', envelope });
        if (envelope.type === 'done') {
          terminalRef.current = true;
          onDoneRef.current?.();
          // #489: a `done` that declares `pendingSuggestions` keeps the socket
          // open for the one trailing `event:suggestions` (the UI already settled
          // as terminal via the dispatch above). Otherwise close now, as before.
          const doneData = envelope.data as ChatDoneData | undefined;
          const pending = doneData?.pendingSuggestions === true;
          if (pending) {
            // #489/BE-5: honour the server's declared grace when present so the
            // wait matches the server's own relay window (one source of truth);
            // a server grace larger than the client default must not be cut off.
            // Fall back to the client default only when the server omits it.
            const serverGraceMs = doneData?.suggestionsGraceMs;
            const graceMs =
              typeof serverGraceMs === 'number' && serverGraceMs >= 0
                ? serverGraceMs
                : suggestionsGraceMs;
            armSuggestionsGrace(graceMs);
          } else {
            clientRef.current?.close();
          }
          return;
        }
        if (envelope.type === 'error') {
          terminalRef.current = true;
          clearGrace();
          clientRef.current?.close();
          return;
        }
        // A post-terminal `event:suggestions` arrived within the grace window
        // (#489): the reducer applied it above; we got what we waited for, so
        // close the socket now and drop the grace timer.
        if (terminalRef.current) {
          if (envelope.type === 'event' && envelope.name === 'suggestions') {
            clearGrace();
            clientRef.current?.close();
          }
          return;
        }
        // Re-arm after EVERY non-terminal envelope (start / delta / event), not
        // just delta: the lifecycle is start → (delta|event)* → done|error, so a
        // `start` (or `event`) that is the last thing before the backend stalls
        // would otherwise leave the watchdog cleared with no timer to synthesize
        // a terminal `stream_disconnected` (#159).
        armWatchdog();
      },
    });
    clientRef.current = client;
    client.connect();

    return () => {
      // Unmount / streamId change: stop the socket (cancels pending reconnect)
      // and drop any pending post-terminal grace timer (#489).
      terminalRef.current = true;
      clearWatchdog();
      clearGrace();
      client.close();
      clientRef.current = null;
    };
  }, [streamId, makeClient, idleTimeoutMs, suggestionsGraceMs, clearWatchdog, clearGrace]);

  return { ...state, connection, cancel };
}
