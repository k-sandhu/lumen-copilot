/**
 * DescribeAssistant (#213, E6-1) — the conversational agent builder surface:
 *   • it drafts from a plain-language description and PRE-FILLS the editor with the
 *     drafted config (AC-1 / "FE loads the draft into the editor");
 *   • it renders the builder's clarifications + notes + warnings above the form
 *     (AC-2 clarifications / AC-3 high-risk warning);
 *   • a blank description is blocked client-side with an inline error;
 *   • a transient draft failure surfaces an actionable, non-crashing error.
 *
 * Rendered against a mocked fetch routed by URL so a contract match is an
 * integration match (ADR-0006 Phase 1).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { AssistantDraft } from '@/api';
import { DescribeAssistant } from './DescribeAssistant';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string, detail?: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status, detail }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

function makeDraft(overrides: Partial<AssistantDraft> = {}): AssistantDraft {
  return {
    draft: {
      name: 'Benefits helper',
      description: 'Answers HR benefits questions',
      instructions: 'You are a friendly benefits assistant. Cite the policy.',
      model: null,
      knowledgeScope: { collectionIds: ['c1'], sourceIds: [], modes: [] },
      toolAllowlist: ['search_documents'],
      autonomyLevel: 'draft',
    },
    clarifications: ['Who is the accountable owner, and who is the backup owner?'],
    notes: [],
    warnings: [],
    ...overrides,
  };
}

/**
 * Route the mocked fetch by URL. POST /assistants/draft returns the given draft (or
 * a scripted error); the editor's reference reads (models/collections/sources/
 * members) return their empty lists.
 */
function mockRoutes(opts: { onDraft?: () => Response }) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input);
    const method = init?.method ?? 'GET';

    if (url.includes('/assistants/draft') && method === 'POST') {
      return Promise.resolve(opts.onDraft ? opts.onDraft() : json(makeDraft()));
    }
    if (url.includes('/admin/members')) {
      return Promise.resolve(json({ items: [], next_cursor: null }));
    }
    if (url.includes('/models')) return Promise.resolve(json({ items: [] }));
    if (url.includes('/collections')) return Promise.resolve(json({ items: [], next_cursor: null }));
    if (url.includes('/sources')) return Promise.resolve(json({ items: [], next_cursor: null }));
    return Promise.resolve(json({ items: [], next_cursor: null }));
  });
}

function renderDescribe() {
  return renderWithQuery(
    <MemoryRouter initialEntries={['/assistants/describe']}>
      <DescribeAssistant />
    </MemoryRouter>,
  );
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('DescribeAssistant — draft → editor (E6-1, #213)', () => {
  it('drafts from a description and pre-fills the editor with the config (AC-1)', async () => {
    mockRoutes({});
    renderDescribe();
    const user = userEvent.setup();

    await user.type(
      screen.getByLabelText(/what should it do/i),
      'A benefits helper that answers from the HR handbook.',
    );
    await user.click(screen.getByRole('button', { name: /draft it/i }));

    // The editor takes over, pre-filled with the drafted name + instructions.
    expect(await screen.findByRole('heading', { name: /new assistant/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/^name/i)).toHaveValue('Benefits helper');
    expect(screen.getByLabelText(/instructions/i)).toHaveValue(
      'You are a friendly benefits assistant. Cite the policy.',
    );
    // The "Save draft" CTA (new mode) is present — nothing was created yet.
    expect(screen.getByRole('button', { name: /save draft/i })).toBeInTheDocument();
  });

  it('shows the builder clarifications above the pre-filled form (AC-2)', async () => {
    mockRoutes({});
    renderDescribe();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/what should it do/i), 'Make me an assistant.');
    await user.click(screen.getByRole('button', { name: /draft it/i }));

    const advisories = await screen.findByTestId('draft-advisories');
    expect(within(advisories).getByText(/accountable owner/i)).toBeInTheDocument();
    expect(within(advisories).getByText(/a few questions to answer/i)).toBeInTheDocument();
  });

  it('surfaces a high-risk tool warning above the form (AC-3)', async () => {
    mockRoutes({
      onDraft: () =>
        json(
          makeDraft({
            draft: {
              name: 'Writer',
              description: null,
              instructions: null,
              model: null,
              knowledgeScope: { collectionIds: [], sourceIds: [], modes: [] },
              toolAllowlist: ['write_file'],
              autonomyLevel: 'suggest',
            },
            warnings: ["'write_file' is a higher-risk (T1) tool that can take consequential actions."],
            clarifications: ['Do you acknowledge the risk and want to keep it?'],
          }),
        ),
    });
    renderDescribe();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/what should it do/i), 'An assistant that writes files.');
    await user.click(screen.getByRole('button', { name: /draft it/i }));

    const advisories = await screen.findByTestId('draft-advisories');
    expect(within(advisories).getByText(/review before publishing/i)).toBeInTheDocument();
    expect(within(advisories).getByText(/write_file/)).toBeInTheDocument();
    expect(within(advisories).getByText(/acknowledge the risk/i)).toBeInTheDocument();
  });

  it('renders the omission notes when the builder drops a tool/scope (AC-N)', async () => {
    mockRoutes({
      onDraft: () =>
        json(
          makeDraft({
            notes: ['Omitted tool(s) the description referenced but that are not available: foo.'],
          }),
        ),
    });
    renderDescribe();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/what should it do/i), 'An assistant that can do foo.');
    await user.click(screen.getByRole('button', { name: /draft it/i }));

    const advisories = await screen.findByTestId('draft-advisories');
    expect(within(advisories).getByText(/the builder adjusted your draft/i)).toBeInTheDocument();
    expect(within(advisories).getByText(/not available/i)).toBeInTheDocument();
  });

  it('blocks a blank description client-side with an inline error', async () => {
    const fetchSpy = mockRoutes({});
    renderDescribe();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /draft it/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/describe what you want/i);
    // The draft endpoint was never called (client-side guard).
    const drafted = fetchSpy.mock.calls.some(
      ([input, init]) => String(input).includes('/assistants/draft') && init?.method === 'POST',
    );
    expect(drafted).toBe(false);
  });

  it('surfaces an actionable error on a transient draft failure (not a crash)', async () => {
    mockRoutes({ onDraft: () => problem(503, 'Service Unavailable', 'The model provider is unavailable.') });
    renderDescribe();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/what should it do/i), 'An assistant.');
    await user.click(screen.getByRole('button', { name: /draft it/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/model provider is unavailable/i);
    // Still on the describe form (no editor takeover), so the user can retry.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /draft it/i })).toBeInTheDocument(),
    );
  });
});
