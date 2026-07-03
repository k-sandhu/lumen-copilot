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

const WEB_CITATION: UiCitation = {
  id: 'w1',
  documentId: '',
  documentName: 'Latest AI regulations',
  chunkId: '',
  snippet: 'The EU AI Act enters into force in stages through 2026.',
  charStart: 0,
  charEnd: 54,
  url: 'https://www.example.org/ai/regulations?ref=1',
  webTitle: 'Latest AI regulations',
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
        answeredAt="2d ago"
        onOpenCitation={() => {}}
      />,
    );
    // Permission-checked status (grounded answer), the answer time, and actions.
    expect(screen.getByText(/permission-checked/i)).toBeInTheDocument();
    expect(screen.getByText(/answered 2d ago/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /copy answer/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /mark this answer helpful/i })).toBeInTheDocument();
  });

  it('labels the footer timestamp as the ANSWER time, never source freshness (#120 GUARD)', () => {
    // The message timestamp is when the ANSWER was produced, not when any cited
    // source was indexed — so the footer must say "answered", never "freshest" /
    // "last indexed" (which would fabricate source provenance).
    render(
      <MessageBubble
        role="assistant"
        content="Revenue rose in Q4."
        citations={[CITATION]}
        answeredAt="2d ago"
        onOpenCitation={() => {}}
      />,
    );
    expect(screen.getByText(/answered 2d ago/i)).toBeInTheDocument();
    expect(screen.queryByText(/freshest/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/last indexed/i)).not.toBeInTheDocument();
  });

  it('omits the answer footer while the turn is still streaming (#120)', () => {
    render(
      <MessageBubble
        role="assistant"
        content="Partial answer so f"
        citations={[]}
        streaming
        answeredAt="Just now"
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
        answeredAt="Just now"
        showNoCitationsNotice
        onOpenCitation={() => {}}
      />,
    );
    // The footer still renders (the answer time) but makes no permission claim.
    expect(screen.queryByText(/permission-checked/i)).not.toBeInTheDocument();
    expect(screen.getByText(/answered just now/i)).toBeInTheDocument();
  });

  it('wraps a streaming assistant answer in a polite live region (#163)', () => {
    render(
      <MessageBubble
        role="assistant"
        content="Partial answer so f"
        citations={[]}
        streaming
        onOpenCitation={() => {}}
      />,
    );
    // role="log" + aria-live="polite" so a screen reader hears tokens stream in;
    // non-atomic so it announces deltas, not the whole answer on each token.
    const live = screen.getByRole('log', { name: /assistant answer/i });
    expect(live).toHaveAttribute('aria-live', 'polite');
    expect(live).toHaveAttribute('aria-atomic', 'false');
  });

  it('does NOT keep the live region on a settled turn — answers are not re-announced (#163)', () => {
    render(
      <MessageBubble
        role="assistant"
        content="Revenue rose in Q4."
        citations={[CITATION]}
        answeredAt="2d ago"
        onOpenCitation={() => {}}
      />,
    );
    // Once settled, the live-region attributes are dropped so the finished answer
    // isn't read out a second time when the caret/footer mount.
    expect(screen.queryByRole('log')).not.toBeInTheDocument();
  });

  it('renders user messages as plain text (no markdown interpretation)', () => {
    render(
      <MessageBubble role="user" content="**not bold**" citations={[]} onOpenCitation={() => {}} />,
    );
    // The asterisks are shown literally; there is no <strong>.
    expect(screen.getByText('**not bold**')).toBeInTheDocument();
  });

  it('renders a web citation DISTINCTLY, with host + snippet + a safe link (#221 AC-2)', () => {
    render(
      <MessageBubble
        role="assistant"
        content="Per the latest guidance, the rules phase in through 2026."
        citations={[WEB_CITATION]}
        onOpenCitation={() => {}}
      />,
    );
    // Under a distinct "Web sources" label (not the corpus "Sources used").
    expect(screen.getByText('Web sources')).toBeInTheDocument();
    expect(screen.queryByText('Sources used')).not.toBeInTheDocument();
    // Host is surfaced (www. stripped) and the snippet is shown.
    expect(screen.getByText('example.org')).toBeInTheDocument();
    expect(screen.getByText(/EU AI Act enters into force/i)).toBeInTheDocument();
    // The external link opens in a new tab with rel="noopener noreferrer".
    const link = screen.getByRole('link', { name: /open .* in a new tab/i });
    expect(link).toHaveAttribute('href', WEB_CITATION.url);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('routes a web-citation row click to the inspector (#221)', async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    render(
      <MessageBubble
        role="assistant"
        content="Answer."
        citations={[WEB_CITATION]}
        onOpenCitation={onOpen}
      />,
    );
    await user.click(screen.getByRole('button', { name: /web source: latest ai regulations/i }));
    // Web rows carry no per-document freshness meta, so only the citation is passed.
    expect(onOpen).toHaveBeenCalledWith(WEB_CITATION);
  });

  it('shows the "web sources were used" disclosure ONLY when web was used (#221 AC-1)', () => {
    const { rerender } = render(
      <MessageBubble
        role="assistant"
        content="Answer grounded on a document."
        citations={[CITATION]}
        webUsed={false}
        onOpenCitation={() => {}}
      />,
    );
    // A document-only answer makes no web disclosure.
    expect(screen.queryByText(/web sources were used/i)).not.toBeInTheDocument();

    // A web-using answer discloses it.
    rerender(
      <MessageBubble
        role="assistant"
        content="Answer grounded on the web."
        citations={[WEB_CITATION]}
        webUsed
        onOpenCitation={() => {}}
      />,
    );
    expect(screen.getByText(/web sources were used/i)).toBeInTheDocument();
  });

  it('never renders an outbound link for an unsafe (non-http) web citation URL (#221)', () => {
    render(
      <MessageBubble
        role="assistant"
        content="Answer."
        citations={[{ ...WEB_CITATION, id: 'w-bad', url: 'javascript:alert(1)' }]}
        onOpenCitation={() => {}}
      />,
    );
    // A javascript: URL is not classified as web (no host) → it isn't rendered as
    // a web source at all, and certainly no outbound link is produced.
    expect(screen.queryByText('Web sources')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /open .* in a new tab/i })).not.toBeInTheDocument();
  });
});
