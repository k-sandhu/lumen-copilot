/**
 * Suggested follow-up chips (spec 0006 #429, AC-3): click sends the suggestion;
 * disabled while a send/stream is in flight; empty list renders nothing.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SuggestionChips } from './SuggestionChips';

describe('SuggestionChips', () => {
  it('sends the clicked suggestion (AC-3)', async () => {
    const onPick = vi.fn();
    render(<SuggestionChips suggestions={['What changed?', 'Who owns it?']} onPick={onPick} />);
    const user = userEvent.setup();
    const group = screen.getByRole('group', { name: 'Suggested follow-up questions' });
    expect(group).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /who owns it\?/i }));
    expect(onPick).toHaveBeenCalledWith('Who owns it?');
  });

  it('renders nothing for an empty list and disables while busy', () => {
    const onPick = vi.fn();
    const { rerender } = render(<SuggestionChips suggestions={[]} onPick={onPick} />);
    expect(screen.queryByRole('group')).not.toBeInTheDocument();
    rerender(<SuggestionChips suggestions={['One?']} disabled onPick={onPick} />);
    expect(screen.getByRole('button', { name: /one\?/i })).toBeDisabled();
  });
});
