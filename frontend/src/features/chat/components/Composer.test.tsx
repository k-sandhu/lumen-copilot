/**
 * Composer (AC-1): send a message (button + Enter), Shift+Enter newline,
 * Send→Stop while streaming (cancellable), disabled empty state. The model
 * picker + knowledge-mode control (#221) are embedded.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Composer } from './Composer';
import type { ChatModelInfo, KnowledgeMode } from '@/api';

const MODELS: ChatModelInfo[] = [
  { id: 'm1', label: 'Model One', provider: 'p', tier: 'frontier', is_default: true },
];

const DEFAULT_MODES: KnowledgeMode[] = ['company', 'uploaded'];

function setup(overrides: Partial<React.ComponentProps<typeof Composer>> = {}) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  const onModelChange = vi.fn();
  const onModesChange = vi.fn();
  render(
    <Composer
      models={MODELS}
      model="m1"
      onModelChange={onModelChange}
      busy={false}
      streaming={false}
      onSend={onSend}
      onStop={onStop}
      modes={DEFAULT_MODES}
      onModesChange={onModesChange}
      {...overrides}
    />,
  );
  return { onSend, onStop, onModelChange, onModesChange };
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

  it('offers "Set as default" and reports the chosen model (#144)', async () => {
    const onSetDefaultModel = vi.fn();
    setup({ defaultModelId: null, onSetDefaultModel });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /set as default/i }));
    expect(onSetDefaultModel).toHaveBeenCalledTimes(1);
  });

  it('shows a "Default" tag (not the button) when the model is already the default (#144)', () => {
    setup({ defaultModelId: 'm1', onSetDefaultModel: vi.fn() });
    expect(screen.queryByRole('button', { name: /set as default/i })).not.toBeInTheDocument();
    expect(screen.getByText('Default')).toBeInTheDocument();
  });

  it('surfaces the knowledge-mode control with the active modes pressed (#221)', () => {
    setup();
    const group = screen.getByRole('group', { name: /knowledge modes/i });
    // Company + Uploaded are active by default; each is a pressed toggle.
    expect(within(group).getByRole('button', { name: /company/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(within(group).getByRole('button', { name: /uploaded/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    // Model is offered but off by default.
    expect(within(group).getByRole('button', { name: /model/i })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('disables the Web mode toggle with a reason when web is not enabled (#221 AC-3)', () => {
    setup();
    const web = screen.getByRole('button', { name: 'Web' });
    expect(web).toBeDisabled();
    // The disabled reason is discoverable (tooltip + accessible description),
    // never a silent no-op.
    expect(web).toHaveAttribute('title', expect.stringMatching(/not enabled|off/i));
  });

  it('reports a mode toggle to the parent (#221)', async () => {
    const { onModesChange } = setup();
    const user = userEvent.setup();
    // Toggling "Model" on adds it to the active set, preserving canonical order.
    await user.click(screen.getByRole('button', { name: /model/i }));
    expect(onModesChange).toHaveBeenCalledWith(['company', 'uploaded', 'model']);
  });
});
