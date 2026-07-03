/**
 * AdminPage integration (#88/#122/#223): the console wraps the governance surfaces
 * in a segmented tab bar (Members & roles / Model governance / Approvals & risk /
 * Tool governance / Data minimization) under a tenant-scoped header. Only the active
 * tab's panel renders; switching tabs swaps the panel. Wrapped in a Router
 * (PageChrome renders a back-to-app Link) + a fresh QueryClient. Each panel resolves
 * its own data through the mocked api/ boundary.
 *
 * No access token is seeded, so `useCurrentUser` stays disabled (no /auth/me
 * call) and the header shows the honest "tenant unavailable" fallback — never a
 * fabricated tenant name.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { MemberList, ModelGovernance, RiskTierList, ToolPolicy } from '@/api';
import { AdminPage } from './AdminPage';

const listMembers = vi.hoisted(() => vi.fn());
const getModelGovernance = vi.hoisted(() => vi.fn());
const getRiskTiers = vi.hoisted(() => vi.fn());
const getToolPolicy = vi.hoisted(() => vi.fn());
const updateToolPolicy = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, listMembers, getModelGovernance, getRiskTiers, getToolPolicy, updateToolPolicy };
});

const MEMBERS: MemberList = {
  items: [{ id: 'u1', email: 'admin@acme.test', role: ['admin'] }],
  next_cursor: null,
};
const GOVERNANCE: ModelGovernance = {
  allowed_models: [{ model_id: 'anthropic/claude-opus-4.8', tier: 'frontier' }],
  tiers: [{ id: 'frontier', description: 'Highest-capability tier.' }],
};
const TIERS: RiskTierList = {
  items: [{ tier: 'T0', description: 'Read-only retrieval.', approval: 'none' }],
};
const TOOL_POLICY: ToolPolicy = {
  items: [
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

function renderAdmin() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <AdminPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  listMembers.mockResolvedValue(MEMBERS);
  getModelGovernance.mockResolvedValue(GOVERNANCE);
  getRiskTiers.mockResolvedValue(TIERS);
  getToolPolicy.mockResolvedValue(TOOL_POLICY);
  updateToolPolicy.mockResolvedValue(TOOL_POLICY);
});

describe('AdminPage', () => {
  it('renders a tenant-scoped header (honest tenant, no fabricated name)', () => {
    renderAdmin();
    // The content header carries the tenant-scoped subtitle (the PageChrome bar
    // also titles the page "Admin" when rendered standalone — that's the shell's).
    expect(screen.getByText(/governance, models, and data controls/i)).toBeInTheDocument();
    // No token seeded → the header falls back honestly, never inventing a company.
    expect(screen.getByText(/tenant unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/northwind/i)).not.toBeInTheDocument();
  });

  it('exposes the governance tabs (including Tool governance, #223)', () => {
    renderAdmin();
    const tablist = screen.getByRole('tablist', { name: /admin sections/i });
    expect(tablist).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /members & roles/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /model governance/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /approvals & risk/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /tool governance/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /data minimization/i })).toBeInTheDocument();
  });

  it('shows the Members panel first and resolves its content', async () => {
    renderAdmin();
    expect(screen.getByRole('tab', { name: /members & roles/i })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(await screen.findByText('admin@acme.test')).toBeInTheDocument();
  });

  it('switches panels when a tab is selected', async () => {
    renderAdmin();
    await screen.findByText('admin@acme.test');

    await userEvent.click(screen.getByRole('tab', { name: /model governance/i }));
    expect(await screen.findByText('anthropic/claude-opus-4.8')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('tab', { name: /approvals & risk/i }));
    expect(await screen.findByText('Read-only retrieval.')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('tab', { name: /data minimization/i }));
    expect(
      await screen.findByRole('heading', { name: /^data minimization$/i }),
    ).toBeInTheDocument();
  });

  it('shows no mutating controls on the read-only panels (Tool governance aside)', async () => {
    renderAdmin();
    await screen.findByText('admin@acme.test');
    expect(screen.getByText(/not available here/i)).toBeInTheDocument();
    // The Members panel (the default) carries no mutation control — the ONE write
    // surface is the Tool governance tab, which is not mounted here.
    const tabs = screen.getAllByRole('tab');
    expect(tabs).toHaveLength(5);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('surfaces the Tool governance write controls when that tab is active (#223)', async () => {
    renderAdmin();
    await screen.findByText('admin@acme.test');
    await userEvent.click(screen.getByRole('tab', { name: /tool governance/i }));
    // The registered tool renders with an enable/approval switch (the write surface).
    expect(await screen.findByText('run_python')).toBeInTheDocument();
    expect(screen.getAllByRole('switch').length).toBeGreaterThan(0);
  });
});
