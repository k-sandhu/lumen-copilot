import { useState } from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearAccessToken, logout, setAccessToken } from '@/api';
import { ProvidersPanel } from '@/features/admin/components/ProvidersPanel';
import { LoginScreen } from '@/features/auth/components/LoginScreen';
import { useAuthStore } from '@/features/auth/model/authStore';
import { RegisterServerModal } from '@/features/mcpServers/components/RegisterServerModal';
import { renderWithQuery } from '@/test/renderWithQuery';
import { storageSnapshot } from '@/test/storageSnapshot';

type Form = 'login' | 'provider' | 'mcp';
const SECRET = 'r8-typed-plaintext-credential';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function FormRoute({ kind }: { kind: Form }) {
  const status = useAuthStore((state) => state.status);
  const [open, setOpen] = useState(true);
  if ((kind === 'login') === (status === 'authenticated')) return null;
  if (kind === 'login') return <LoginScreen />;
  if (kind === 'provider') return <ProvidersPanel />;
  return (
    <>
      <button onClick={() => setOpen(true)}>Open registration</button>
      {open && <RegisterServerModal open onClose={() => setOpen(false)} />}
    </>
  );
}

// Traverse the retained subtree itself, rather than querying only connected DOM.
// Detached descendants are passed separately because a manager may remove its
// mirror before the application clears the form, and still hold that node.
function expectScrubbed(roots: Node[], secret = SECRET) {
  function visit(node: Node) {
    expect(node.nodeValue ?? '').not.toContain(secret);
    if (node instanceof Element) {
      for (const attribute of node.attributes) expect(attribute.value).not.toContain(secret);
    }
    if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
      expect(node.value).not.toContain(secret);
      expect(node.defaultValue).not.toContain(secret);
    }
    for (const child of node.childNodes) visit(child);
  }
  for (const root of roots) {
    visit(root);
    if (root instanceof Element) expect(root.outerHTML).not.toContain(secret);
    if (root instanceof HTMLFormElement) {
      expect(JSON.stringify([...new FormData(root).entries()])).not.toContain(secret);
    }
  }
}

function addCopies(root: HTMLFormElement, secret: string) {
  root.setAttribute('data-credential-copy', secret);
  root.setAttribute('aria-description', `Draft ${secret}`);
  const mirror = document.createElement('input');
  mirror.type = 'hidden';
  mirror.name = 'credential_mirror';
  mirror.value = secret;
  mirror.defaultValue = secret;
  mirror.setAttribute('data-credential-copy', secret);
  mirror.setAttribute('aria-label', `Copy ${secret}`);
  root.append(mirror);
  const detached = mirror.cloneNode(true) as HTMLInputElement;
  const text = document.createElement('span');
  text.hidden = true;
  text.textContent = secret;
  root.append(detached, text);
  detached.remove();
  text.remove();
  return [root, detached, text];
}

beforeEach(() => {
  clearAccessToken();
  useAuthStore.setState({ status: 'unauthenticated' });
});
afterEach(() => vi.restoreAllMocks());

