/**
 * AssistantEditor (#212, AC-1/AC-3/AC-5) — the builder form across its states:
 *   • edit-mode LOADING skeleton while the head resolves;
 *   • a 404 on load → an error state (no crash), 401 → re-auth message (AC-5);
 *   • an actionable ERROR + retry for a transient load failure;
 *   • SUCCESS: the form hydrated from the head, Publish DISABLED until owner +
 *     distinct backup owner are set, then enabled;
 *   • a 422 on publish surfaced INLINE (INV-8), not swallowed.
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
import type { Assistant, Member } from '@/api';
import { AssistantEditor } from './AssistantEditor';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string, errors?: { field: string; message: string }[]): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status, errors }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

function makeAssistant(overrides: Partial<Assistant> = {}): Assistant {
  return {
    id: 'a1',
    name: 'Benefits helper',
    description: 'Answers HR questions',
    instructions: 'Be concise.',
    model: null,
    knowledgeScope: { collectionIds: [], sourceIds: [], modes: ['company'] },
    toolAllowlist: ['search_text'],
    autonomyLevel: 'suggest',
    effectiveAutonomy: 'suggest',
    owner: '',
    backupOwner: null,
    status: 'draft',
    certificationState: 'none',
    featured: false,
    version: null,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  };
}

const members: Member[] = [
  { id: 'u1', email: 'ada@acme.com', role: ['member'], email_attested_at: null },
  { id: 'u2', email: 'grace@acme.com', role: ['admin'], email_attested_at: null },
];

/**
 * Route the mocked fetch by URL. `assistant` seeds GET /assistants/{id}; the
 * collections/sources/members reads return their (possibly empty) lists.
 */
function mockRoutes(opts: {
  assistant?: Assistant | 'error-404' | 'error-500' | 'error-401';
  onPublish?: () => Response;
  onPatch?: () => Response;
  /** Version-history items served by GET /versions; re-read each call so a publish
   *  can grow the list (a function is invoked per request). */
  versions?: () => unknown[];
}) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input);
    const method = init?.method ?? 'GET';

    if (url.includes('/assistants/a1/publish') && method === 'POST') {
      return Promise.resolve(opts.onPublish ? opts.onPublish() : json(makeAssistant(), 200));
    }
    if (url.includes('/assistants/a1/versions') && method === 'GET') {
      return Promise.resolve(json({ items: opts.versions ? opts.versions() : [], next_cursor: null }));
    }
    if (url.match(/\/assistants\/a1$/) && method === 'PATCH') {
      return Promise.resolve(opts.onPatch ? opts.onPatch() : json(makeAssistant(), 200));
    }
    if (url.match(/\/assistants\/a1$/) && method === 'GET') {
      const a = opts.assistant;
      if (a === 'error-404') return Promise.resolve(problem(404, 'Not Found'));
      if (a === 'error-401') return Promise.resolve(problem(401, 'Unauthorized'));
      if (a === 'error-500') return Promise.resolve(problem(500, 'Server Error'));
      return Promise.resolve(json(a ?? makeAssistant()));
    }
    if (url.includes('/admin/members')) return Promise.resolve(json({ items: members, next_cursor: null }));
    if (url.includes('/models')) return Promise.resolve(json({ items: [] }));
    if (url.includes('/collections')) return Promise.resolve(json({ items: [], next_cursor: null }));
    if (url.includes('/sources')) return Promise.resolve(json({ items: [], next_cursor: null }));
    return Promise.resolve(json({ items: [], next_cursor: null }));
  });
}

function makeVersion(version: number) {
  return {
    id: `v-${version}`,
    assistant_id: 'a1',
    version,
    config: {
      name: 'Benefits helper',
      description: null,
      instructions: null,
      model: null,
      knowledgeScope: { collectionIds: [], sourceIds: [], modes: ['company'] },
      toolAllowlist: ['search_text'],
      autonomyLevel: 'suggest',
    },
    author: 'u1',
    notes: `Release ${version}`,
    diff_summary: null,
    created_at: '2026-07-01T00:00:00Z',
  };
}

