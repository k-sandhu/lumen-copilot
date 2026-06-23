/**
 * AnswerFooter (#120, wireframe chat.html "answer-meta"): the trust-signal +
 * action row under a settled assistant answer.
 *
 * Asserts the honest contract:
 *  - "Permission-checked" shows only when the answer is grounded (≥1 source).
 *  - Freshness renders the derived "freshest <ago>" label, omitted when absent.
 *  - Helpful / Not-helpful are LOCAL-ONLY toggles (aria-pressed, mutually
 *    exclusive, clearable) — they send NOTHING (no backend feedback endpoint).
 *  - Copy writes the answer text to the clipboard, client-side only.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AnswerFooter } from './AnswerFooter';

const ANSWER = 'Northwind approved the Q3 pricing change on May 28.';

describe('AnswerFooter', () => {
  it('shows "Permission-checked" only when the answer is grounded', () => {
    const { rerender } = render(
      <AnswerFooter answerText={ANSWER} permissionChecked={false} freshness="2d ago" />,
    );
    expect(screen.queryByText(/permission-checked/i)).not.toBeInTheDocument();

    rerender(<AnswerFooter answerText={ANSWER} permissionChecked freshness="2d ago" />);
    expect(screen.getByText(/permission-checked/i)).toBeInTheDocument();
  });

  it('renders the freshness label and omits it when absent', () => {
    const { rerender } = render(<AnswerFooter answerText={ANSWER} permissionChecked />);
    expect(screen.queryByText(/freshest/i)).not.toBeInTheDocument();

    rerender(<AnswerFooter answerText={ANSWER} permissionChecked freshness="2d ago" />);
    expect(screen.getByText(/freshest 2d ago/i)).toBeInTheDocument();
  });

  it('renders nothing when there is no status and no freshness (no empty rule)', () => {
    const { container } = render(
      <AnswerFooter answerText={ANSWER} permissionChecked={false} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('toggles helpful / not-helpful as a mutually exclusive, clearable LOCAL vote', async () => {
    const user = userEvent.setup();
    render(<AnswerFooter answerText={ANSWER} permissionChecked />);

    const helpful = screen.getByRole('button', { name: /mark this answer helpful/i });
    const notHelpful = screen.getByRole('button', { name: /mark this answer not helpful/i });

    expect(helpful).toHaveAttribute('aria-pressed', 'false');
    expect(notHelpful).toHaveAttribute('aria-pressed', 'false');

    await user.click(helpful);
    expect(helpful).toHaveAttribute('aria-pressed', 'true');

    // Choosing the other clears the first (one-of toggle).
    await user.click(notHelpful);
    expect(helpful).toHaveAttribute('aria-pressed', 'false');
    expect(notHelpful).toHaveAttribute('aria-pressed', 'true');

    // Clicking the active vote again clears it (nothing persisted).
    await user.click(notHelpful);
    expect(notHelpful).toHaveAttribute('aria-pressed', 'false');
  });

  it('copies the answer text to the clipboard (client-side only)', async () => {
    // user-event installs its own clipboard stub on setup; let the component
    // write through it and read it back, asserting the exact text was copied.
    const user = userEvent.setup();
    render(<AnswerFooter answerText={ANSWER} permissionChecked />);

    await user.click(screen.getByRole('button', { name: /copy answer/i }));
    // The button flips to a "copied" confirmation once the write resolves.
    expect(await screen.findByRole('button', { name: /answer copied/i })).toBeInTheDocument();
    // The exact answer text landed on the clipboard, client-side only.
    expect(await navigator.clipboard.readText()).toBe(ANSWER);
  });
});
