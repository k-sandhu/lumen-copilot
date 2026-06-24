/**
 * AppearanceMenu tests (issue #134) — the popover is a preferences DIALOG, not a
 * menu. Its content is grouped toggle-button controls (theme/mode/accent/density),
 * so exposing role="menu" gave assistive tech the wrong semantics (review finding).
 * This guards the exposed role so it can't regress.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { AppearanceMenu } from './AppearanceMenu';

describe('AppearanceMenu', () => {
  it('exposes the popover as a labelled dialog (not a menu)', async () => {
    const user = userEvent.setup();
    render(<AppearanceMenu />);

    const trigger = screen.getByRole('button', { name: /Appearance —/ });
    expect(trigger).toHaveAttribute('aria-haspopup', 'dialog');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('dialog')).toBeNull();

    await user.click(trigger);

    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('dialog', { name: 'Appearance & preferences' })).toBeInTheDocument();
    // Regression guard: the preferences popover must never be a menu.
    expect(screen.queryByRole('menu')).toBeNull();
  });
});
