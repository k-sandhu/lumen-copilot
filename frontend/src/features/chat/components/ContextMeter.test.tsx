/**
 * Context meter (spec 0007 #432, AC-1): utilization label from the usage
 * endpoint; honest empty state; silent on error (a nicety, never a blocker).
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ContextMeter } from './ContextMeter';
import { formatTokens } from '../model/composerHelpers';
import type { SessionUsage } from '@/api';

const USAGE: SessionUsage = {
  model: 'anthropic/claude-opus-4.8',
  totals: {
    answers: 3,
    prompt_tokens: 30_000,
    completion_tokens: 4_000,
    total_tokens: 34_000,
    cached_prompt_tokens: 12_000,
    cache_write_tokens: 0,
  },
  last: {
    prompt_tokens: 47_000, // billing sum across loop turns + suggestions
    completion_tokens: 900,
    total_tokens: 47_900,
    cached_prompt_tokens: 9_000,
    cache_write_tokens: 0,
    context_prompt_tokens: 19_100, // actual final-turn window occupancy (#434 NEW-1)
  },
  input_budget_tokens: 190_976,
  window_known: true,
};

function mockUsage(body: SessionUsage | null, status = 200) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      new Response(body ? JSON.stringify(body) : '{}', {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  );
}

afterEach(() => vi.restoreAllMocks());

describe('ContextMeter', () => {
  it('shows last-turn utilization against the input budget (AC-1)', async () => {
    mockUsage(USAGE);
    renderWithQuery(<ContextMeter sessionId="s1" />);
    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument());
    // 19_100 / 190_976 → 10%.
    expect(screen.getByText(/Context 10% · 19\.1k\/191\.0k/)).toBeInTheDocument();
  });

  it('renders an honest empty meter for a fresh session', async () => {
    mockUsage({
      ...USAGE,
      totals: { ...USAGE.totals, answers: 0, total_tokens: 0 },
      last: undefined as never,
    });
    renderWithQuery(<ContextMeter sessionId="s1" />);
    await waitFor(() => expect(screen.getByText(/Context 191\.0k available/)).toBeInTheDocument());
  });

  it('renders nothing on a failed read (silent nicety)', async () => {
    mockUsage(null, 500);
    renderWithQuery(<ContextMeter sessionId="s1" />);
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
  });
});

describe('formatTokens', () => {
  it('abbreviates thousands and millions', () => {
    expect(formatTokens(999)).toBe('999');
    expect(formatTokens(19_100)).toBe('19.1k');
    expect(formatTokens(1_200_000)).toBe('1.2M');
  });
});