describe('R7-001: typed secrets leave no live or retained DOM representation', () => {
  for (const kind of ['login', 'provider', 'mcp'] as const) {
    it.each(['success', 'failure', 'unmount', 'identity', 'logout'] as const)(
      `${kind}: scrub the complete retained subtree at %s`,
      async (boundary) => {
        const response = deferred<Response>();
        const started = deferred<RequestInit>();
        vi.spyOn(globalThis, 'fetch').mockImplementation((_input, init) => {
          if (init?.method === 'POST') {
            started.resolve(init);
            return response.promise;
          }
          return Promise.resolve(json({ items: [] }));
        });
        if (kind !== 'login') {
          setAccessToken('jwt-a');
          useAuthStore.setState({ status: 'authenticated' });
        }
        const user = userEvent.setup();
        const view = renderWithQuery(<FormRoute kind={kind} />);
        if (kind === 'provider') await screen.findByText(/no llm providers registered/i);
        if (kind === 'login') await user.type(screen.getByLabelText(/email/i), 'a@example.test');
        else {
          await user.type(screen.getByLabelText(/^name$/i), 'R8 fixture');
          await user.type(
            screen.getByLabelText(kind === 'provider' ? /base url/i : /endpoint url/i),
            'https://r8.example/v1',
          );
        }
        const secretInput = screen.getByLabelText(
          kind === 'login' ? /password/i : kind === 'provider' ? /api key/i : /^secret/i,
        ) as HTMLInputElement;
        await user.type(secretInput, SECRET);
        await user.click(screen.getByRole('button', { name: /show (password|api key|secret)/i }));
        const form = secretInput.closest('form')!;
        const retained = addCopies(form, SECRET);
        // Also exercise secret-bearing attributes on the primitive itself.
        secretInput.setAttribute('data-credential-copy', SECRET);
        secretInput.setAttribute('aria-description', `Revealed ${SECRET}`);
        if (boundary !== 'success' && boundary !== 'failure') {
          for (const input of form.querySelectorAll('input')) {
            // A manager can misclassify adjacent email/name/URL controls too.
            input.value = SECRET;
            input.defaultValue = SECRET;
            input.setAttribute('defaultValue', SECRET);
            input.setAttribute('data-credential-copy', SECRET);
            input.setAttribute('aria-description', `Manager copy ${SECRET}`);
          }
        }

        if (boundary === 'success' || boundary === 'failure') {
          await user.click(
            within(form).getByRole('button', { name: /^(sign in|add provider|register server)$/i }),
          );
          await started.promise;
          if (kind === 'provider') {
            // Submit intent clears immediately. A second manager write while
            // pending must also be scrubbed when either outcome settles.
            expectScrubbed(retained);
            secretInput.value = SECRET;
            secretInput.defaultValue = SECRET;
            retained.push(...addCopies(form, SECRET));
          }
          await act(async () =>
            response.resolve(
              boundary === 'failure'
                ? json({ title: 'Rejected', status: 422 }, 422)
                : kind === 'login'
                  ? json({ access_token: 'jwt-b', token_type: 'bearer', expires_in: 900 })
                  : json(
                      { id: 'r8', name: 'R8 fixture', status: 'pending', secret_hint: '****safe' },
                      201,
                    ),
            ),
          );
          await waitFor(() => expect(secretInput.value).toBe(''));
          if (kind === 'login' && boundary === 'success') expect(form.isConnected).toBe(false);
          if (kind === 'mcp' && boundary === 'success') expect(form.isConnected).toBe(false);
        } else if (boundary === 'unmount') view.unmount();
        else if (boundary === 'identity') act(() => setAccessToken('jwt-b'));
        else {
          let revocation!: Promise<void>;
          act(() => {
            revocation = logout();
          });
          const init = await started.promise;
          expect(new Headers(init.headers).get('Authorization')).toBe(
            kind === 'login' ? null : 'Bearer jwt-a',
          );
          // The response is still held, so cleanup cannot depend on the server.
          expectScrubbed(retained);
          if (kind !== 'login') expect(form.isConnected).toBe(false);
          await act(async () => {
            response.resolve(new Response(null, { status: 204 }));
            await revocation;
          });
        }

        expectScrubbed(retained);
        expect(secretInput.type).toBe('password');
        if (form.isConnected) {
          const reveal = within(form).getByRole('button', {
            name: /show (password|api key|secret)/i,
          });
          expect(reveal).toHaveAttribute('aria-pressed', 'false');
          // Force another render: uncleared React state must not rehydrate DOM.
          await user.click(reveal);
          expectScrubbed(retained);
        }
        expect(
          JSON.stringify(
            view.queryClient
              .getMutationCache()
              .getAll()
              .map((mutation) => mutation.state),
          ),
        ).not.toContain(SECRET);
        expect(storageSnapshot(localStorage)).not.toContain(SECRET);
        expect(storageSnapshot(sessionStorage)).not.toContain(SECRET);
        expect(location.href).not.toContain(SECRET);
      },
    );
  }

  it('MCP Cancel really unmounts the dialog and reopening cannot restore any copy', async () => {
    setAccessToken('jwt-a');
    useAuthStore.setState({ status: 'authenticated' });
    const user = userEvent.setup();
    renderWithQuery(<FormRoute kind="mcp" />);
    const input = screen.getByLabelText(/^secret/i) as HTMLInputElement;
    await user.type(input, SECRET);
    await user.click(screen.getByRole('button', { name: /show secret/i }));
    const retained = addCopies(input.closest('form')!, SECRET);
    await user.click(screen.getByRole('button', { name: /cancel/i }));
    expect(input.isConnected).toBe(false);
    expectScrubbed(retained);
    expect(input.type).toBe('password');
    await user.click(screen.getByRole('button', { name: /open registration/i }));
    expect(screen.getByLabelText(/^secret/i)).toHaveValue('');
    expect(screen.getByRole('button', { name: /show secret/i })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('scrubs the key even when a larger adjacent value contains the same key', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    setAccessToken('jwt-a');
    useAuthStore.setState({ status: 'authenticated' });
    const user = userEvent.setup();
    renderWithQuery(<FormRoute kind="provider" />);
    await screen.findByText(/no llm providers registered/i);
    const input = screen.getByLabelText(/api key/i) as HTMLInputElement;
    await user.type(input, SECRET);
    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: `prefix-${SECRET}` } });
    fireEvent.change(screen.getByLabelText(/base url/i), {
      target: { value: `https://r8.example/${SECRET}` },
    });
    const retained = addCopies(input.closest('form')!, SECRET);
    act(() => setAccessToken('jwt-b'));
    expectScrubbed(retained);
  });

  it('scrubs earlier attribute and hidden text copies after the draft is edited and erased', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    setAccessToken('jwt-a');
    useAuthStore.setState({ status: 'authenticated' });
    const user = userEvent.setup();
    renderWithQuery(<FormRoute kind="provider" />);
    await screen.findByText(/no llm providers registered/i);
    const input = screen.getByLabelText(/api key/i) as HTMLInputElement;
    await user.type(input, SECRET);
    const form = input.closest('form')!;
    form.setAttribute('data-credential-copy', SECRET);
    form.setAttribute('aria-description', `Earlier ${SECRET}`);
    const copy = document.createElement('span');
    copy.hidden = true;
    copy.textContent = SECRET;
    form.append(copy);
    copy.remove();
    await user.type(input, '-edited');
    await user.clear(input);
    act(() => setAccessToken('jwt-b'));
    expectScrubbed([form, copy]);
  });

  it.each(['login', 'mcp'] as const)(
    '%s: a failed submit clears React-managed adjacent copies too',
    async (kind) => {
      const response = deferred<Response>();
      const started = deferred<void>();
      vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
        started.resolve();
        return response.promise;
      });
      if (kind === 'mcp') {
        setAccessToken('jwt-a');
        useAuthStore.setState({ status: 'authenticated' });
      }
      const user = userEvent.setup();
      renderWithQuery(<FormRoute kind={kind} />);
      if (kind === 'login') await user.type(screen.getByLabelText(/email/i), 'a@example.test');
      else {
        await user.type(screen.getByLabelText(/^name$/i), 'R8 fixture');
        await user.type(screen.getByLabelText(/endpoint url/i), 'https://r8.example/v1');
      }
      const input = screen.getByLabelText(
        kind === 'login' ? /password/i : /^secret/i,
      ) as HTMLInputElement;
      await user.type(input, SECRET);
      const form = input.closest('form')!;
      await user.click(within(form).getByRole('button', { name: /^(sign in|register server)$/i }));
      await started.promise;
      const adjacent = screen.getByLabelText(kind === 'login' ? /email/i : /^name$/i);
      // Model a manager that DOES dispatch the input event, so clearing only DOM
      // would let the owner's metadata state restore the copy on the next render.
      fireEvent.change(adjacent, { target: { value: SECRET } });
      await act(async () => response.resolve(json({ title: 'Rejected', status: 422 }, 422)));
      await screen.findByRole('alert');
      expectScrubbed([form]);
    },
  );
});
