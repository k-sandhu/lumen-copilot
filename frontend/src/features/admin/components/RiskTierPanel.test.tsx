/**
 * RiskTierPanel state coverage (#88): success (T0–T3 rendered through the
 * design-system RiskTierBadge with escalating colour + the approval each needs),
 * empty, the 403 error, and the read-only invariant — the risk-tier map is a
 * reference, not a control surface (ADR-0007 §4: no approve/deny actions).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { RiskTierList } from '@/api';
import { RiskTierPanel } from './RiskTierPanel';

const getRiskTiers = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getRiskTiers };
});

const TIERS: RiskTierList = {
  items: [
    { tier: 'T0', description: 'Read-only retrieval.', approval: 'none' },
    { tier: 'T1', description: 'Low-impact internal write.', approval: 'owner' },
    { tier: 'T2', description: 'External notification.', approval: 'human approval' },
    {
      tier: 'T3',
      description: 'Destructive / irreversible external.',
      approval: 'dual approval + risk tier',
    },
  ],
};

beforeEach(() => getRiskTiers.mockReset());

describe('RiskTierPanel', () => {
  it('renders T0–T3 with their descriptions and required approval', async () => {
    getRiskTiers.mockResolvedValue(TIERS);
    renderWithQuery(<RiskTierPanel />);

    expect(await screen.findByText('Read-only retrieval.')).toBeInTheDocument();
    expect(screen.getByText('Destructive / irreversible external.')).toBeInTheDocument();
    expect(screen.getByText('dual approval + risk tier')).toBeInTheDocument();
    // Risk-tier badges come from the design-system kit (escalating colour).
    expect(screen.getByLabelText(/risk tier T0/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/risk tier T3/i)).toBeInTheDocument();
  });

  it('renders the badge colour escalating to danger at T3', async () => {
    getRiskTiers.mockResolvedValue(TIERS);
    const { container } = renderWithQuery(<RiskTierPanel />);
    await screen.findByText('Read-only retrieval.');
    expect(container.querySelector('.lc-badge--danger')).not.toBeNull();
  });

  it('has NO approve/deny or other mutating controls (ADR-0007 §4)', async () => {
    getRiskTiers.mockResolvedValue(TIERS);
    renderWithQuery(<RiskTierPanel />);
    await screen.findByText('Read-only retrieval.');
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('shows an empty state when no tiers are configured', async () => {
    getRiskTiers.mockResolvedValue({ items: [] });
    renderWithQuery(<RiskTierPanel />);
    expect(await screen.findByText(/no risk tiers are configured/i)).toBeInTheDocument();
  });

  it('surfaces a 403 as an actionable error (INV-5)', async () => {
    getRiskTiers.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<RiskTierPanel />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/admin role/i)).toBeInTheDocument();
  });
});
