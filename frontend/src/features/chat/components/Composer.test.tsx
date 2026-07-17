/**
 * Composer (AC-1): send a message (button + Enter), Shift+Enter newline,
 * Send→Stop while streaming (cancellable), disabled empty state. The model
 * picker + knowledge-mode control (#221) are embedded.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import { renderWithQuery } from '@/test/renderWithQuery';
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
  renderWithQuery(
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

// --- Spec 0006 (#429): ghost prefill (AC-4) + history recall (AC-5) ----------

describe('Composer ghost prefill (spec 0006 AC-4)', () => {
  it('shows the ghost over an empty composer and accepts it with Tab', async () => {
    const { onSend } = setup({ ghostSuggestion: 'What changed in Q2?' });
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    expect(screen.getByText('What changed in Q2?')).toBeInTheDocument();
    input.focus();
    await user.keyboard('{Tab}');
    expect(input).toHaveValue('What changed in Q2?');
    // Accepting fills the draft — it does not send.
    expect(onSend).not.toHaveBeenCalled();
    // The ghost is gone once the draft is non-empty.
    expect(
      screen.queryByRole('button', { name: /use suggested question/i }),
    ).not.toBeInTheDocument();
  });

  it('accepts via the click affordance and dismisses on typing', async () => {
    setup({ ghostSuggestion: 'Who owns this?' });
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    // Typing anything dismisses the ghost — it never overwrites user text.
    await user.type(input, 'my own question');
    expect(
      screen.queryByRole('button', { name: /use suggested question/i }),
    ).not.toBeInTheDocument();
    expect(input).toHaveValue('my own question');
    await user.clear(input);
    // Empty again — the ghost returns; clicking the affordance inserts it.
    await user.click(screen.getByRole('button', { name: /use suggested question/i }));
    expect(input).toHaveValue('Who owns this?');
  });

  it('Tab without a ghost keeps normal focus traversal', async () => {
    setup();
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    input.focus();
    await user.keyboard('{Tab}');
    // No ghost: the composer must not trap Tab.
    expect(input).toHaveValue('');
    expect(input).not.toHaveFocus();
  });
});

describe('Composer history recall (spec 0006 AC-5)', () => {
  const HISTORY = ['newest question', 'older question', 'oldest question'];

  it('ArrowUp walks previous messages newest-first; ArrowDown walks back and restores the draft', async () => {
    setup({ historyEntries: HISTORY });
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    await user.type(input, 'work in progress');
    await user.keyboard('{ArrowUp}');
    expect(input).toHaveValue('newest question');
    await user.keyboard('{ArrowUp}');
    expect(input).toHaveValue('older question');
    await user.keyboard('{ArrowUp}');
    expect(input).toHaveValue('oldest question');
    // Bounded at the oldest entry.
    await user.keyboard('{ArrowUp}');
    expect(input).toHaveValue('oldest question');
    await user.keyboard('{ArrowDown}');
    expect(input).toHaveValue('older question');
    await user.keyboard('{ArrowDown}');
    expect(input).toHaveValue('newest question');
    // Past the newest: the stashed in-progress draft is restored.
    await user.keyboard('{ArrowDown}');
    expect(input).toHaveValue('work in progress');
  });

  it('Escape restores the stashed draft immediately', async () => {
    setup({ historyEntries: HISTORY });
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    await user.type(input, 'my draft');
    await user.keyboard('{ArrowUp}');
    expect(input).toHaveValue('newest question');
    await user.keyboard('{Escape}');
    expect(input).toHaveValue('my draft');
  });

  it('is multiline-safe: ArrowUp only recalls from the first line', async () => {
    setup({ historyEntries: HISTORY });
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message') as HTMLTextAreaElement;
    await user.type(input, 'line one{Shift>}{Enter}{/Shift}line two');
    // Caret is at the end (second line): ArrowUp is ordinary caret movement.
    await user.keyboard('{ArrowUp}');
    expect(input).toHaveValue('line one\nline two');
    // Move the caret to the first line: now ArrowUp recalls.
    input.setSelectionRange(2, 2);
    await user.keyboard('{ArrowUp}');
    expect(input).toHaveValue('newest question');
  });

  it('editing a recalled entry ends navigation (the edit is the new draft)', async () => {
    setup({ historyEntries: HISTORY });
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    await user.click(input);
    await user.keyboard('{ArrowUp}');
    expect(input).toHaveValue('newest question');
    await user.type(input, ' plus edits');
    // Down no longer walks history — navigation ended on edit.
    await user.keyboard('{ArrowDown}');
    expect(input).toHaveValue('newest question plus edits');
  });
});

// --- Spec 0007 (#432): @-mention document pinning + pills --------------------

describe('Composer @-mention picker (spec 0007 AC-4)', () => {
  function mockSuggest(docs: { id: string; text: string }[]): void {
    // A NEW Response per call: bodies are single-use and the picker refetches
    // per keystroke.
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            suggestions: docs.map((d) => ({
              kind: 'document',
              text: d.text,
              document_id: d.id,
            })),
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
  }

  afterEach(() => vi.restoreAllMocks());

  it('opens on @, pins via Enter, and strips the token from the draft', async () => {
    mockSuggest([
      { id: 'doc-1', text: 'Budget FY26.xlsx' },
      { id: 'doc-2', text: 'Budget notes.md' },
    ]);
    const onPinDocument = vi.fn();
    const { onSend } = setup({ onPinDocument, pinnedDocuments: [] });
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    await user.type(input, 'Summarize @bud');
    const listbox = await screen.findByRole('listbox', { name: 'Pin a document' });
    expect(listbox).toBeInTheDocument();
    await screen.findByRole('option', { name: /Budget FY26\.xlsx/ });
    // Arrow to the second option, Enter pins it (Enter must NOT send).
    await user.keyboard('{ArrowDown}{Enter}');
    expect(onPinDocument).toHaveBeenCalledWith({ id: 'doc-2', name: 'Budget notes.md' });
    expect(onSend).not.toHaveBeenCalled();
    expect(input).toHaveValue('Summarize');
  });

  it('Escape dismisses the picker for the current token', async () => {
    mockSuggest([{ id: 'doc-1', text: 'Budget FY26.xlsx' }]);
    setup({ onPinDocument: vi.fn() });
    const user = userEvent.setup();
    const input = screen.getByLabelText('Message');
    await user.type(input, '@bud');
    await screen.findByRole('option', { name: /Budget FY26\.xlsx/ });
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('listbox', { name: 'Pin a document' })).not.toBeInTheDocument();
  });

  it('renders pinned pills and unpins via ×', async () => {
    const onUnpinDocument = vi.fn();
    setup({
      pinnedDocuments: [{ id: 'doc-1', name: 'Budget FY26.xlsx' }],
      onPinDocument: vi.fn(),
      onUnpinDocument,
    });
    const user = userEvent.setup();
    expect(screen.getByRole('group', { name: 'Pinned documents' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Unpin Budget FY26.xlsx' }));
    expect(onUnpinDocument).toHaveBeenCalledWith('doc-1');
  });

  it('already-pinned documents are excluded from the picker', async () => {
    mockSuggest([{ id: 'doc-1', text: 'Budget FY26.xlsx' }]);
    setup({
      pinnedDocuments: [{ id: 'doc-1', name: 'Budget FY26.xlsx' }],
      onPinDocument: vi.fn(),
      onUnpinDocument: vi.fn(),
    });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Message'), '@bud');
    expect(await screen.findByText('No matching documents.')).toBeInTheDocument();
  });
});
