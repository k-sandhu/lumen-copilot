/**
 * VersionHistory (#214, F-AB-4, E6-7) — the append-only version-history panel:
 *   • LOADING skeleton while the first page resolves;
 *   • EMPTY ("no versions yet") for a never-published draft;
 *   • ERROR + retry for a transient failure;
 *   • SUCCESS: version cards, the CURRENT one badged, per-version config summary,
 *     and the field-level diff vs. the current head;
 *   • ROLLBACK issues POST /rollback {version} behind the ConfirmDialog and
 *     refetches the history on success;
 *   • a 422 from rollback surfaces INLINE (INV-8), not swallowed.
 *
 * Rendered against a mocked fetch routed by URL so a contract match is an
 * integration match (ADR-0006 Phase 1).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { Assistant, AssistantVersion, AssistantVersionConfig, Member } from '@/api';
import { VersionHistory } from './VersionHistory';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

const members: Member[] = [{ id: 'u1', email: 'ada@acme.com', role: ['member'] }];

function makeConfig(overrides: Partial<AssistantVersionConfig> = {}): AssistantVersionConfig {
  return {
    name: 'Benefits helper',
    description: 'Answers HR questions',
    instructions: 'Be concise.',
    model: null,
    knowledgeScope: { collectionIds: [], sourceIds: [], modes: ['company'] },
    toolAllowlist: ['search_text'],
    autonomyLevel: 'suggest',
    ...overrides,
  };
}

function makeVersion(version: number, overrides: Partial<AssistantVersion> = {}): AssistantVersion {
  return {
    id: `v-${version}`,
    assistant_id: 'a1',
    version,
    config: makeConfig(),
    author: 'u1',
    notes: `Release ${version}`,
    diff_summary: null,
    created_at: '2026-07-01T00:00:00Z',
    ...overrides,
  };
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
    owner: 'u1',
    backupOwner: 'u2',
    status: 'published',
    certificationState: 'none',
    featured: false,
    version: 2,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  };
}

/**
 * Route the mocked fetch. `versions` seeds GET /assistants/a1/versions (or an
 * error sentinel); `onRollback` overrides the POST /rollback response.
 */
function mockRoutes(opts: {
  versions?: AssistantVersion[] | 'error-500' | 'error-404';
  onRollback?: () => Response;
  onVersions?: () => Response;
}) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input);
    const method = init?.method ?? 'GET';

    if (url.includes('/assistants/a1/rollback') && method === 'POST') {
      return Promise.resolve(opts.onRollback ? opts.onRollback() : json(makeVersion(3), 200));
    }
    if (url.includes('/assistants/a1/versions') && method === 'GET') {
      if (opts.onVersions) return Promise.resolve(opts.onVersions());
      const v = opts.versions;
      if (v === 'error-500') return Promise.resolve(problem(500, 'Server Error'));
      if (v === 'error-404') return Promise.resolve(problem(404, 'Not Found'));
      return Promise.resolve(json({ items: v ?? [], next_cursor: null }));
    }
    if (url.includes('/models')) return Promise.resolve(json({ items: [] }));
    return Promise.resolve(json({ items: [], next_cursor: null }));
  });
}

function renderPanel(assistant: Assistant = makeAssistant()) {
  return renderWithQuery(<VersionHistory assistant={assistant} members={members} />);
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('VersionHistory — async states', () => {
  it('shows a LOADING skeleton while the first page resolves', async () => {
    let resolve!: (r: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      if (String(input).includes('/assistants/a1/versions')) {
        return new Promise<Response>((r) => {
          resolve = r;
        });
      }
      return Promise.resolve(json({ items: [] }));
    });
    renderPanel();
    expect(await screen.findByText(/loading version history/i)).toBeInTheDocument();
    resolve(json({ items: [makeVersion(2)], next_cursor: null }));
    expect(await screen.findByText('Version 2')).toBeInTheDocument();
  });

  it('shows an EMPTY state for a never-published draft', async () => {
    mockRoutes({ versions: [] });
    renderPanel(makeAssistant({ status: 'draft', version: null }));
    expect(await screen.findByText(/no versions yet/i)).toBeInTheDocument();
  });

  it('shows an ERROR state with retry on a transient failure', async () => {
    mockRoutes({ versions: 'error-500' });
    renderPanel();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('renders versions newest-first and MARKS the current one', async () => {
    mockRoutes({ versions: [makeVersion(2), makeVersion(1)] });
    renderPanel(makeAssistant({ version: 2 }));

    const v2 = await screen.findByText('Version 2');
    expect(v2).toBeInTheDocument();
    expect(screen.getByText('Version 1')).toBeInTheDocument();

    // The current version (v2) is badged "Current"; v1 offers a rollback.
    expect(screen.getByText('Current')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /roll back to v1/i })).toBeInTheDocument();
    // The current version is announced via aria-current on its card.
    const currentCard = v2.closest('[aria-current="true"]');
    expect(currentCard).not.toBeNull();
  });

  it('shows the field-level diff vs. the current head for a historical version', async () => {
    mockRoutes({
      versions: [
        makeVersion(2, { config: makeConfig({ instructions: 'Be thorough.' }) }),
        makeVersion(1, { config: makeConfig({ instructions: 'Be concise.' }) }),
      ],
    });
    renderPanel(makeAssistant({ version: 2 }));
    await screen.findByText('Version 1');
    // The historical version (v1) diffs against the head (v2): instructions changed.
    expect(screen.getByText(/differs from current \(v2\)/i)).toBeInTheDocument();
  });
});

describe('VersionHistory — rollback (E6-7, AC-2)', () => {
  it('issues POST /rollback {version} behind the confirm and refetches', async () => {
    const fetchMock = mockRoutes({ versions: [makeVersion(2), makeVersion(1)] });
    const user = userEvent.setup();
    renderPanel(makeAssistant({ version: 2 }));

    await screen.findByText('Version 1');
    await user.click(screen.getByRole('button', { name: /roll back to v1/i }));

    // The ConfirmDialog explains it creates a NEW version (history preserved).
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/creates a new version/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/preserved/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: /roll back to v1/i }));

    // The rollback call carried the target version number.
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url, init]) => {
        return String(url).includes('/assistants/a1/rollback') && init?.method === 'POST';
      });
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({ version: 1 });
    });

    // On success the versions endpoint is refetched (invalidation).
    await waitFor(() => {
      const getVersionsCalls = fetchMock.mock.calls.filter(([url, init]) => {
        return (
          String(url).includes('/assistants/a1/versions') && (init?.method ?? 'GET') === 'GET'
        );
      });
      expect(getVersionsCalls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it('surfaces a 422 from rollback INLINE (unknown version, INV-8)', async () => {
    mockRoutes({
      versions: [makeVersion(2), makeVersion(1)],
      onRollback: () => problem(422, 'That version does not exist.'),
    });
    const user = userEvent.setup();
    renderPanel(makeAssistant({ version: 2 }));

    await screen.findByText('Version 1');
    await user.click(screen.getByRole('button', { name: /roll back to v1/i }));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: /roll back to v1/i }));

    expect(await screen.findByText(/that version does not exist/i)).toBeInTheDocument();
  });
});
