/**
 * MessageBubble (AC-2/AC-5 + #89 trust-signal re-skin): assistant content
 * rendered through the sanitized markdown pipeline (never raw); inline citation
 * chips (kit CitationChip) that open the SourceInspector; a per-source
 * FreshnessPill; a model badge; a collapsible RetrievalTrace; and an honest
 * zero-citation notice (no fabricated refs). User input rendered as plain text.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MessageBubble } from './MessageBubble';
import type { UiCitation } from '../model/citation';

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
    render(
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

  it('renders clickable citation chips that open the inspector (AC-2)', async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    render(
      <MessageBubble
        role="assistant"
        content="Per the strategy doc, revenue rose."
        citations={[CITATION]}
        onOpenCitation={onOpen}
      />,
    );
    // The kit CitationChip is a real button named "Citation 1: <source>".
    const chip = screen.getByRole('button', { name: /citation 1: Q4 strategy\.pdf/i });
    await user.click(chip);
    // The chip opens the inspector with the citation (and optional source meta).
    expect(onOpen).toHaveBeenCalledWith(CITATION, undefined);
  });

  it('shows a FreshnessPill on a cited source when freshness is known (#89)', () => {
    render(
      <MessageBubble
        role="assistant"
        content="Answer."
        citations={[CITATION]}
        sourceMeta={{ 'doc-9': { freshness: '2d ago', stale: false } }}
        onOpenCitation={() => {}}
      />,
    );
    expect(screen.getByText('2d ago')).toBeInTheDocument();
  });

  it('renders a model badge and a collapsible retrieval trace (#89)', async () => {
    const user = userEvent.setup();
    render(
      <MessageBubble
        role="assistant"
        content="Answer."
        model="anthropic/claude-opus-4.8"
        modelLabel="Claude Opus 4.8"
        citations={[CITATION]}
        traceSummary="Looked at 1 source · 412 passages"
        traceSteps={[{ label: 'Searched sources — 412 passages' }]}
        onOpenCitation={() => {}}
      />,
    );
    expect(screen.getByText('Claude Opus 4.8')).toBeInTheDocument();
    // The trace summary shows; the step is hidden until expanded.
    expect(screen.getByText('Looked at 1 source · 412 passages')).toBeInTheDocument();
    expect(screen.queryByText('Searched sources — 412 passages')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /retrieval/i }));
    expect(screen.getByText('Searched sources — 412 passages')).toBeInTheDocument();
  });

  it('shows an honest zero-citation notice and NO references (AC-5)', () => {
    render(
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

  it('renders the answer footer on a settled grounded assistant turn (#120)', () => {
    render(
      <MessageBubble
        role="assistant"
        content="Revenue rose in Q4."
        citations={[CITATION]}
        freshness="2d ago"
        onOpenCitation={() => {}}
      />,
    );
    // Permission-checked status (grounded answer), freshness, and the actions.
    expect(screen.getByText(/permission-checked/i)).toBeInTheDocument();
    expect(screen.getByText(/freshest 2d ago/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /copy answer/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /mark this answer helpful/i })).toBeInTheDocument();
  });

  it('omits the answer footer while the turn is still streaming (#120)', () => {
    render(
      <MessageBubble
        role="assistant"
        content="Partial answer so f"
        citations={[]}
        streaming
        freshness="Just now"
        onOpenCitation={() => {}}
      />,
    );
    expect(screen.queryByRole('button', { name: /copy answer/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/permission-checked/i)).not.toBeInTheDocument();
  });

  it('does not claim "Permission-checked" on an ungrounded answer (#120, honest)', () => {
    render(
      <MessageBubble
        role="assistant"
        content="I could not find that in your documents."
        citations={[]}
        freshness="Just now"
        showNoCitationsNotice
        onOpenCitation={() => {}}
      />,
    );
    // The footer still renders (freshness) but makes no permission claim.
    expect(screen.queryByText(/permission-checked/i)).not.toBeInTheDocument();
    expect(screen.getByText(/freshest just now/i)).toBeInTheDocument();
  });

  it('renders user messages as plain text (no markdown interpretation)', () => {
    render(
      <MessageBubble role="user" content="**not bold**" citations={[]} onOpenCitation={() => {}} />,
    );
    // The asterisks are shown literally; there is no <strong>.
    expect(screen.getByText('**not bold**')).toBeInTheDocument();
  });
});
