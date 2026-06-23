/**
 * ModelGovernancePanel state coverage (#88): success (models grouped under their
 * tier, with tier descriptions), the 401-expired-session error, empty, and the
 * read-only invariant (ADR-0007 §4 — no governance-edit controls). A model whose
 * tier is referenced but not declared in `tiers` is still shown (never dropped).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { ModelGovernance } from '@/api';
import { ModelGovernancePanel } from './ModelGovernancePanel';

const getModelGovernance = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getModelGovernance };
});

const GOVERNANCE: ModelGovernance = {
  allowed_models: [
    { model_id: 'anthropic/claude-opus-4.8', tier: 'frontier', label: 'Claude Opus 4.8' },
    { model_id: 'openai/gpt-4o-mini', tier: 'fast' },
    // A model on a tier NOT declared in `tiers` — must still render.
    { model_id: 'meta/llama-3-70b', tier: 'oss' },
  ],
  tiers: [
    { id: 'frontier', description: 'Highest-capability tier.' },
    { id: 'fast', description: 'Low-latency tier.' },
  ],
};

beforeEach(() => getModelGovernance.mockReset());

describe('ModelGovernancePanel', () => {
  it('groups allowed models under their governance tier', async () => {
    getModelGovernance.mockResolvedValue(GOVERNANCE);
    renderWithQuery(<ModelGovernancePanel />);

    expect(await screen.findByText('Claude Opus 4.8')).toBeInTheDocument();
    expect(screen.getByText('Highest-capability tier.')).toBeInTheDocument();
    // The id is shown even when a label is present (the wire id is load-bearing).
    expect(screen.getByText('anthropic/claude-opus-4.8')).toBeInTheDocument();
    // Falls back to the model_id when no label is provided.
    expect(screen.getByText('openai/gpt-4o-mini')).toBeInTheDocument();
  });

  it('still shows a model whose tier is not declared in tiers', async () => {
    getModelGovernance.mockResolvedValue(GOVERNANCE);
    renderWithQuery(<ModelGovernancePanel />);
    expect(await screen.findByText('meta/llama-3-70b')).toBeInTheDocument();
  });

  it('has NO mutating controls — read-only governance (ADR-0007 §4)', async () => {
    getModelGovernance.mockResolvedValue(GOVERNANCE);
    renderWithQuery(<ModelGovernancePanel />);
    await screen.findByText('Claude Opus 4.8');
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('shows an empty state when nothing is configured', async () => {
    getModelGovernance.mockResolvedValue({ allowed_models: [], tiers: [] });
    renderWithQuery(<ModelGovernancePanel />);
    expect(await screen.findByText(/no model governance is configured/i)).toBeInTheDocument();
  });

  it('surfaces an expired session (401) as an actionable error (INV-4)', async () => {
    getModelGovernance.mockRejectedValue(new ApiError('unauthorized', 401));
    renderWithQuery(<ModelGovernancePanel />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/session has expired/i)).toBeInTheDocument();
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });
});
