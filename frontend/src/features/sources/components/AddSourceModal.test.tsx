/**
 * AddSourceModal (#27, ADR-0009 §5; #455, ADR-0019 §1/§5) — add a source.
 * Tested against a mocked fetch so a contract match is an integration match
 * (ADR-0006 Phase 1): client validation blocks a bad URL before any request; a
 * valid web submit POSTs `{type:'web', url}` and closes on the 201; a 422
 * (invalid / SSRF-blocked URL, ADR-0009 §3) renders INLINE and the modal stays
 * open; the dialog is a labelled modal that moves focus to the field on open.
 *
 * Managed (`gdrive`) coverage: the connector picker renders ONLY for a tenant
 * admin (INV-5 — a non-admin sees no managed affordance at all); the config
 * step builds the EXACT closed mode-discriminated variants (my_drive / folder
 * [+optional drive_id] / shared_drive); submit runs create → connect → browser
 * navigation to the returned `authorization_url`; a create 403 (the admin
 * gate) and a connect failure both render INLINE — never a blank pane.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { GdriveSource, Source } from '@/api';
import { AddSourceModal } from './AddSourceModal';
import { navigateToConsent } from '../model/browser';

// The consent redirect leaves the SPA entirely — mock the one navigation seam
// (jsdom cannot navigate).
vi.mock('../model/browser', () => ({ navigateToConsent: vi.fn() }));

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, body: Record<string, unknown>): Response {
  return new Response(JSON.stringify({ type: 'about:blank', status, ...body }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

const created: Source = {
  id: 's-new',
  type: 'web',
  config: { url: 'https://acme.com/docs', mode: 'page' },
  status: 'pending',
  indexed_count: 0,
  last_synced_at: null,
  owner_id: 'u1',
  created_at: '2026-06-23T12:00:00Z',
  updated_at: '2026-06-23T12:00:00Z',
};

const createdGdrive: GdriveSource = {
  id: 'g-new',
  type: 'gdrive',
  config: { mode: 'my_drive' },
  status: 'pending_auth',
  indexed_count: 0,
  last_synced_at: null,
  connected_account: null,
  acl_synced_at: null,
  unmapped_acl_count: null,
  reauthorize_required: false,
  owner_id: 'u1',
  created_at: '2026-07-18T12:00:00Z',
  updated_at: '2026-07-18T12:00:00Z',
};

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
  vi.mocked(navigateToConsent).mockReset();
});

describe('AddSourceModal', () => {
  it('renders nothing when closed', () => {
    renderWithQuery(<AddSourceModal open={false} onClose={() => {}} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('is a labelled modal dialog and focuses the URL field on open', async () => {
    renderWithQuery(<AddSourceModal open onClose={() => {}} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName(/add a source/i);
    const field = screen.getByLabelText(/link/i);
    await waitFor(() => expect(field).toHaveFocus());
  });

  it('traps Tab within the dialog — focus never escapes the open modal (#163)', async () => {
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={() => {}} />);
    const dialog = screen.getByRole('dialog');
    await waitFor(() => expect(screen.getByLabelText(/link/i)).toHaveFocus());

    // Tabbing forward and backward keeps focus inside the dialog (URL field →
    // Add source → Cancel → Close, wrapping at the edges), never on the page.
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it('keeps the Tab trap ACTIVE while the create is pending (#163)', async () => {
    // A slow/hung POST /sources holds `create.isPending` true. The Tab trap must
    // stay installed — gating it off during submission would run the hook's
    // cleanup and let focus escape to the page BEHIND the still-mounted overlay.
    // fetch never resolves so we stay pending. We render a focusable control
    // OUTSIDE the dialog (a stand-in for the page behind the overlay); a live
    // trap must never let Tab land on it.
    vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise<Response>(() => {}));
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(
      <>
        <button type="button" data-testid="behind-overlay">
          behind the overlay
        </button>
        <AddSourceModal open onClose={onClose} />
      </>,
    );

    const dialog = screen.getByRole('dialog');
    const outside = screen.getByTestId('behind-overlay');
    await user.type(screen.getByLabelText(/link/i), 'https://acme.com/docs');
    await user.click(screen.getByRole('button', { name: /add source/i }));

    // Now submitting: the button shows the pending label and inputs are disabled.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /adding/i })).toBeInTheDocument(),
    );
    expect(screen.getByLabelText(/link/i)).toBeDisabled();

    // The Tab trap is still live: cycling forward/backward keeps focus inside the
    // dialog (its controls are disabled, so the trap parks focus on the container)
    // and NEVER escapes to the focusable element behind the overlay.
    for (let i = 0; i < 4; i++) {
      await user.tab();
      expect(outside).not.toHaveFocus();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.tab({ shift: true });
    expect(outside).not.toHaveFocus();
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('blocks a bad URL client-side without firing a request', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={() => {}} />);

    await user.type(screen.getByLabelText(/link/i), 'not a url');
    await user.click(screen.getByRole('button', { name: /add source/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/valid url/i);
    expect(screen.getByLabelText(/link/i)).toHaveAttribute('aria-invalid', 'true');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('POSTs {type:web, url} and closes on a 201', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(created, 201));
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={onClose} />);

    await user.type(screen.getByLabelText(/link/i), 'https://acme.com/docs');
    await user.click(screen.getByRole('button', { name: /add source/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const call = fetchSpy.mock.calls[0];
    expect(call).toBeDefined();
    const init = call?.[1];
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({ type: 'web', url: 'https://acme.com/docs' });
  });

  it('shows an INLINE error and stays open on a 422 SSRF block (url_blocked)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problem(422, { title: 'Unprocessable', code: 'url_blocked' }),
    );
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={onClose} />);

    await user.type(screen.getByLabelText(/link/i), 'http://169.254.169.254/latest');
    await user.click(screen.getByRole('button', { name: /add source/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/blocked or private address/i);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('clears the server error once the user edits the field', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problem(422, { title: 'Unprocessable', code: 'url_blocked' }),
    );
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={() => {}} />);

    await user.type(screen.getByLabelText(/link/i), 'http://10.0.0.1/');
    await user.click(screen.getByRole('button', { name: /add source/i }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();

    await user.type(screen.getByLabelText(/link/i), 'x');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('AddSourceModal — managed gdrive flow (ADR-0019 §1/§5)', () => {
  it('shows NO Google Drive option to a non-admin (INV-5 — no managed affordance)', () => {
    renderWithQuery(<AddSourceModal open onClose={() => {}} isAdmin={false} />);
    expect(screen.queryByRole('radio', { name: /^google drive/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/connector/i)).not.toBeInTheDocument();
    // The web form is still fully usable.
    expect(screen.getByLabelText(/link/i)).toBeInTheDocument();
  });

  it('lets an admin pick Google Drive and the sync scope', async () => {
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={() => {}} isAdmin />);

    await user.click(screen.getByRole('radio', { name: /^google drive/i }));
    // The three closed config modes are offered; My Drive is the default.
    expect(screen.getByRole('radio', { name: /^my drive/i })).toBeChecked();
    expect(screen.getByRole('radio', { name: /^a folder/i })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /^a shared drive/i })).toBeInTheDocument();
    // No id fields for My Drive (the closed variant takes none).
    expect(screen.queryByLabelText(/folder id/i)).not.toBeInTheDocument();
  });

  it('creates {type:gdrive, config:{mode:my_drive}}, connects, and navigates to the consent URL', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/sources/g-new/connect')) {
        return Promise.resolve(
          json({ authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=opaque' }),
        );
      }
      return Promise.resolve(json(createdGdrive, 201));
    });
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={() => {}} isAdmin />);

    await user.click(screen.getByRole('radio', { name: /^google drive/i }));
    await user.click(screen.getByRole('button', { name: /continue to google/i }));

    await waitFor(() =>
      expect(navigateToConsent).toHaveBeenCalledWith(
        'https://accounts.google.com/o/oauth2/v2/auth?state=opaque',
      ),
    );
    const createCall = fetchSpy.mock.calls.find(
      ([u, i]) => String(u).endsWith('/sources') && i?.method === 'POST',
    );
    expect(createCall).toBeDefined();
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
      type: 'gdrive',
      config: { mode: 'my_drive' },
    });
  });

  it('builds the folder variant with the optional drive_id only when given', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/connect')) {
        return Promise.resolve(json({ authorization_url: 'https://accounts.google.com/x' }));
      }
      return Promise.resolve(json(createdGdrive, 201));
    });
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={() => {}} isAdmin />);

    await user.click(screen.getByRole('radio', { name: /^google drive/i }));
    await user.click(screen.getByRole('radio', { name: /^a folder/i }));
    await user.type(screen.getByLabelText(/^folder id/i), '  f-123  ');
    expect(screen.getByLabelText(/^folder id/i)).toHaveValue('  f-123  ');
    await user.type(screen.getByLabelText(/shared drive id/i), 'd-9');
    await user.click(screen.getByRole('button', { name: /continue to google/i }));

    await waitFor(() => expect(navigateToConsent).toHaveBeenCalled());
    const createCall = fetchSpy.mock.calls.find(
      ([u, i]) => String(u).endsWith('/sources') && i?.method === 'POST',
    );
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
      type: 'gdrive',
      config: { mode: 'folder', folder_id: 'f-123', drive_id: 'd-9' },
    });
  });

  it('blocks a folder submit without a folder id client-side (no request)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={() => {}} isAdmin />);

    await user.click(screen.getByRole('radio', { name: /^google drive/i }));
    await user.click(screen.getByRole('radio', { name: /^a folder/i }));
    await user.click(screen.getByRole('button', { name: /continue to google/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/folder id/i);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('blocks a shared-drive submit without a drive id client-side (no request)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={() => {}} isAdmin />);

    await user.click(screen.getByRole('radio', { name: /^google drive/i }));
    await user.click(screen.getByRole('radio', { name: /^a shared drive/i }));
    await user.click(screen.getByRole('button', { name: /continue to google/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/shared drive id/i);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('renders a create 403 INLINE (the admin gate checked at action time, INV-5)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(403, { title: 'Forbidden' }));
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={onClose} isAdmin />);

    await user.click(screen.getByRole('radio', { name: /^google drive/i }));
    await user.click(screen.getByRole('button', { name: /continue to google/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/tenant admin/i);
    expect(onClose).not.toHaveBeenCalled();
    expect(navigateToConsent).not.toHaveBeenCalled();
  });

  it('renders a connect failure INLINE and notes the source was created (retry from its card)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/connect')) {
        return Promise.resolve(problem(409, { title: 'Conflict', code: 'not_connectable' }));
      }
      return Promise.resolve(json(createdGdrive, 201));
    });
    const user = userEvent.setup();
    renderWithQuery(<AddSourceModal open onClose={() => {}} isAdmin />);

    await user.click(screen.getByRole('radio', { name: /^google drive/i }));
    await user.click(screen.getByRole('button', { name: /continue to google/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/can't start a consent/i);
    expect(alert).toHaveTextContent(/the source was created/i);
    expect(navigateToConsent).not.toHaveBeenCalled();
  });
});
