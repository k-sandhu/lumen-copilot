/**
 * AppShell (issue #110) — the cohesive chrome that wraps every authenticated
 * screen. Covers the demo-critical invariants: the shell renders (brand + rail
 * groups), the rail highlights the active route, developer pages are excluded
 * from the rail, the omni bar opens the existing command palette, the appearance
 * + account controls are present, and an unbacked rail path (Sources) renders
 * disabled rather than as a dead link.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TooltipProvider } from '@/components/Tooltip';
import { setAccessToken, clearAccessToken } from '@/api';
import { AppShell } from './AppShell';

const ME = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'avery.madison@northwind.test',
  tenant_id: 'northwind-uuid-0001',
  tenant_name: 'Northwind',
  roles: ['member'],
  created_at: '2026-06-18T00:00:00Z',
};

function meResponse(): Response {
  return new Response(JSON.stringify(ME), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderShell(initialPath = '/') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route path="/" element={<div>chat screen</div>} />
              <Route path="/search" element={<div>search screen</div>} />
              <Route path="/documents" element={<div>documents screen</div>} />
              <Route path="/audit" element={<div>audit screen</div>} />
              <Route path="/admin" element={<div>admin screen</div>} />
              {/* Deep links into the #374 surfaces still render the shell. */}
              <Route path="*" element={<div>feature screen</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  setAccessToken('jwt');
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(meResponse());
});
afterEach(() => {
  vi.restoreAllMocks();
  clearAccessToken();
});

describe('AppShell', () => {
  it('renders the brand cell and both rail groups', () => {
    renderShell();
    expect(screen.getByText('Lumen')).toBeInTheDocument();
    expect(screen.getByText('Copilot')).toBeInTheDocument();
    const rail = screen.getByRole('navigation', { name: /primary/i });
    expect(within(rail).getByText('Workspace')).toBeInTheDocument();
    expect(within(rail).getByText('Administration')).toBeInTheDocument();
  });

  it('renders the routed screen in the shell main', () => {
    renderShell('/search');
    expect(screen.getByText('search screen')).toBeInTheDocument();
  });

  it('groups the workspace + administration nav items', () => {
    renderShell();
    const rail = screen.getByRole('navigation', { name: /primary/i });
    for (const label of ['Assistant', 'Search', 'Documents', 'Audit log', 'Admin']) {
      expect(within(rail).getByText(label)).toBeInTheDocument();
    }
  });

  it('highlights the active route (aria-current)', () => {
    renderShell('/audit');
    const active = screen.getByRole('link', { name: /audit log/i });
    expect(active).toHaveAttribute('aria-current', 'page');
    // a different link is not current
    expect(screen.getByRole('link', { name: /^search$/i })).not.toHaveAttribute('aria-current');
  });

  it('excludes the developer pages from the rail', () => {
    renderShell();
    const rail = screen.getByRole('navigation', { name: /primary/i });
    expect(within(rail).queryByText('Documentation')).not.toBeInTheDocument();
    expect(within(rail).queryByText('Features built')).not.toBeInTheDocument();
  });

  it('renders Sources as an active rail link now that the /sources route exists (#27)', () => {
    renderShell();
    const rail = screen.getByRole('navigation', { name: /primary/i });
    // The Sources screen (#27) is now backed by a discovered route, so the rail
    // resolves it to a real link rather than a disabled "coming soon" entry.
    const link = within(rail).getByRole('link', { name: /sources/i });
    expect(link).toHaveAttribute('href', '/sources');
    expect(link).not.toHaveAttribute('aria-disabled');
  });

  it('lists every #374 surface as an enabled rail link (real discovery manifests)', () => {
    renderShell();
    const rail = screen.getByRole('navigation', { name: /primary/i });
    for (const [label, href] of [
      ['Assistants', '/assistants'],
      ['Schedules', '/schedules'],
      ['Run history', '/runs'],
      ['Artifacts', '/artifacts'],
      ['MCP servers', '/mcp-servers'],
    ] as const) {
      const link = within(rail).getByRole('link', { name: new RegExp(`^${label}$`, 'i') });
      expect(link).toHaveAttribute('href', href);
      expect(link).not.toHaveAttribute('aria-disabled');
    }
  });

  it('highlights the rail item for a DEEP link into a #374 surface', () => {
    renderShell('/assistants/a1');
    expect(screen.getByRole('link', { name: /^assistants$/i })).toHaveAttribute(
      'aria-current',
      'page',
    );
    // The chat home link ('/') must not claim the deep path.
    expect(screen.getByRole('link', { name: /^assistant$/i })).not.toHaveAttribute('aria-current');
  });

  it('offers a palette command for every #374 surface', async () => {
    const user = userEvent.setup();
    renderShell();
    await user.keyboard('{Control>}k{/Control}');
    const dialog = await screen.findByRole('dialog', { name: /command palette/i });
    for (const label of [
      'Go to Assistants',
      'Go to Schedules',
      'Go to Run history',
      'Go to Artifacts',
      'Go to MCP servers',
    ]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
  });

  it('opens the command palette from the omni bar', async () => {
    const user = userEvent.setup();
    renderShell();
    expect(screen.queryByRole('dialog', { name: /command palette/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /search or ask across your workspace/i }));
    expect(await screen.findByRole('dialog', { name: /command palette/i })).toBeInTheDocument();
  });

  it('opens the command palette with the ⌘/Ctrl-K shortcut', async () => {
    const user = userEvent.setup();
    renderShell();
    await user.keyboard('{Control>}k{/Control}');
    expect(await screen.findByRole('dialog', { name: /command palette/i })).toBeInTheDocument();
  });

  it('exposes the appearance control and an account avatar', () => {
    renderShell();
    expect(screen.getByRole('button', { name: /appearance/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /account menu/i })).toBeInTheDocument();
  });

  it('shows the tenant NAME (not the raw id) from /auth/me with a hard-isolation tooltip', async () => {
    renderShell();
    expect(await screen.findByText('Northwind')).toBeInTheDocument();
    // The raw id is kept out of the visible pill (tooltip only, #247).
    expect(screen.queryByText('northwind-uuid-0001')).not.toBeInTheDocument();
  });

  it('exposes a focus-revealed "Skip to content" link targeting #main-content (#163)', () => {
    renderShell();
    const skip = screen.getByRole('link', { name: /skip to content/i });
    expect(skip).toHaveAttribute('href', '#main-content');
    // It targets the real <main> landmark the shell renders.
    expect(document.getElementById('main-content')?.tagName).toBe('MAIN');
  });

  it('moves focus to <main> when the route changes (#163)', async () => {
    const user = userEvent.setup();
    renderShell('/');
    const main = document.getElementById('main-content');
    expect(main).not.toBeNull();

    // Navigate to a different screen via the rail; focus lands on the routed
    // <main> (tabIndex=-1) rather than staying on the clicked nav link.
    await user.click(screen.getByRole('link', { name: /^search$/i }));
    expect(await screen.findByText('search screen')).toBeInTheDocument();
    await waitFor(() => expect(main).toHaveFocus());
  });

  it('opens the account menu showing the signed-in principal', async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByRole('button', { name: /account menu/i }));
    const menu = await screen.findByRole('menu', { name: /account/i });
    // the embedded CurrentUserMenu (sign-out) is rendered inside the menu
    expect(within(menu).getByRole('button', { name: /sign out/i })).toBeInTheDocument();
    expect(within(menu).getAllByText('avery.madison@northwind.test').length).toBeGreaterThan(0);
  });
});
