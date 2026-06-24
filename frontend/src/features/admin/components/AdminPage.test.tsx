/**
 * AdminPage integration (#88/#122): the read-only console wraps four governance
 * surfaces in a segmented tab bar (Members & roles / Model governance / Approvals
 * & risk / Data minimization) under a tenant-scoped header. Only the active tab's
 * panel renders; switching tabs swaps the panel. Wrapped in a Router (PageChrome
 * renders a back-to-app Link) + a fresh QueryClient. Each panel resolves its own
 * data through the mocked api/ boundary.
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
import type { MemberList, ModelGovernance, RiskTierList } from '@/api';
import { AdminPage } from './AdminPage';

const listMembers = vi.hoisted(() => vi.fn());
const getModelGovernance = vi.hoisted(() => vi.fn());
const getRiskTiers = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, listMembers, getModelGovernance, getRiskTiers };
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

  it('exposes the four governance tabs', () => {
    renderAdmin();
    const tablist = screen.getByRole('tablist', { name: /admin sections/i });
    expect(tablist).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /members & roles/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /model governance/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /approvals & risk/i })).toBeInTheDocument();
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

  it('offers no page-level mutating controls — only tab + chrome affordances', async () => {
    renderAdmin();
    await screen.findByText('admin@acme.test');
    expect(screen.getByText(/not available here/i)).toBeInTheDocument();
    // The only buttons are the four tabs + the chrome back/theme affordances —
    // no admin mutation control (invite / switch / toggle / approve) renders.
    const tabs = screen.getAllByRole('tab');
    expect(tabs).toHaveLength(4);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });
});
