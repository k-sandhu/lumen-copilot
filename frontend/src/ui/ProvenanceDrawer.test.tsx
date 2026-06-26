import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProvenanceDrawer, type ProvenanceDetail } from './ProvenanceDrawer';

const DETAIL: ProvenanceDetail = {
  id: 'evt_9f3a',
  meta: [
    { key: 'Actor', value: 'dana@acme' },
    { key: 'Model', value: 'anthropic/opus' },
  ],
  candidates: [
    { title: 'Q3 Revenue.pdf', decision: 'allowed', reason: 'rank 1' },
    { title: 'Board deck.pptx', decision: 'excluded', reason: 'no access' },
  ],
  raw: { event: 'answer', tenant: 'acme' },
};

describe('ProvenanceDrawer', () => {
  it('renders nothing when closed', () => {
    render(<ProvenanceDrawer open={false} detail={DETAIL} onClose={() => {}} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('renders a labelled modal dialog when open', () => {
    render(<ProvenanceDrawer open detail={DETAIL} onClose={() => {}} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName(/provenance · evt_9f3a/i);
  });

  it('lists metadata, allow/exclude candidates, and the raw payload', () => {
    render(<ProvenanceDrawer open detail={DETAIL} onClose={() => {}} />);
    expect(screen.getByText('dana@acme')).toBeInTheDocument();
    // both the allowed AND the excluded candidate are provable after the fact
    expect(screen.getByText('Q3 Revenue.pdf')).toBeInTheDocument();
    expect(screen.getByText('Board deck.pptx')).toBeInTheDocument();
    expect(screen.getByText('no access')).toBeInTheDocument();
    expect(screen.getByText(/"tenant": "acme"/)).toBeInTheDocument();
  });

  it('moves focus into the drawer on open', async () => {
    render(<ProvenanceDrawer open detail={DETAIL} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole('dialog')).toHaveFocus());
  });

  it('closes on Escape', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<ProvenanceDrawer open detail={DETAIL} onClose={onClose} />);
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });

  it('closes on a backdrop click', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<ProvenanceDrawer open detail={DETAIL} onClose={onClose} />);
    await user.click(screen.getByRole('button', { name: /close provenance drawer/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it('traps Tab within the dialog — focus never escapes to the page (#163)', async () => {
    const user = userEvent.setup();
    render(<ProvenanceDrawer open detail={DETAIL} onClose={() => {}} />);
    const dialog = screen.getByRole('dialog');

    // Tabbing repeatedly always lands on an element inside the dialog; focus
    // never leaks to the backdrop or document body behind the overlay.
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });
});
