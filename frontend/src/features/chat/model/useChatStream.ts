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
  /**
   * True ONLY while the post-terminal suggestions grace is actually armed
   * (#489/FE2-3): from a `done(pendingSuggestions=true)` until the suggestions
   * land, the grace elapses, the stream is cancelled, or the hook tears down.
   * It is the gate on accepting a post-terminal `event:suggestions`, and it
   * tells the caller the socket must OUTLIVE the persisted-history reload
   * (BE2-7) — retiring the live bubble must not tear the subscription down.
   */
  awaitingSuggestions: boolean;
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
  // FE2-3: ONE generation token for the whole hook, bumped by every event that
  // ends a subscription's authority — a new effect run, effect cleanup,
  // `cancel()`, and every deliberate close. A socket callback captures the
  // generation it was created under and no-ops the moment it no longer matches,
  // so nothing a dead socket says can reach reducer state, a timer, or React.
  // A ref (not an effect-local `let`) is what lets `cancel()` — declared outside
  // the effect — invalidate the run that is currently live.
  const generationRef = useRef(0);
  // FE2-3: is the post-terminal suggestions grace armed RIGHT NOW? Mirrored in
  // a ref for the socket callbacks (which must decide synchronously) and in
  // state for the caller (BE2-7 keeps the subscription mounted while it is up).
  const awaitingRef = useRef(false);
  const [awaitingSuggestions, setAwaitingSuggestions] = useState(false);
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

  // FE2-3: the ONE way this hook closes a socket while mounted (cancel, terminal
  // done/error, watchdog, mid-stream drop, grace expiry, suggestions delivered).
  // It invalidates the generation token FIRST, so this socket's own — possibly
  // asynchronous — `closed` callback and anything still queued behind it are
  // already stale and cannot dispatch, re-arm a timer, or set state. Everything
  // that callback used to do is therefore done here, explicitly: drop every
  // timer, discard unflushed text, close the suggestions window, and report the
  // connection as closed (which is now the truth, not a prediction).
  const closeStream = useCallback(() => {
    generationRef.current += 1;
    clearWatchdog();
    clearGrace();
    discardPendingText();
    awaitingRef.current = false;
    setAwaitingSuggestions(false);
    const client = clientRef.current;
    clientRef.current = null;
    client?.close();
    setConnection('closed');
  }, [clearWatchdog, clearGrace, discardPendingText]);

  const cancel = useCallback(() => {
    terminalRef.current = true;
    closeStream();
  }, [closeStream]);

  useEffect(() => {
    if (!streamId) {
      clearWatchdog();
      clearGrace();
      awaitingRef.current = false;
      setAwaitingSuggestions(false);
      setConnection('closed');
      return;
    }

    dispatch({ kind: 'reset' });
    discardPendingText();
    terminalRef.current = false;
    awaitingRef.current = false;
    setAwaitingSuggestions(false);

    // FE-4/FE2-3: this run's slice of the hook-wide generation token. A real
    // WebSocket reports `closed` ASYNCHRONOUSLY, so THIS socket's
    // onStateChange/onEnvelope can still fire AFTER its authority ended — on
    // unmount, on a streamId change that has already reset the shared refs
    // (terminalRef/clientRef/buffer) for the NEXT stream, after `cancel()`, or
    // after any deliberate close. Every socket callback checks `isCurrent()` and
    // no-ops once stale, so a late callback can neither setConnection on an
    // unmounted hook nor bleed into settled/next-stream state (flush its buffer,
    // dispatch a disconnect, accept a suggestion, or close another client).
    generationRef.current += 1;
    const generation = generationRef.current;
    const isCurrent = () => generationRef.current === generation;

    const armWatchdog = () => {
      clearWatchdog();
      watchdogRef.current = setTimeout(() => {
        watchdogRef.current = null;
        if (!isCurrent() || terminalRef.current) return;
        terminalRef.current = true;
        // Commit any buffered partial text before the synthetic terminal so the
        // disconnect banner shows how far the answer got (AC-4 preserves text).
        flushPendingText();
        dispatch({ kind: 'disconnect' });
        closeStream();
      }, idleTimeoutMs);
    };

    const armSuggestionsGrace = (graceMs: number) => {
      // The stream is already terminal (UI settled on `done`); hold the socket
      // open a bounded while for the one post-terminal `event:suggestions` (#489).
      // If it never comes, close the socket — the terminal stands. `graceMs` is the
      // server's declared grace when present (#489/BE-5), else the client default.
      // FE2-3: this — and ONLY this — opens the suggestions window; closing the
      // stream (delivery, expiry, cancel, teardown) is what shuts it again.
      clearGrace();
      awaitingRef.current = true;
      setAwaitingSuggestions(true);
      graceRef.current = setTimeout(() => {
        graceRef.current = null;
        if (!isCurrent()) return;
        closeStream();
      }, graceMs);
    };

    const client = makeClient({
      path: `/chat/${streamId}`,
      onStateChange: (next) => {
        if (!isCurrent()) return; // FE-4/FE2-3: stale socket — no-op.
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
            // FE-5: clear ALL timers on this terminal close path — the watchdog
            // too, not just the grace — and (FE2-3) shut the suggestions window,
            // since the server ending the post-terminal grace means nothing more
            // is coming. `closeStream` does all of it and invalidates this run,
            // so no further callback from this dead socket can be honoured.
            closeStream();
          } else {
            clearWatchdog();
            terminalRef.current = true;
            // Flush the partial answer BEFORE the synthetic terminal so a
            // mid-stream drop preserves the text streamed so far (AC-4).
            flushPendingText();
            dispatch({ kind: 'disconnect' });
            closeStream();
          }
        }
      },
      onEnvelope: (envelope) => {
        if (!isCurrent()) return; // FE-4/FE2-3: stale socket — no-op.
        // Only the matching stream's envelopes (defensive — one socket per id).
        if (envelope.streamId !== streamId) return;

        // FE-5: once the stream is terminal (done/error/cancel/disconnect) it has
        // settled. The ONLY envelope still honoured is the single post-terminal
        // `event:suggestions` we hold the socket open for (#489) — and (FE2-3)
        // only while that window is ACTUALLY open: `awaitingRef` is true just
        // between a `done(pendingSuggestions=true)` and the grace closing, so a
        // suggestion after a cancel, after the grace expired, or after any other
        // terminal (error/disconnect/plain done) is dropped like every other
        // straggler. Every other late/queued/stray envelope — a straggler delta,
        // a duplicate terminal, a late side-band — is ignored here BEFORE it can
        // buffer text, schedule a flush, or re-arm the idle watchdog (which would
        // leave a timer alive past close). Handled first so the delta/non-text
        // paths below only ever run pre-terminal.
        if (terminalRef.current) {
          if (awaitingRef.current && envelope.type === 'event' && envelope.name === 'suggestions') {
            // The awaited post-terminal suggestions arrived within the grace: the
            // reducer attaches it without un-settling the terminal (phase stays
            // 'done'); we got what we waited for, so shut the window and close.
            // No text can be buffered here — post-terminal deltas were ignored.
            dispatch({ kind: 'flush', envelopes: [envelope] });
            closeStream();
          }
          return;
        }
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
            closeStream();
          }
          return;
        }
        if (envelope.type === 'error') {
          terminalRef.current = true;
          closeStream();
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
      // Unmount / streamId change: invalidate the generation token FIRST (FE-4)
      // so the socket's asynchronous `closed`/late-envelope callbacks no-op
      // instead of touching the next stream's shared refs. Then stop the socket
      // (cancels pending reconnect), drop any pending post-terminal grace timer
      // (#489), close the suggestions window, and discard any
      // buffered-but-unflushed text so no trailing frame fires after teardown
      // (#493 AC-4). Deliberately NOT `closeStream()`: this path can run on
      // unmount, where a setState would be pointless work on a dead tree.
      generationRef.current += 1;
      terminalRef.current = true;
      awaitingRef.current = false;
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
    closeStream,
    scheduleFlush,
    flushPendingText,
    discardPendingText,
    cancelScheduledFlush,
  ]);

  return { ...state, connection, cancel, awaitingSuggestions };
}
