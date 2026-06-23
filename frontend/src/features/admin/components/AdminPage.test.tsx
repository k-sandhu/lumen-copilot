/**
 * AdminPage integration (#88): the read-only console composes all three
 * governance panels (Members, Model governance, Risk tiers) under the page
 * chrome, and each panel resolves its own data. Wrapped in a Router (PageChrome
 * renders a back-to-app Link) + a fresh QueryClient. Asserts the three section
 * headings render and the panels surface their fetched content.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
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
  it('renders the three governance sections', () => {
    renderAdmin();
    expect(screen.getByRole('heading', { name: /members & roles/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /model governance/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /approvals & risk tiers/i })).toBeInTheDocument();
  });

  it('resolves each panel with its fetched content', async () => {
    renderAdmin();
    expect(await screen.findByText('admin@acme.test')).toBeInTheDocument();
    expect(await screen.findByText('anthropic/claude-opus-4.8')).toBeInTheDocument();
    expect(await screen.findByText('Read-only retrieval.')).toBeInTheDocument();
  });

  it('states the read-only scope and offers no page-level mutating controls', async () => {
    renderAdmin();
    await screen.findByText('admin@acme.test');
    expect(screen.getByText(/not available here in v1/i)).toBeInTheDocument();
    // Only chrome affordances exist (back-to-app + theme toggle); no admin
    // mutation buttons anywhere on the page.
    const buttons = screen.queryAllByRole('button');
    expect(buttons.length).toBeLessThanOrEqual(1);
  });
});
