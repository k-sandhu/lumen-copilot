/**
 * useFocusTrap — the ONE focus-trap shared by every aria-modal dialog. This is
 * the regression test for issue #163: before the hook existed, Tab could escape
 * an open modal to the page behind it. It exercises the contract directly on a
 * tiny harness so every dialog that adopts the hook inherits the guarantee:
 *   - focus moves inside on open (initialFocus, else first focusable),
 *   - Tab from the last focusable wraps to the first,
 *   - Shift+Tab from the first wraps to the last,
 *   - Escape calls onClose,
 *   - focus is restored to the opener on close.
 */
import { useRef, useState } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useFocusTrap } from './useFocusTrap';

function Harness({ onClose = () => {} }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstRef = useRef<HTMLButtonElement>(null);

  useFocusTrap(open, dialogRef, onClose, { initialFocus: firstRef });

  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        open dialog
      </button>
      {open && (
        <div ref={dialogRef} role="dialog" aria-modal="true" tabIndex={-1}>
          <button ref={firstRef} type="button">
            first
          </button>
          <button type="button">middle</button>
          <button type="button" onClick={() => setOpen(false)}>
            last
          </button>
        </div>
      )}
    </div>
  );
}

describe('useFocusTrap', () => {
  it('moves focus to the initialFocus target on open', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'open dialog' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'first' })).toHaveFocus());
  });

  it('wraps Tab from the last focusable back to the first', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'open dialog' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'first' })).toHaveFocus());

    // first → middle → last → (wrap) first
    await user.tab();
    expect(screen.getByRole('button', { name: 'middle' })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'last' })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'first' })).toHaveFocus();
  });

  it('wraps Shift+Tab from the first focusable back to the last', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'open dialog' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'first' })).toHaveFocus());

    await user.tab({ shift: true });
    expect(screen.getByRole('button', { name: 'last' })).toHaveFocus();
  });

  it('calls onClose on Escape', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Harness onClose={onClose} />);
    await user.click(screen.getByRole('button', { name: 'open dialog' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'first' })).toHaveFocus());

    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('restores focus to the opener when the dialog closes', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole('button', { name: 'open dialog' });
    await user.click(opener);
    await waitFor(() => expect(screen.getByRole('button', { name: 'first' })).toHaveFocus());

    // The "last" button closes the dialog; focus returns to the opener.
    await user.click(screen.getByRole('button', { name: 'last' }));
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
