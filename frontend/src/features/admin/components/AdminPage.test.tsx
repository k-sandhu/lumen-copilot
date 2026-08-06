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
import type {
  AutonomyPolicy,
  GroupList,
  MemberList,
  ModelGovernance,
  RiskTierList,
  SandboxPolicy,
  ToolPolicy,
} from '@/api';
import { AdminPage } from './AdminPage';

const listMembers = vi.hoisted(() => vi.fn());
const listGroups = vi.hoisted(() => vi.fn());
const getModelGovernance = vi.hoisted(() => vi.fn());
const getRiskTiers = vi.hoisted(() => vi.fn());
const getToolPolicy = vi.hoisted(() => vi.fn());
const updateToolPolicy = vi.hoisted(() => vi.fn());
const getSandboxPolicy = vi.hoisted(() => vi.fn());
const updateSandboxPolicy = vi.hoisted(() => vi.fn());
const getAutonomyPolicy = vi.hoisted(() => vi.fn());
const updateAutonomyPolicy = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listMembers,
    listGroups,
    getModelGovernance,
    getRiskTiers,
    getToolPolicy,
    updateToolPolicy,
    getSandboxPolicy,
    updateSandboxPolicy,
    getAutonomyPolicy,
    updateAutonomyPolicy,
  };
});

const MEMBERS: MemberList = {
  items: [{ id: 'u1', email: 'admin@acme.test', role: ['admin'], email_attested_at: null }],
  next_cursor: null,
};
const GROUPS: GroupList = {
  items: [
    {
      id: 'g0',
      name: 'All members',
      kind: 'system',
      member_count: null,
      created_at: '2026-07-30T00:00:00Z',
      updated_at: '2026-07-30T00:00:00Z',
    },
  ],
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
const SANDBOX_POLICY: SandboxPolicy = {
  enabled: false,
  allowed_packages: [],
  denied_packages: [],
  egress_allowed: false,
  egress_allowlist: [],
  max_runtime_s: 30,
  max_memory_mb: 512,
  daily_runtime_cap_s: 3600,
  max_concurrency: 2,
  is_default: true,
  max_runtime_s_ceiling: 30,
  max_memory_mb_ceiling: 512,
  daily_runtime_cap_s_ceiling: 3600,
  max_concurrency_ceiling: 2,
};
const AUTONOMY_POLICY: AutonomyPolicy = {
  max_autonomy: 'act_auto',
  is_default: true,
  levels: ['suggest', 'draft', 'act_with_approval', 'act_auto'],
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
  listGroups.mockResolvedValue(GROUPS);
  getModelGovernance.mockResolvedValue(GOVERNANCE);
  getRiskTiers.mockResolvedValue(TIERS);
  getToolPolicy.mockResolvedValue(TOOL_POLICY);
  updateToolPolicy.mockResolvedValue(TOOL_POLICY);
  getSandboxPolicy.mockResolvedValue(SANDBOX_POLICY);
  updateSandboxPolicy.mockResolvedValue(SANDBOX_POLICY);
  getAutonomyPolicy.mockResolvedValue(AUTONOMY_POLICY);
  updateAutonomyPolicy.mockResolvedValue(AUTONOMY_POLICY);
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
    expect(screen.getByRole('tab', { name: /^groups$/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /model governance/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /approvals & risk/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /tool governance/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /sandbox governance/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /^autonomy$/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /data minimization/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /branding/i })).toBeInTheDocument();
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
    // The Members panel (the default) carries no mutation control — the write
    // surfaces are the Tool governance + Sandbox governance + Branding tabs, not
    // mounted here.
    const tabs = screen.getAllByRole('tab');
    expect(tabs).toHaveLength(10);
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

  it('surfaces the Sandbox governance write controls when that tab is active (#233)', async () => {
    renderAdmin();
    await screen.findByText('admin@acme.test');
    await userEvent.click(screen.getByRole('tab', { name: /sandbox governance/i }));
    // The sandbox policy form renders with its enable toggle + Save control.
    expect(
      await screen.findByRole('switch', { name: /enable code execution/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save policy/i })).toBeInTheDocument();
  });

  it('surfaces the Autonomy cap write control when that tab is active (#218)', async () => {
    renderAdmin();
    await screen.findByText('admin@acme.test');
    await userEvent.click(screen.getByRole('tab', { name: /^autonomy$/i }));
    // The autonomy cap form renders with its level select (the write surface).
    expect(await screen.findByLabelText(/maximum autonomy/i)).toBeInTheDocument();
  });
});
