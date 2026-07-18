/**
 * Context panel (spec 0007 #432, AC-2): aggregates cited documents, the tool
 * trace, artifacts, and usage for one conversation; clicks route to the source
 * inspector / artifacts pane; sections handle empty and error states.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ContextPanel } from './ContextPanel';

const MESSAGES = {
  items: [
    {
      id: 'm1',
      session_id: 's1',
      role: 'user',
      content: 'q',
      citations: [],
      created_at: '2026-07-17T00:00:00Z',
    },
    {
      id: 'm2',
      session_id: 's1',
      role: 'assistant',
      content: 'a',
      citations: [
        {
          id: 'c1',
          document_id: 'doc-1',
          document_name: 'Budget FY26.xlsx',
          chunk_id: 'ch1',
          snippet: 's',
          char_start: 0,
          char_end: 5,
        },
        {
          id: 'c2',
          document_id: 'doc-1',
          document_name: 'Budget FY26.xlsx',
          chunk_id: 'ch2',
          snippet: 's2',
          char_start: 6,
          char_end: 9,
        },
      ],
      tool_invocations: [
        {
          id: 't1',
          tool_name: 'search_text',
          ok: true,
          duration_ms: 12,
          created_at: '2026-07-17T00:00:01Z',
        },
        {
          id: 't2',
          tool_name: 'web_search',
          ok: false,
          error: 'tool_error',
          duration_ms: 5,
          created_at: '2026-07-17T00:00:02Z',
        },
      ],
      created_at: '2026-07-17T00:00:03Z',
    },
  ],
  next_cursor: null,
};

const USAGE = {
  model: 'm',
  totals: {
    answers: 1,
    prompt_tokens: 1000,
    completion_tokens: 200,
    total_tokens: 1200,
    cached_prompt_tokens: 0,
    cache_write_tokens: 0,
  },
  input_budget_tokens: 100_000,
  window_known: true,
};

const ARTIFACTS = {
  items: [
    {
      id: 'a1',
      filename: 'report.md',
      mime_type: 'text/markdown',
      size_bytes: 2048,
      owner_id: 'u1',
      produced_by: 'tool',
      session_id: 's1',
      created_at: '2026-07-17T00:00:04Z',
    },
  ],
  next_cursor: null,
};

function mockRouter() {
  vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
    const url = String(input);
    const body = url.includes('/usage') ? USAGE : url.includes('/artifacts') ? ARTIFACTS : MESSAGES;
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });
}

afterEach(() => vi.restoreAllMocks());

describe('ContextPanel', () => {
  it('aggregates documents, tools, artifacts, and usage (AC-2)', async () => {
    mockRouter();
    const onOpenCitation = vi.fn();
    const onOpenArtifact = vi.fn();
    renderWithQuery(
      <ContextPanel
        sessionId="s1"
        onOpenCitation={onOpenCitation}
        onOpenArtifact={onOpenArtifact}
        onClose={vi.fn()}
      />,
    );
    const user = userEvent.setup();

    // Documents dedupe by id with a citation count.
    const docRow = await screen.findByRole('button', { name: 'Open Budget FY26.xlsx' });
    expect(docRow).toHaveTextContent('2 citations');
    await user.click(docRow);
    expect(onOpenCitation).toHaveBeenCalledWith(expect.objectContaining({ document_id: 'doc-1' }));

    // Tools aggregate with failure counts.
    expect(screen.getByText('search_text')).toBeInTheDocument();
    expect(screen.getByText(/1× · 1 failed/)).toBeInTheDocument();

    // Artifacts list and route to the artifacts pane.
    const artifactRow = await screen.findByRole('button', { name: 'View artifact report.md' });
    await user.click(artifactRow);
    expect(onOpenArtifact).toHaveBeenCalledWith('a1');

    // Usage stats render.
    await waitFor(() => expect(screen.getByText(/tokens total/)).toBeInTheDocument());
  });

  it('shows honest empty states', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      const body = url.includes('/usage')
        ? { ...USAGE, totals: { ...USAGE.totals, answers: 0, total_tokens: 0 } }
        : { items: [], next_cursor: null };
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    });
    renderWithQuery(
      <ContextPanel
        sessionId="s1"
        onOpenCitation={vi.fn()}
        onOpenArtifact={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(await screen.findByText('No documents cited yet.')).toBeInTheDocument();
    expect(screen.getByText('No tools invoked yet.')).toBeInTheDocument();
    expect(
      await screen.findByText('No artifacts produced in this conversation.'),
    ).toBeInTheDocument();
  });
});

describe('#434 round-1: retryable failures', () => {
  it('failed loads show Retry (and tools never fake an empty state)', async () => {
    let failMessages = true;
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      const fail = url.includes('/artifacts') || (url.includes('/messages') && failMessages);
      const body = url.includes('/usage')
        ? USAGE
        : url.includes('/artifacts')
          ? ARTIFACTS
          : MESSAGES;
      return Promise.resolve(
        new Response(JSON.stringify(fail ? { title: 'boom', status: 500 } : body), {
          status: fail ? 500 : 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    });
    const user = userEvent.setup();
    renderWithQuery(
      <ContextPanel
        sessionId="s1"
        onOpenCitation={vi.fn()}
        onOpenArtifact={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    // Messages failed: the documents section shows Retry AND the tools section
    // reads as an error too (two occurrences), NOT "No tools invoked yet".
    expect(await screen.findAllByText('Could not load messages.', { exact: false })).toHaveLength(
      2,
    );
    expect(screen.queryByText('No tools invoked yet.')).not.toBeInTheDocument();
    expect(
      await screen.findByText('Could not load artifacts.', { exact: false }),
    ).toBeInTheDocument();
    const retries = screen.getAllByRole('button', { name: 'Retry' });
    expect(retries.length).toBeGreaterThanOrEqual(2);
    // Retrying messages after the backend recovers renders the documents list.
    failMessages = false;
    await user.click(retries[0]!);
    expect(
      await screen.findByRole('button', { name: 'Open Budget FY26.xlsx' }),
    ).toBeInTheDocument();
  });
});
