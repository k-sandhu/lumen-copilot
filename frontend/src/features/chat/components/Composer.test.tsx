/**
 * Composer (AC-1): send a message (button + Enter), Shift+Enter newline,
 * Send→Stop while streaming (cancellable), disabled empty state. The model
 * picker is embedded.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Composer } from './Composer';
import type { ChatModelInfo } from '@/api';

const MODELS: ChatModelInfo[] = [
  { id: 'm1', label: 'Model One', provider: 'p', tier: 'frontier', is_default: true },
];

function setup(overrides: Partial<React.ComponentProps<typeof Composer>> = {}) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  const onModelChange = vi.fn();
  render(
    <Composer
      models={MODELS}
      model="m1"
      onModelChange={onModelChange}
      busy={false}
      streaming={false}
      onSend={onSend}
      onStop={onStop}
      {...overrides}
    />,
  );
  return { onSend, onStop, onModelChange };
}

describe('Composer', () => {
  it('sends a trimmed message on Send click (AC-1)', async () => {
    const { onSend } = setup();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Message'), '  hello there  ');
    await user.click(screen.getByRole('button', { name: /send/i }));
    expect(onSend).toHaveBeenCalledWith('hello there');
  });

  it('sends on Enter, inserts a newline on Shift+Enter (AC-1)', async () => {
    const { onSend } = setup();
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    await user.type(input, 'line one{Shift>}{Enter}{/Shift}line two');
    expect(onSend).not.toHaveBeenCalled();
    expect(input).toHaveValue('line one\nline two');
    await user.type(input, '{Enter}');
    expect(onSend).toHaveBeenCalledWith('line one\nline two');
  });

  it('disables Send when the draft is empty', () => {
    setup();
    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled();
  });

  it('shows Stop instead of Send while streaming and cancels (AC-5)', async () => {
    const { onStop } = setup({ streaming: true, busy: true });
    const user = userEvent.setup();
    expect(screen.queryByRole('button', { name: /^send$/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /stop/i }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('does not send while disabled (no session)', async () => {
    const { onSend } = setup({ disabled: true });
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    expect(input).toBeDisabled();
    await user.type(input, 'hi{Enter}');
    expect(onSend).not.toHaveBeenCalled();
  });

  it('surfaces the knowledge-mode chips, "Company sources" active by default (#89)', () => {
    setup();
    const group = screen.getByRole('group', { name: /knowledge mode/i });
    const company = within(group).getByRole('button', { name: /company sources/i });
    expect(company).toHaveAttribute('aria-pressed', 'true');
    // "Selected sources" is offered but disabled until a collection scope exists
    // (we never imply a capability that isn't wired).
    expect(within(group).getByRole('button', { name: /selected sources/i })).toBeDisabled();
  });
});
