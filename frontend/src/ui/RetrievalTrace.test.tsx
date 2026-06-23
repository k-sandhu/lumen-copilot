import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RetrievalTrace, type TraceStep } from './RetrievalTrace';

const STEPS: TraceStep[] = [
  { label: 'Searched Drive · 412 passages' },
  { label: 'Ranked top 6 by hybrid score' },
  { label: 'Excluded 38 — no access', excluded: true },
];

describe('RetrievalTrace', () => {
  it('renders the summary collapsed by default (steps hidden)', () => {
    render(<RetrievalTrace summary="3 sources · 1,204 passages · 38 excluded" steps={STEPS} />);
    expect(screen.getByText('3 sources · 1,204 passages · 38 excluded')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retrieval/i })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
    expect(screen.queryByText('Searched Drive · 412 passages')).not.toBeInTheDocument();
  });

  it('expands to reveal the steps, including excluded candidates', async () => {
    const user = userEvent.setup();
    render(<RetrievalTrace summary="…" steps={STEPS} />);
    await user.click(screen.getByRole('button', { name: /retrieval/i }));
    expect(screen.getByRole('button', { name: /retrieval/i })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByText('Searched Drive · 412 passages')).toBeInTheDocument();
    const excluded = screen.getByText('Excluded 38 — no access');
    expect(excluded.closest('.lc-trace__step')).toHaveClass('lc-trace__step--excluded');
  });

  it('honors defaultOpen', () => {
    render(<RetrievalTrace summary="…" steps={STEPS} defaultOpen />);
    expect(screen.getByText('Ranked top 6 by hybrid score')).toBeInTheDocument();
  });

  it('shows an empty message when expanded with no steps', async () => {
    const user = userEvent.setup();
    render(<RetrievalTrace summary="0 sources" steps={[]} />);
    await user.click(screen.getByRole('button', { name: /retrieval/i }));
    expect(screen.getByText(/no retrieval steps recorded/i)).toBeInTheDocument();
  });

  it('disables the toggle while loading', () => {
    render(<RetrievalTrace summary="loading…" loading />);
    expect(screen.getByRole('button', { name: /retrieval/i })).toBeDisabled();
  });
});
