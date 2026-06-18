/**
 * NavOverlay: a11y + keyboard behavior. Collapsed by default (links absent),
 * reveals on activation, exposes links as navigation, and Esc closes + restores
 * focus to the trigger.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { NavOverlay } from './NavOverlay';

const items = [
  { to: '/docs', label: 'Documentation', icon: '📖' },
  { to: '/features', label: 'Features built', icon: '✨' },
];

function renderOverlay() {
  return render(
    <MemoryRouter>
      <NavOverlay items={items} />
    </MemoryRouter>,
  );
}

describe('NavOverlay', () => {
  it('is collapsed by default with an accessible, unexpanded trigger', () => {
    renderOverlay();
    const trigger = screen.getByRole('button', { name: /developer pages/i });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('link', { name: /documentation/i })).not.toBeInTheDocument();
  });

  it('reveals the links on activation and exposes their routes', async () => {
    renderOverlay();
    const trigger = screen.getByRole('button', { name: /developer pages/i });

    await userEvent.click(trigger);

    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('link', { name: /documentation/i })).toHaveAttribute('href', '/docs');
    expect(screen.getByRole('link', { name: /features built/i })).toHaveAttribute(
      'href',
      '/features',
    );
  });

  it('closes on Escape and restores focus to the trigger', async () => {
    const user = userEvent.setup();
    renderOverlay();
    const trigger = screen.getByRole('button', { name: /developer pages/i });

    await user.click(trigger);
    expect(screen.getByRole('link', { name: /documentation/i })).toBeInTheDocument();

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('link', { name: /documentation/i })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
