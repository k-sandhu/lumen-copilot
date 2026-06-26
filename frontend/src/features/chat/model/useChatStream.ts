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
import type { WsClientOptions, WsConnectionState, WsEnvelope } from '@/api';
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
  onDone,
}: UseChatStreamOptions): UseChatStreamResult {
  const [state, dispatch] = useReducer(reducer, initialStreamState);
  const [connection, setConnection] = useState<WsConnectionState>('closed');
  const clientRef = useRef<StreamSocket | null>(null);
  const watchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);
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

  const cancel = useCallback(() => {
    terminalRef.current = true;
    clearWatchdog();
    clientRef.current?.close();
    clientRef.current = null;
  }, [clearWatchdog]);

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

    const client = makeClient({
      path: `/chat/${streamId}`,
      onStateChange: (next) => {
        setConnection(next);
        if (next === 'open') {
          armWatchdog();
        }
        // Socket dropped mid-stream with no terminal envelope → terminal error.
        if (next === 'closed' && !terminalRef.current) {
          clearWatchdog();
          terminalRef.current = true;
          dispatch({ kind: 'disconnect' });
        }
      },
      onEnvelope: (envelope) => {
        // Only the matching stream's envelopes (defensive — one socket per id).
        if (envelope.streamId !== streamId) return;
        clearWatchdog();
        dispatch({ kind: 'envelope', envelope });
        if (envelope.type === 'done') {
          terminalRef.current = true;
          clientRef.current?.close();
          onDoneRef.current?.();
        }
        if (envelope.type === 'error') {
          terminalRef.current = true;
          clientRef.current?.close();
        }
        if (envelope.type === 'delta' && !terminalRef.current) {
          armWatchdog();
        }
      },
    });
    clientRef.current = client;
    client.connect();

    return () => {
      // Unmount / streamId change: stop the socket (cancels pending reconnect).
      terminalRef.current = true;
      clearWatchdog();
      client.close();
      clientRef.current = null;
    };
  }, [streamId, makeClient, idleTimeoutMs, clearWatchdog]);

  return { ...state, connection, cancel };
}
