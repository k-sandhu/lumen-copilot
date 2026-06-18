/**
 * MessageBubble (AC-2/AC-5): assistant content rendered through the sanitized
 * markdown pipeline (never raw), citations as clickable references, and an
 * honest zero-citation notice (no fabricated refs). User input rendered as plain
 * text.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TooltipProvider } from '@/components/Tooltip';
import { MessageBubble } from './MessageBubble';
import type { UiCitation } from '../model/citation';

function renderBubble(ui: React.ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}

const CITATION: UiCitation = {
  id: 'c1',
  documentId: 'doc-9',
  documentName: 'Q4 strategy.pdf',
  chunkId: 'k1',
  snippet: 'Revenue grew 12% in Q4.',
  charStart: 100,
  charEnd: 140,
};

describe('MessageBubble', () => {
  it('renders assistant markdown (rendered, not raw)', () => {
    renderBubble(
      <MessageBubble
        role="assistant"
        content="**bold answer** with a `code` span"
        citations={[]}
        onOpenCitation={() => {}}
      />,
    );
    // The markdown is rendered to elements, not dumped as a raw string.
    expect(screen.getByText('bold answer').tagName).toBe('STRONG');
    expect(screen.getByText('code').tagName).toBe('CODE');
  });

  it('renders clickable citation references that open the viewer (AC-2)', async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderBubble(
      <MessageBubble
        role="assistant"
        content="Per the strategy doc, revenue rose."
        citations={[CITATION]}
        onOpenCitation={onOpen}
      />,
    );
    // Numbered chip + the document name are both clickable affordances.
    const chip = screen.getByRole('button', { name: /citation 1: open Q4 strategy\.pdf/i });
    await user.click(chip);
    expect(onOpen).toHaveBeenCalledWith(CITATION);
  });

  it('shows an honest zero-citation notice and NO references (AC-5)', () => {
    renderBubble(
      <MessageBubble
        role="assistant"
        content="I could not find that in your documents."
        citations={[]}
        showNoCitationsNotice
        onOpenCitation={() => {}}
      />,
    );
    expect(screen.getByText(/no sources were cited/i)).toBeInTheDocument();
    // No "Sources" footer — nothing fabricated.
    expect(screen.queryByText(/^sources$/i)).not.toBeInTheDocument();
  });

  it('renders user messages as plain text (no markdown interpretation)', () => {
    renderBubble(
      <MessageBubble role="user" content="**not bold**" citations={[]} onOpenCitation={() => {}} />,
    );
    // The asterisks are shown literally; there is no <strong>.
    expect(screen.getByText('**not bold**')).toBeInTheDocument();
  });
});
