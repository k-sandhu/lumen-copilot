/**
 * #495 — ChatThread render hygiene (AC-1 / AC-2).
 *
 * The failure this pins: while an answer streams, `ChatThread` re-renders per
 * delta (its `live` prop changes each token), and — before the fix — it re-maps
 * EVERY persisted row on every render, re-deriving citations / tool activity /
 * retrieval trace and re-rendering every historical `MessageBubble`. In a long
 * thread that is thousands of wasted bubble renders per answer.
 *
 * These tests measure it directly:
 *  - AC-1: persisted `MessageBubble` render count stays FLAT across N deltas.
 *  - AC-2: per-message derivations (here `toolActivityFromInvocations`, a
 *          persisted-only helper) run once per message per DATA change, not once
 *          per render — so a burst of deltas triggers zero re-derivation.
 *
 * MessageBubble is mocked with a render recorder so the count is exact and cheap;
 * `toolActivityFromInvocations` is wrapped so its call count is observable. Both
 * are persisted-only signals (the live turn maps `fromWsCitation` + `live.tools`
 * directly), so filtering the persisted content prefix isolates the list.
 *
 * SCOPE — this is the ChatThread HALF of AC-1. It hands ChatThread a deliberately
 * stable `onSendText`, so it cannot see the other half: `ActiveSession` handing
 * ChatThread a callback whose identity churns per delta (e.g. a `useCallback`
 * closing over a TanStack mutation RESULT object). `ChatViewRenderHygiene.test.tsx`
 * covers that by driving deltas through the real ChatView/ActiveSession wiring;
 * keep both — this one localises the failure, that one proves the whole path.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import type { Message } from '@/api';
import { ChatThread, type ChatThreadProps, type LiveAnswer } from './ChatThread';

// Record every MessageBubble render by its `content` prop (AC-1). `vi.hoisted`
// makes the array reachable from the hoisted mock factory.
const { bubbleRenders } = vi.hoisted(() => ({ bubbleRenders: [] as string[] }));
vi.mock('./MessageBubble', () => ({
  MessageBubble: ({ content }: { content: string }) => {
    bubbleRenders.push(content);
    return null;
  },
}));

// Wrap the persisted-only derivation so we can count how often it runs (AC-2),
// while keeping every other presentation helper real.
vi.mock('../model/presentation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../model/presentation')>();
  return { ...actual, toolActivityFromInvocations: vi.fn(actual.toolActivityFromInvocations) };
});
import { toolActivityFromInvocations } from '../model/presentation';

function assistantMessage(id: string, content: string): Message {
  return {
    id,
    session_id: 's',
    role: 'assistant',
    content,
    model: 'frontier/opus',
    citations: [],
    tool_invocations: [],
    created_at: '2026-06-18T00:01:00Z',
  };
}

function live(text: string): LiveAnswer {
  return {
    phase: 'streaming',
    text,
    citations: [],
    tools: [],
    codeRuns: [],
    steps: [],
    narration: null,
    askUser: null,
    problem: null,
    model: 'frontier/opus',
  };
}

// A ≥20-message thread of assistant turns (they exercise the derivation path).
const PERSISTED = Array.from({ length: 20 }, (_, i) => assistantMessage(`m${i}`, `Answer ${i}`));
const onOpenCitation = () => {};
const onSendText = () => {};

// Every non-`live` prop is a STABLE reference across rerenders, so only the
// streaming turn changes — exactly the production shape during a live answer.
function props(liveText: string): ChatThreadProps {
  return {
    messages: PERSISTED,
    isLoading: false,
    isError: false,
    error: null,
    onRetryLoad: () => {},
    live: live(liveText),
    onRetryStream: () => {},
    onOpenCitation,
    onSendText,
    sendBusy: true,
  };
}

const persistedRenders = () => bubbleRenders.filter((c) => c.startsWith('Answer '));

beforeEach(() => {
  bubbleRenders.length = 0;
  vi.mocked(toolActivityFromInvocations).mockClear();
});

describe('#495 ChatThread render hygiene', () => {
  it('AC-1: persisted MessageBubbles do not re-render across streamed deltas', () => {
    const { rerender } = render(<ChatThread {...props('a')} />);
    // Mount rendered each persisted bubble exactly once.
    expect(persistedRenders()).toHaveLength(20);
    bubbleRenders.length = 0;

    // 40 deltas, each growing the live answer text (a new `live` object per
    // render — the production per-token shape). The persisted list must not move.
    for (let seq = 2; seq <= 41; seq++) {
      rerender(<ChatThread {...props('a'.repeat(seq))} />);
    }

    expect(persistedRenders()).toHaveLength(0);
  });

  it('AC-2: per-message derivations run once per data change, not once per delta', () => {
    const { rerender } = render(<ChatThread {...props('a')} />);
    // Derived once per assistant message on mount.
    expect(vi.mocked(toolActivityFromInvocations).mock.calls.length).toBe(20);
    vi.mocked(toolActivityFromInvocations).mockClear();

    for (let seq = 2; seq <= 21; seq++) {
      rerender(<ChatThread {...props('a'.repeat(seq))} />);
    }

    // No delta re-derived any persisted row.
    expect(toolActivityFromInvocations).not.toHaveBeenCalled();
  });

  it('AC-2: appending a message re-derives only on the data change', () => {
    const { rerender } = render(<ChatThread {...props('a')} />);
    vi.mocked(toolActivityFromInvocations).mockClear();

    // A brand-new persisted turn arrives (server reload) → one data change.
    const grown = [...PERSISTED, assistantMessage('m20', 'Answer 20')];
    rerender(<ChatThread {...props('a')} messages={grown} />);

    // The derivation recomputes for the new data set (memo keyed on messages),
    // but a subsequent delta with the SAME messages does not re-run it.
    const afterAppend = vi.mocked(toolActivityFromInvocations).mock.calls.length;
    expect(afterAppend).toBeGreaterThan(0);
    vi.mocked(toolActivityFromInvocations).mockClear();
    rerender(<ChatThread {...props('ab')} messages={grown} />);
    expect(toolActivityFromInvocations).not.toHaveBeenCalled();
  });
});
