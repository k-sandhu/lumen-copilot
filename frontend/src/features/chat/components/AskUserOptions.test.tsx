/**
 * Clarifying-question options (spec 0006 #429, AC-2): clickable while active
 * (sends the label), inert once answered/superseded, chosen option highlighted.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AskUserOptions } from './AskUserOptions';
import type { AskUserQuestion } from '@/api';

const QUESTION: AskUserQuestion = {
  question: 'Which quarter did you mean?',
  options: [{ label: 'Q1 2026', description: 'January through March' }, { label: 'Q2 2026' }],
  allow_free_text: true,
};

describe('AskUserOptions', () => {
  it('sends the clicked option label while active (AC-2)', async () => {
    const onChoose = vi.fn();
    render(<AskUserOptions question={QUESTION} active onChoose={onChoose} />);
    const user = userEvent.setup();
    const group = screen.getByRole('group', { name: 'Answer options' });
    expect(group).toBeInTheDocument();
    expect(screen.getByText('January through March')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Q2 2026/ }));
    expect(onChoose).toHaveBeenCalledWith('Q2 2026');
    // The free-text path stays advertised while answerable.
    expect(screen.getByText(/type your own answer/i)).toBeInTheDocument();
  });

  it('renders inert once inactive, highlighting the chosen option', () => {
    const onChoose = vi.fn();
    render(
      <AskUserOptions
        question={QUESTION}
        active={false}
        chosenAnswer="q1 2026"
        onChoose={onChoose}
      />,
    );
    const chosen = screen.getByRole('button', { name: /Q1 2026/ });
    const other = screen.getByRole('button', { name: /Q2 2026/ });
    expect(chosen).toBeDisabled();
    expect(other).toBeDisabled();
    // Case-insensitive match against the following user turn.
    expect(chosen).toHaveAttribute('aria-pressed', 'true');
    expect(other).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByText(/type your own answer/i)).not.toBeInTheDocument();
  });
});
