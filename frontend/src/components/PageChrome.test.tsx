/**
 * PageChrome (issue #110) — the shared screen chrome is shell-aware: inside the
 * app shell it suppresses its own header/back-link/theme-toggle (the shell already
 * provides them) and renders only the body, so screens nest with no duplicate
 * chrome. Standalone (a dev page reached directly, outside the shell) it keeps the
 * full pinned top bar, as before.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PageChrome } from './PageChrome';
import { InAppShellProvider } from '@/routes/shell/ShellContext';

describe('PageChrome', () => {
  it('renders its own back-to-app chrome when standalone (outside the shell)', () => {
    render(
      <MemoryRouter>
        <PageChrome title="Documentation">body</PageChrome>
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: /app/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /toggle color theme/i })).toBeInTheDocument();
    expect(screen.getByText('body')).toBeInTheDocument();
  });

  it('suppresses its own chrome when inside the app shell (no duplicate header)', () => {
    render(
      <MemoryRouter>
        <InAppShellProvider value={true}>
          <PageChrome title="Audit log">body</PageChrome>
        </InAppShellProvider>
      </MemoryRouter>,
    );
    // no back-link, no theme toggle — the shell owns those
    expect(screen.queryByRole('link', { name: /app/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /toggle color theme/i })).not.toBeInTheDocument();
    // body still renders
    expect(screen.getByText('body')).toBeInTheDocument();
  });
});
