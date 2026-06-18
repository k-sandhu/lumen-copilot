/**
 * Login screen (AC-1). Email + password → POST /auth/login via useLogin; on
 * success the token is stored and the route guard renders the app shell.
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
import { Card, CardBody, CardHeader, CardTitle } from '@/components/Card';
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

  return (
    <main className="flex min-h-screen items-center justify-center bg-surface px-4 text-foreground">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Sign in to Lumen Copilot</CardTitle>
        </CardHeader>
        <CardBody>
          <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate={false}>
            <div className="flex flex-col gap-1.5">
              <label htmlFor={emailId} className="text-sm font-medium">
                Email
              </label>
              <input
                id={emailId}
                type="email"
                name="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                aria-invalid={errorMessage ? true : undefined}
                aria-describedby={errorMessage ? errorId : undefined}
                className="rounded-md border border-border bg-surface px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <label htmlFor={passwordId} className="text-sm font-medium">
                Password
              </label>
              <input
                id={passwordId}
                type="password"
                name="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-invalid={errorMessage ? true : undefined}
                aria-describedby={errorMessage ? errorId : undefined}
                className="rounded-md border border-border bg-surface px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
              />
            </div>

            {errorMessage && (
              <p
                id={errorId}
                role="alert"
                className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
              >
                {errorMessage}
              </p>
            )}

            <button
              type="submit"
              disabled={login.isPending}
              className="mt-1 rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {login.isPending ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        </CardBody>
      </Card>
    </main>
  );
}
