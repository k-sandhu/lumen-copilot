/**
 * Login screen (AC-1) — the UNAUTHENTICATED front door.
 *
 * Polished to the wireframe (docs/wireframes/login.html, issue #116): a branded
 * brand cell (sparkle logo + "Lumen Copilot"), a centered elevated card on a
 * themed accent-glow backdrop, labelled icon-prefixed inputs, a clear PRIMARY
 * "Sign in" button, and inline error states. It is rendered by RouteGuard when
 * unauthenticated — NOT inside the app shell (no PageChrome). Built on the
 * production Tailwind tokens (styles/index.css) so it is correct in light + dark
 * without depending on the kit's [data-mode] being set; it consumes the @/ui
 * Icon for the sparkle glyph. Tokens are shared and are never edited here.
 *
 * Behavior is unchanged from the bare version: email + password → POST
 * /auth/login via useLogin; on success the token is stored and the route guard
 * renders the app shell.
 *
 * AC-4 (no account-existence disclosure): regardless of WHICH field is wrong,
 * the UI shows ONE generic message ("Incorrect email or password."). We never
 * surface the server's per-occurrence detail for auth failures, and never hint
 * whether the email exists. A 422 (malformed) and a transport error get their
 * own generic copy so the user is never stranded (quality bar: every state).
 */
import { useId, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError } from '@/api';
import { Icon } from '@/ui';
import { useLogin } from '../model/queries';

/** Map any login failure to a single, non-leaky message (AC-4). */
function genericError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Incorrect email or password.';
    if (error.status === 422) return 'Please enter a valid email and password.';
    if (error.status === 0) return 'Could not reach the server. Check your connection and retry.';
  }
  return 'Something went wrong signing in. Please try again.';
}

/** Shared input styling: token-backed, icon-prefixed, with a visible focus ring. */
const inputClass =
  'w-full rounded-md border border-border bg-surface py-2 pl-10 pr-3 text-sm text-foreground ' +
  'placeholder:text-foreground-muted/70 transition-colors ' +
  'focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/50 ' +
  'aria-[invalid=true]:border-danger';

export function LoginScreen() {
  const login = useLogin();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const emailId = useId();
  const passwordId = useId();
  const errorId = useId();

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    login.mutate({ email, password });
  }

  const errorMessage = login.isError ? genericError(login.error) : null;
  const invalid = errorMessage ? true : undefined;

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-surface-muted px-4 py-10 text-foreground">
      {/* Themed accent glow backdrop — derived from the --accent token, honors
          reduced-motion (no animation), and is purely decorative. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10"
        style={{
          background:
            'radial-gradient(60rem 60rem at 50% -10%, rgb(var(--accent) / 0.16), transparent 60%),' +
            'radial-gradient(40rem 40rem at 110% 110%, rgb(var(--accent) / 0.10), transparent 55%)',
        }}
      />

      <div className="w-full max-w-sm">
        {/* Brand cell */}
        <div className="mb-6 flex items-center gap-3">
          <span
            className="grid h-9 w-9 place-items-center rounded-lg text-white shadow-sm"
            style={{ background: 'linear-gradient(135deg, rgb(var(--accent)), rgb(var(--accent) / 0.78))' }}
          >
            <Icon name="sparkles" className="h-5 w-5" />
          </span>
          <span className="text-base font-bold tracking-tight">Lumen Copilot</span>
        </div>

        {/* Elevated card */}
        <div className="rounded-xl border border-border bg-surface p-6 shadow-lg sm:p-8">
          <h1 className="text-xl font-semibold tracking-tight">Sign in to your workspace</h1>
          <p className="mt-1.5 text-sm text-foreground-muted">
            Grounded, permission-checked answers over the sources you already trust.
          </p>

          <form className="mt-6 flex flex-col gap-4" onSubmit={onSubmit}>
            <div className="flex flex-col gap-1.5">
              <label htmlFor={emailId} className="text-sm font-medium text-foreground-muted">
                Email
              </label>
              <div className="relative">
                <Icon
                  name="user"
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted"
                />
                <input
                  id={emailId}
                  type="email"
                  name="email"
                  autoComplete="username"
                  placeholder="you@company.com"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  aria-invalid={invalid}
                  aria-describedby={errorMessage ? errorId : undefined}
                  className={inputClass}
                />
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <label htmlFor={passwordId} className="text-sm font-medium text-foreground-muted">
                Password
              </label>
              <div className="relative">
                <Icon
                  name="lock"
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted"
                />
                <input
                  id={passwordId}
                  type="password"
                  name="password"
                  autoComplete="current-password"
                  placeholder="••••••••"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  aria-invalid={invalid}
                  aria-describedby={errorMessage ? errorId : undefined}
                  className={inputClass}
                />
              </div>
            </div>

            {errorMessage && (
              <p
                id={errorId}
                role="alert"
                className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
              >
                <Icon name="alert-triangle" className="mt-0.5 h-4 w-4 flex-none" />
                <span>{errorMessage}</span>
              </p>
            )}

            <button
              type="submit"
              disabled={login.isPending}
              className="mt-1 inline-flex h-10 items-center justify-center gap-2 rounded-md bg-accent px-4 text-sm font-medium text-white shadow-sm transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-60"
            >
              {login.isPending ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        </div>

        {/* Trust footer — the front door's promise, mirroring the wireframe badges. */}
        <div className="mt-5 flex items-center justify-center gap-1.5 text-xs text-foreground-muted">
          <Icon name="shield-check" className="h-3.5 w-3.5" />
          <span>Permissioned, cited, and auditable by design.</span>
        </div>
      </div>
    </main>
  );
}
