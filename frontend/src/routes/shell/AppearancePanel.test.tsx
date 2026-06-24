/**
 * AppearancePanel tests (issue #134) — renders every axis, reflects the active
 * selection via aria-pressed, fires the right setter on click, and gates Reset.
 * Driven by a stub appearance api (the panel takes it as a prop), so these assert
 * the UI contract without the store.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { AppearanceApi } from '@/ui';
import { AppearancePanel } from './AppearancePanel';

function makeApi(overrides: Partial<AppearanceApi> = {}): AppearanceApi {
  return {
    theme: 'aurora',
    mode: 'system',
    accent: null,
    density: 'cozy',
    resolved: 'light',
    setTheme: vi.fn(),
    setMode: vi.fn(),
    setAccent: vi.fn(),
    setDensity: vi.fn(),
    resetAccent: vi.fn(),
    ...overrides,
  };
}

describe('AppearancePanel', () => {
  it('renders the theme gallery, mode/density groups, and accent swatches', () => {
    render(<AppearancePanel appearance={makeApi()} />);
    expect(screen.getByRole('button', { name: /Aurora/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Forest/ })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Mode' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Density' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Accent color' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Teal' })).toBeInTheDocument();
  });

  it('marks the active theme, mode, and density via aria-pressed', () => {
    render(
      <AppearancePanel
        appearance={makeApi({ theme: 'forest', mode: 'dark', density: 'compact' })}
      />,
    );
    expect(screen.getByRole('button', { name: /Forest/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Compact' })).toHaveAttribute('aria-pressed', 'true');
    // a non-selected option is not pressed
    expect(screen.getByRole('button', { name: 'Light' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('fires the matching setter on click', async () => {
    const api = makeApi();
    const user = userEvent.setup();
    render(<AppearancePanel appearance={api} />);

    await user.click(screen.getByRole('button', { name: /Sunset/ }));
    expect(api.setTheme).toHaveBeenCalledWith('sunset');

    await user.click(screen.getByRole('button', { name: 'Dark' }));
    expect(api.setMode).toHaveBeenCalledWith('dark');

    await user.click(screen.getByRole('button', { name: 'Comfortable' }));
    expect(api.setDensity).toHaveBeenCalledWith('comfortable');

    await user.click(screen.getByRole('button', { name: 'Teal' }));
    expect(api.setAccent).toHaveBeenCalledWith('20 184 166');
  });

  it('gates Reset on whether an accent override is set', async () => {
    const api = makeApi({ accent: '20 184 166' });
    const user = userEvent.setup();
    const { rerender } = render(<AppearancePanel appearance={api} />);

    const reset = screen.getByRole('button', { name: 'Reset' });
    expect(reset).toBeEnabled();
    await user.click(reset);
    expect(api.resetAccent).toHaveBeenCalled();

    rerender(<AppearancePanel appearance={makeApi({ accent: null })} />);
    expect(screen.getByRole('button', { name: 'Reset' })).toBeDisabled();
  });
});
