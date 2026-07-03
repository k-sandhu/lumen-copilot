/**
 * ToolGovernancePanel state coverage (#223): the one WRITE admin surface. Covers
 * every async state (frontend/AGENTS.md: not just success) plus the governance
 * behaviour that matters:
 *
 * - loading shimmer → the tool table with risk badges + enable / requires-approval
 *   switches;
 * - a write-tier tool (run_python) exposes BOTH toggles; a read-only tool (T0)
 *   exposes only the enable toggle (approval is n/a);
 * - flipping "requires approval" PATCHes with both flags (the run_python
 *   pre-approval — AC-2 unlock) and surfaces a success toast;
 * - a 422 (unknown tool) / 403 write error surfaces a dismissible error, never a
 *   silent no-op;
 * - the read 403 (INV-5) lands as the shared actionable panel error;
 * - the empty state when no tools are registered.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { ToolPolicy } from '@/api';
import { ToolGovernancePanel } from './ToolGovernancePanel';

const getToolPolicy = vi.hoisted(() => vi.fn());
const updateToolPolicy = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getToolPolicy, updateToolPolicy };
});

const POLICY: ToolPolicy = {
  items: [
    {
      tool_name: 'search_text',
      risk_tier: 'T0',
      read_only: true,
      enabled: true,
      requires_approval: false,
      is_default: true,
    },
    {
      tool_name: 'run_python',
      risk_tier: 'T2',
      read_only: false,
      enabled: true,
      requires_approval: true,
      is_default: true,
    },
  ],
};

beforeEach(() => {
  getToolPolicy.mockReset();
  updateToolPolicy.mockReset();
});

describe('ToolGovernancePanel', () => {
  it('renders each tool with its risk badge and enable switch', async () => {
    getToolPolicy.mockResolvedValue(POLICY);
    renderWithQuery(<ToolGovernancePanel />);

    expect(await screen.findByText('run_python')).toBeInTheDocument();
    expect(screen.getByText('search_text')).toBeInTheDocument();
    expect(screen.getByLabelText(/risk tier T2/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/risk tier T0/i)).toBeInTheDocument();
    // A write-tier tool exposes an enable AND an approval toggle.
    expect(screen.getByRole('switch', { name: /disable run_python/i })).toBeInTheDocument();
    expect(
      screen.getByRole('switch', { name: /pre-approve run_python/i }),
    ).toBeInTheDocument();
  });

  it('offers no approval control for a read-only (T0) tool', async () => {
    getToolPolicy.mockResolvedValue(POLICY);
    renderWithQuery(<ToolGovernancePanel />);
    await screen.findByText('search_text');
    // The read-only tool has an enable switch but no requires-approval control.
    expect(screen.getByRole('switch', { name: /disable search_text/i })).toBeInTheDocument();
    expect(
      screen.queryByRole('switch', { name: /approval.*search_text/i }),
    ).not.toBeInTheDocument();
  });

  it('pre-approves run_python by PATCHing both flags (the AC-2 unlock)', async () => {
    getToolPolicy.mockResolvedValue(POLICY);
    updateToolPolicy.mockResolvedValue({
      items: [
        { ...POLICY.items[0] },
        { ...POLICY.items[1], requires_approval: false, is_default: false },
      ],
    });
    renderWithQuery(<ToolGovernancePanel />);
    await screen.findByText('run_python');

    // Clearing "requires approval" while enabled is the admin pre-approval.
    await userEvent.click(screen.getByRole('switch', { name: /pre-approve run_python/i }));

    expect(updateToolPolicy).toHaveBeenCalledWith({
      tool_name: 'run_python',
      enabled: true,
      requires_approval: false,
    });
    // The audited action's success is visible, never silent.
    expect(await screen.findByText(/updated run_python/i)).toBeInTheDocument();
  });

  it('disables a tool by PATCHing enabled=false', async () => {
    getToolPolicy.mockResolvedValue(POLICY);
    updateToolPolicy.mockResolvedValue(POLICY);
    renderWithQuery(<ToolGovernancePanel />);
    await screen.findByText('run_python');

    await userEvent.click(screen.getByRole('switch', { name: /disable run_python/i }));
    expect(updateToolPolicy).toHaveBeenCalledWith({
      tool_name: 'run_python',
      enabled: false,
      requires_approval: true,
    });
  });

  it('surfaces a write error (403) as a dismissible alert, not a silent no-op', async () => {
    getToolPolicy.mockResolvedValue(POLICY);
    updateToolPolicy.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<ToolGovernancePanel />);
    await screen.findByText('run_python');

    await userEvent.click(screen.getByRole('switch', { name: /disable run_python/i }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/admin role/i)).toBeInTheDocument();
  });

  it('shows an empty state when no tools are registered', async () => {
    getToolPolicy.mockResolvedValue({ items: [] });
    renderWithQuery(<ToolGovernancePanel />);
    expect(await screen.findByText(/no tools are registered/i)).toBeInTheDocument();
  });

  it('surfaces a read 403 as an actionable panel error (INV-5)', async () => {
    getToolPolicy.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<ToolGovernancePanel />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/admin role/i)).toBeInTheDocument();
  });
});
