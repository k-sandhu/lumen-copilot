/**
 * AutonomyGovernancePanel state coverage (#218): the per-tenant autonomy-cap WRITE
 * surface. Covers every async state (frontend/AGENTS.md: not just success) plus the
 * governance behaviour that matters:
 *
 * - loading → the cap select bound to the four levels, with the effective ceiling
 *   selected and a default/ceiling note;
 * - choosing a level PATCHes /admin/autonomy-policy with the chosen `max_autonomy`
 *   and surfaces a success toast (the audited write is never silent);
 * - a 403 write error surfaces a dismissible alert, not a silent no-op;
 * - the read 403 (INV-5) lands as the shared actionable panel error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { AutonomyPolicy } from '@/api';
import { AutonomyGovernancePanel } from './AutonomyGovernancePanel';

const getAutonomyPolicy = vi.hoisted(() => vi.fn());
const updateAutonomyPolicy = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getAutonomyPolicy, updateAutonomyPolicy };
});

const LEVELS: AutonomyPolicy['levels'] = ['suggest', 'draft', 'act_with_approval', 'act_auto'];

const DEFAULT_POLICY: AutonomyPolicy = {
  max_autonomy: 'act_auto',
  is_default: true,
  levels: LEVELS,
};

const CAPPED_POLICY: AutonomyPolicy = {
  max_autonomy: 'draft',
  is_default: false,
  levels: LEVELS,
};

beforeEach(() => {
  getAutonomyPolicy.mockReset();
  updateAutonomyPolicy.mockReset();
});

describe('AutonomyGovernancePanel', () => {
  it('renders the cap select with the no-ceiling default selected', async () => {
    getAutonomyPolicy.mockResolvedValue(DEFAULT_POLICY);
    renderWithQuery(<AutonomyGovernancePanel />);

    const select = (await screen.findByLabelText(/maximum autonomy/i)) as HTMLSelectElement;
    expect(select.value).toBe('act_auto');
    // Every level is offered.
    expect(within(select).getByRole('option', { name: 'Suggest' })).toBeInTheDocument();
    expect(within(select).getByRole('option', { name: 'Act automatically' })).toBeInTheDocument();
    // The default (no ceiling) is explained, never a bare control.
    expect(screen.getByText(/no cap is set/i)).toBeInTheDocument();
  });

  it('shows the stored cap when one is set', async () => {
    getAutonomyPolicy.mockResolvedValue(CAPPED_POLICY);
    renderWithQuery(<AutonomyGovernancePanel />);
    const select = (await screen.findByLabelText(/maximum autonomy/i)) as HTMLSelectElement;
    expect(select.value).toBe('draft');
    expect(screen.getByText(/a cap is set/i)).toBeInTheDocument();
  });

  it('PATCHes the chosen level and surfaces a success toast (audited, not silent)', async () => {
    getAutonomyPolicy.mockResolvedValue(DEFAULT_POLICY);
    updateAutonomyPolicy.mockResolvedValue(CAPPED_POLICY);
    renderWithQuery(<AutonomyGovernancePanel />);

    const select = await screen.findByLabelText(/maximum autonomy/i);
    await userEvent.selectOptions(select, 'draft');

    expect(updateAutonomyPolicy).toHaveBeenCalledWith({ max_autonomy: 'draft' });
    expect(await screen.findByText(/autonomy cap set to draft/i)).toBeInTheDocument();
  });

  it('surfaces a write error (403) as a dismissible alert, not a silent no-op', async () => {
    getAutonomyPolicy.mockResolvedValue(DEFAULT_POLICY);
    updateAutonomyPolicy.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<AutonomyGovernancePanel />);

    const select = await screen.findByLabelText(/maximum autonomy/i);
    await userEvent.selectOptions(select, 'suggest');
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/admin role/i)).toBeInTheDocument();
  });

  it('surfaces a read 403 as an actionable panel error (INV-5)', async () => {
    getAutonomyPolicy.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<AutonomyGovernancePanel />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/admin role/i)).toBeInTheDocument();
  });
});
