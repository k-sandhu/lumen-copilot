/**
 * SearchPage (#84, #110) — the `/search` screen nests through the shared
 * shell-aware `PageChrome`. Inside the app shell the shell owns the chrome (top
 * bar + nav + theme + account), so the page must render ONLY the search body and
 * NO duplicate top bar — no back-to-Chat link, no theme toggle, no second account
 * menu (the regression PR #113 fixed). The search functionality (the composer,
 * the initial prompt) still renders.
 */
import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { InAppShellProvider } from '@/routes/shell/ShellContext';
import { SearchPage } from './SearchPage';

function renderInShell(inShell: boolean) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <InAppShellProvider value={inShell}>
          <SearchPage />
        </InAppShellProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe('SearchPage', () => {
  it('renders the search body without any duplicate top bar inside the shell', () => {
    renderInShell(true);

    // The shell owns the chrome — the page renders no back-to-Chat link, no theme
    // toggle, and no account menu of its own.
    expect(screen.queryByRole('link', { name: /chat/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /app/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /toggle color theme/i }),
    ).not.toBeInTheDocument();

    // Search functionality is intact: the composer and initial prompt render.
    expect(screen.getByRole('searchbox')).toBeInTheDocument();
    expect(
      screen.getByText(/search across your connected sources/i),
    ).toBeInTheDocument();
  });
});
