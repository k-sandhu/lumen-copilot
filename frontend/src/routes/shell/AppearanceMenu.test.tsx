/**
 * AppearanceMenu tests (issue #134) — the popover is a preferences DIALOG, not a
 * menu. Its content is grouped toggle-button controls (theme/mode/accent/density),
 * so exposing role="menu" gave assistive tech the wrong semantics (review finding).
 * This guards the exposed role so it can't regress.
 *
 * The popover now also hosts the account-level default-model control (#167),
 * which reads GET /preferences + GET /models on open — hence the QueryClient
 * wrapper (renderWithQuery) and the stubbed fetch.
 */
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import { AppearanceMenu } from './AppearanceMenu';

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  // The default-model control's preferences query only runs once authenticated.
  setAccessToken('test-jwt');
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input);
    if (url.includes('/preferences')) {
      return jsonResponse({ default_model_id: null, updated_at: null });
    }
    if (url.includes('/models')) {
      return jsonResponse({
        items: [
          { id: 'm-default', label: 'Default', provider: 'p', tier: 'frontier', is_default: true },
        ],
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
});
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('AppearanceMenu', () => {
  it('exposes the popover as a labelled dialog (not a menu)', async () => {
    const user = userEvent.setup();
    renderWithQuery(<AppearanceMenu />);

    const trigger = screen.getByRole('button', { name: /Appearance —/ });
    expect(trigger).toHaveAttribute('aria-haspopup', 'dialog');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('dialog')).toBeNull();

    await user.click(trigger);

    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('dialog', { name: 'Appearance & preferences' })).toBeInTheDocument();
    // Regression guard: the preferences popover must never be a menu.
    expect(screen.queryByRole('menu')).toBeNull();
    // The account-level default-model control is part of the popover (#167);
    // awaiting it also flushes its on-open queries within act().
    expect(await screen.findByRole('combobox', { name: /default model/i })).toBeInTheDocument();
  });
});
