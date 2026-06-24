/**
 * AdminTabs coverage (#122): a navigational segmented control — it switches the
 * visible panel and mutates NO state. Asserts the WAI-ARIA tablist contract
 * (role=tab, aria-selected, aria-controls, roving tabindex) and keyboard arrow
 * navigation, plus that selecting a tab calls onChange (and nothing more).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AdminTabs, type AdminTab } from './AdminTabs';
import { adminTabIds } from './tabIds';

const TABS: AdminTab[] = [
  { id: 'members', label: 'Members & roles', icon: 'user' },
  { id: 'models', label: 'Model governance', icon: 'database' },
  { id: 'data', label: 'Data minimization', icon: 'lock' },
];

function setup(value = 'members') {
  const onChange = vi.fn();
  render(<AdminTabs tabs={TABS} value={value} onChange={onChange} idPrefix="admin" />);
  return { onChange };
}

describe('AdminTabs', () => {
  it('renders a labelled tablist with one tab per entry', () => {
    setup();
    expect(screen.getByRole('tablist', { name: /admin sections/i })).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(TABS.length);
  });

  it('marks the active tab selected and wires aria-controls to its panel', () => {
    setup('models');
    const active = screen.getByRole('tab', { name: /model governance/i });
    expect(active).toHaveAttribute('aria-selected', 'true');
    expect(active).toHaveAttribute('aria-controls', adminTabIds('admin', 'models').panel);
    // Roving tabindex: only the active tab is in the tab order.
    expect(active).toHaveAttribute('tabindex', '0');
    expect(screen.getByRole('tab', { name: /members & roles/i })).toHaveAttribute(
      'tabindex',
      '-1',
    );
  });

  it('calls onChange when a tab is clicked', async () => {
    const { onChange } = setup('members');
    await userEvent.click(screen.getByRole('tab', { name: /data minimization/i }));
    expect(onChange).toHaveBeenCalledWith('data');
  });

  it('moves between tabs with arrow keys (roving focus)', async () => {
    const { onChange } = setup('members');
    screen.getByRole('tab', { name: /members & roles/i }).focus();
    await userEvent.keyboard('{ArrowRight}');
    expect(onChange).toHaveBeenCalledWith('models');
  });

  it('wraps from the last tab to the first with ArrowRight', async () => {
    const { onChange } = setup('data');
    screen.getByRole('tab', { name: /data minimization/i }).focus();
    await userEvent.keyboard('{ArrowRight}');
    expect(onChange).toHaveBeenCalledWith('members');
  });

  it('exposes no switches/checkboxes — it is navigation, not mutation', () => {
    setup();
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });
});
