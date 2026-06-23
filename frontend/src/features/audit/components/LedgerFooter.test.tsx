/**
 * Tests for the tamper-evident audit footer (#121) — the append-only ledger
 * status and the honest, page-scoped count.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { LedgerFooter } from './LedgerFooter';

describe('LedgerFooter', () => {
  it('states the append-only ledger property', () => {
    render(<LedgerFooter shown={10} />);
    expect(screen.getByText(/append-only ledger/i)).toBeInTheDocument();
    expect(screen.getByText(/tamper-evident/i)).toBeInTheDocument();
  });

  it('counts what is shown on this page, singularizing one event', () => {
    const { rerender } = render(<LedgerFooter shown={1} />);
    expect(screen.getByText(/showing 1 event on this page/i)).toBeInTheDocument();
    rerender(<LedgerFooter shown={3} />);
    expect(screen.getByText(/showing 3 events on this page/i)).toBeInTheDocument();
  });
});
