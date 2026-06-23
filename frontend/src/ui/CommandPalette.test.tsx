import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CommandPalette, type CommandItem } from './CommandPalette';

function makeItems(onRun: (id: string) => void): CommandItem[] {
  return [
    {
      id: 'chat',
      label: 'Go to Assistant',
      keywords: 'chat ask',
      icon: 'message-square',
      run: () => onRun('chat'),
    },
    {
      id: 'search',
      label: 'Search sources',
      keywords: 'find',
      icon: 'search',
      run: () => onRun('search'),
    },
    { id: 'audit', label: 'Open audit log', icon: 'list', run: () => onRun('audit') },
  ];
}

describe('CommandPalette', () => {
  it('renders nothing when closed', () => {
    render(<CommandPalette open={false} onClose={() => {}} items={makeItems(() => {})} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('renders a combobox + listbox of commands when open and focuses the input', () => {
    render(<CommandPalette open onClose={() => {}} items={makeItems(() => {})} />);
    expect(screen.getByRole('combobox', { name: /search commands/i })).toHaveFocus();
    expect(screen.getAllByRole('option')).toHaveLength(3);
  });

  it('filters by label and keywords', async () => {
    const user = userEvent.setup();
    render(<CommandPalette open onClose={() => {}} items={makeItems(() => {})} />);
    await user.type(screen.getByRole('combobox'), 'find');
    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveTextContent('Search sources');
  });

  it('shows the empty state when nothing matches', async () => {
    const user = userEvent.setup();
    render(<CommandPalette open onClose={() => {}} items={makeItems(() => {})} />);
    await user.type(screen.getByRole('combobox'), 'zzz');
    expect(screen.getByText(/no commands match/i)).toBeInTheDocument();
  });

  it('moves the active option with the arrow keys (aria-activedescendant follows)', async () => {
    const user = userEvent.setup();
    render(<CommandPalette open onClose={() => {}} items={makeItems(() => {})} />);
    const input = screen.getByRole('combobox');
    // first option is active initially
    const first = screen.getAllByRole('option')[0]!;
    expect(first).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('{ArrowDown}');
    const second = screen.getAllByRole('option')[1]!;
    expect(second).toHaveAttribute('aria-selected', 'true');
    expect(input).toHaveAttribute('aria-activedescendant', second.id);
  });

  it('runs the active command on Enter and closes', async () => {
    const onRun = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<CommandPalette open onClose={onClose} items={makeItems(onRun)} />);
    await user.keyboard('{ArrowDown}{Enter}');
    expect(onRun).toHaveBeenCalledWith('search');
    expect(onClose).toHaveBeenCalled();
  });

  it('runs a command on click', async () => {
    const onRun = vi.fn();
    const user = userEvent.setup();
    render(<CommandPalette open onClose={() => {}} items={makeItems(onRun)} />);
    await user.click(screen.getByRole('option', { name: /open audit log/i }));
    expect(onRun).toHaveBeenCalledWith('audit');
  });

  it('closes on Escape', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<CommandPalette open onClose={onClose} items={makeItems(() => {})} />);
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });
});