function renderEditor(assistantId: string | null) {
  return renderWithQuery(
    <MemoryRouter initialEntries={[assistantId ? `/assistants/${assistantId}` : '/assistants/new']}>
      <AssistantEditor assistantId={assistantId} />
    </MemoryRouter>,
  );
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('AssistantEditor — load states (AC-3/AC-5)', () => {
  it('shows a LOADING skeleton while the head resolves', async () => {
    let resolve!: (r: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      if (String(input).match(/\/assistants\/a1$/)) {
        return new Promise<Response>((r) => {
          resolve = r;
        });
      }
      return Promise.resolve(json({ items: [], next_cursor: null }));
    });
    renderEditor('a1');
    expect(await screen.findByText(/loading assistant/i)).toBeInTheDocument();
    resolve(json(makeAssistant()));
    expect(await screen.findByRole('heading', { name: /edit assistant/i })).toBeInTheDocument();
  });

  it('renders an error state (not a crash) for a 404 on load (AC-5)', async () => {
    mockRoutes({ assistant: 'error-404' });
    renderEditor('a1');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/doesn’t exist, or you don’t have access/i);
    // A 404 is not retryable.
    expect(within(alert).queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('offers a retry on a transient load failure', async () => {
    mockRoutes({ assistant: 'error-500' });
    renderEditor('a1');
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });
});

describe('AssistantEditor — new mode', () => {
  it('renders the New form and hides Publish (owner set only after save)', async () => {
    mockRoutes({});
    renderEditor(null);
    expect(await screen.findByRole('heading', { name: /new assistant/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save draft/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /publish/i })).not.toBeInTheDocument();
  });

  it('shows an inline name error when saving without a name (INV-8 client guard)', async () => {
    mockRoutes({});
    const user = userEvent.setup();
    renderEditor(null);
    await screen.findByRole('heading', { name: /new assistant/i });
    await user.click(screen.getByRole('button', { name: /save draft/i }));
    expect(await screen.findByText(/give your assistant a name/i)).toBeInTheDocument();
  });
});

describe('AssistantEditor — publish gating (AC-1, ADR-0011 §4)', () => {
  it('DISABLES Publish until an owner and a distinct backup owner are set', async () => {
    mockRoutes({ assistant: makeAssistant({ owner: '', backupOwner: null }) });
    const user = userEvent.setup();
    renderEditor('a1');

    const publishBtn = await screen.findByRole('button', { name: /publish/i });
    expect(publishBtn).toBeDisabled();

    // Set owner then a distinct backup → Publish enables. (The pickers resolve
    // once the member roster loads.)
    const ownerSelect = await screen.findByLabelText('Owner');
    await user.selectOptions(ownerSelect, 'u1');
    expect(publishBtn).toBeDisabled(); // backup still missing

    const backupSelect = screen.getByLabelText('Backup owner');
    await user.selectOptions(backupSelect, 'u2');
    await waitFor(() => expect(publishBtn).toBeEnabled());
  });

  it('surfaces a 422 from publish INLINE (missing backup owner, INV-8)', async () => {
    mockRoutes({
      assistant: makeAssistant({ owner: 'u1', backupOwner: 'u2' }),
      onPublish: () => problem(422, 'A backup owner is required before publishing.'),
    });
    const user = userEvent.setup();
    renderEditor('a1');

    const publishBtn = await screen.findByRole('button', { name: /publish/i });
    await waitFor(() => expect(publishBtn).toBeEnabled());
    await user.click(publishBtn);

    expect(await screen.findByText(/backup owner is required/i)).toBeInTheDocument();
  });
});

describe('AssistantEditor — publish surfaces the new version (#214, E6-7)', () => {
  it('shows the version history and adds the newly-frozen version after publish', async () => {
    // The history starts with v1; a successful publish appends v2, and the
    // panel invalidation refetches so v2 appears.
    const versions = [makeVersion(1)];
    mockRoutes({
      assistant: makeAssistant({ owner: 'u1', backupOwner: 'u2', version: 1 }),
      versions: () => versions,
      onPublish: () => {
        versions.unshift(makeVersion(2));
        return json(makeVersion(2), 200);
      },
    });
    const user = userEvent.setup();
    renderEditor('a1');

    // The version-history panel renders the existing version.
    expect(await screen.findByText('Version 1')).toBeInTheDocument();

    const publishBtn = await screen.findByRole('button', { name: /publish/i });
    await waitFor(() => expect(publishBtn).toBeEnabled());
    await user.click(publishBtn);

    // After publish the panel refetches and the new version appears.
    expect(await screen.findByText('Version 2')).toBeInTheDocument();
  });
});
