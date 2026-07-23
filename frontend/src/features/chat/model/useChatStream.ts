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
// Text deltas are accumulated at the hook INGRESS and flushed to reducer state
// once per animation frame (#493): render frequency is then bounded by the
// display refresh, not by provider chunk size (~800 deltas for a 2k-token
// answer → ~800 commits before, ~one-per-frame after). Non-DOM/SSR runtimes
// without requestAnimationFrame fall back to this timer cadence (~display rate).
const DELTA_FLUSH_INTERVAL_MS = 40;
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
  | { kind: 'flush'; envelopes: WsEnvelope[] }
  | { kind: 'disconnect' };

function reducer(state: StreamState, action: Action): StreamState {
  switch (action.kind) {
    case 'reset':
      return initialStreamState;
    case 'flush':
      // Fold one or more envelopes through the pure reducer in a SINGLE React
      // commit (#493 delta batching). Buffered text deltas are collapsed here; a
      // trailing non-text envelope is appended so it applies right after the text
      // it follows, preserving exact order (AC-3). `reduceStream` keeps its own
      // seq-dedupe / terminal / retract handling, so folding is byte-identical to
      // dispatching each envelope separately — only the render count differs.
      return action.envelopes.reduce(reduceStream, state);
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

  // --- Delta batching (#493) -------------------------------------------------
  // Raw text `delta` envelopes accumulate here until a scheduled flush folds them
  // through the reducer in ONE commit. A non-text envelope flushes this buffer
  // FIRST (see onEnvelope) so ordering is exact.
  const pendingRef = useRef<WsEnvelope[]>([]);
  // The scheduled flush handle (rAF or timer) with its matching canceller.
  const flushHandleRef = useRef<{ cancel: () => void } | null>(null);

  const cancelScheduledFlush = useCallback(() => {
    if (flushHandleRef.current !== null) {
      flushHandleRef.current.cancel();
      flushHandleRef.current = null;
    }
  }, []);

  // Commit the buffered text deltas (if any) as a single reducer flush.
  const flushPendingText = useCallback(() => {
    cancelScheduledFlush();
    const buffered = pendingRef.current;
    if (buffered.length === 0) return;
    pendingRef.current = [];
    dispatch({ kind: 'flush', envelopes: buffered });
  }, [cancelScheduledFlush]);

  // Drop the buffer WITHOUT committing — teardown paths (unmount / streamId
  // change / cancel) must not let a trailing frame fire after the socket is gone
  // (AC-4: no state update on an unmounted component, no act warning).
  const discardPendingText = useCallback(() => {
    cancelScheduledFlush();
    pendingRef.current = [];
  }, [cancelScheduledFlush]);

  // Schedule a single coalesced flush on the next animation frame (timer fallback
  // where rAF is unavailable). Idempotent while one is already pending.
  const scheduleFlush = useCallback(() => {
    if (flushHandleRef.current !== null) return;
    if (typeof requestAnimationFrame === 'function') {
      const id = requestAnimationFrame(() => {
        flushHandleRef.current = null;
        flushPendingText();
      });
      flushHandleRef.current = { cancel: () => cancelAnimationFrame(id) };
    } else {
      const id = setTimeout(() => {
        flushHandleRef.current = null;
        flushPendingText();
      }, DELTA_FLUSH_INTERVAL_MS);
      flushHandleRef.current = { cancel: () => clearTimeout(id) };
    }
  }, [flushPendingText]);

  const cancel = useCallback(() => {
    terminalRef.current = true;
    clearWatchdog();
    clearGrace();
    discardPendingText();
    clientRef.current?.close();
    clientRef.current = null;
  }, [clearWatchdog, clearGrace, discardPendingText]);

  useEffect(() => {
    if (!streamId) {
      clearWatchdog();
      setConnection('closed');
      return;
    }

    dispatch({ kind: 'reset' });
    discardPendingText();
    terminalRef.current = false;

    const armWatchdog = () => {
      clearWatchdog();
      watchdogRef.current = setTimeout(() => {
        watchdogRef.current = null;
        if (terminalRef.current) return;
        terminalRef.current = true;
        // Commit any buffered partial text before the synthetic terminal so the
        // disconnect banner shows how far the answer got (AC-4 preserves text).
        flushPendingText();
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
            // Already terminal (e.g. server ending the post-terminal grace): no
            // buffered text can be pending, but drop any scheduled frame so it
            // cannot fire after the socket is gone.
            discardPendingText();
          } else {
            clearWatchdog();
            terminalRef.current = true;
            // Flush the partial answer BEFORE the synthetic terminal so a
            // mid-stream drop preserves the text streamed so far (AC-4).
            flushPendingText();
            dispatch({ kind: 'disconnect' });
            clientRef.current?.close();
          }
        }
      },
      onEnvelope: (envelope) => {
        // Only the matching stream's envelopes (defensive — one socket per id).
        if (envelope.streamId !== streamId) return;
        clearWatchdog();

        // TEXT DELTAS (#493): accumulate and coalesce into one React commit per
        // animation frame. The watchdog is re-armed on ARRIVAL below (not on
        // flush) so a healthy, batched stream can never trip the idle watchdog.
        if (envelope.type === 'delta') {
          pendingRef.current.push(envelope);
          scheduleFlush();
          armWatchdog();
          return;
        }

        // NON-TEXT envelope (start, every side-band event incl. answer_retract,
        // and the terminal done/error): flush the buffered text FIRST, then apply
        // this envelope — folded through the pure reducer in a SINGLE dispatch so
        // ordering is exact (AC-3) and no extra render is introduced. A retract
        // (#488) folds right after its buffered deltas, clearing them in the same
        // commit, so speculative text never lands after the retraction.
        const buffered = pendingRef.current;
        pendingRef.current = [];
        cancelScheduledFlush();
        dispatch({ kind: 'flush', envelopes: [...buffered, envelope] });

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
        // Re-arm after EVERY non-terminal envelope (start / event), matching the
        // delta arm above: the lifecycle is start → (delta|event)* → done|error,
        // so a `start` (or `event`) that is the last thing before the backend
        // stalls would otherwise leave the watchdog cleared with no timer to
        // synthesize a terminal `stream_disconnected` (#159).
        armWatchdog();
      },
    });
    clientRef.current = client;
    client.connect();

    return () => {
      // Unmount / streamId change: stop the socket (cancels pending reconnect),
      // drop any pending post-terminal grace timer (#489), and discard any
      // buffered-but-unflushed text so no trailing frame fires after teardown
      // (#493 AC-4).
      terminalRef.current = true;
      clearWatchdog();
      clearGrace();
      discardPendingText();
      client.close();
      clientRef.current = null;
    };
  }, [
    streamId,
    makeClient,
    idleTimeoutMs,
    suggestionsGraceMs,
    clearWatchdog,
    clearGrace,
    scheduleFlush,
    flushPendingText,
    discardPendingText,
    cancelScheduledFlush,
  ]);

  return { ...state, connection, cancel };
}
