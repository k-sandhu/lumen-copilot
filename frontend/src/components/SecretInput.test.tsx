import { useRef, useState } from 'react';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { clearCredentialDrafts } from '@/lib/credentialLifecycle';
import { SecretInput, type SecretInputHandle } from './SecretInput';

function Draft() {
  const [value, setValue] = useState('');
  const handle = useRef<SecretInputHandle>(null);
  return (
    <>
      <SecretInput
        ref={handle}
        aria-label="Draft key"
        purpose="new-secret"
        name="draft_key"
        value={value}
        onValueChange={setValue}
        revealLabel="draft key"
      />
      <button onClick={() => handle.current?.reset()}>Clear draft</button>
    </>
  );
}

describe('SecretInput retained native representations (R7-001)', () => {
  it('preserves authored field semantics when a secret happens to match an attribute substring', async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, 'writeText');
    render(<Draft />);
    const input = screen.getByLabelText('Draft key');
    await user.type(input, 'password');
    await user.click(screen.getByRole('button', { name: /show draft key/i }));
    await user.click(screen.getByRole('button', { name: /clear draft/i }));
    expect(input).toHaveAttribute('name', 'draft_key');
    expect(input).toHaveAttribute('autocomplete', 'new-password');
    expect(input).toHaveAttribute('type', 'password');
    expect(writeText).not.toHaveBeenCalled();
  });
  it.each(['reset', 'principal', 'unmount'] as const)(
    'scrubs typed defaults and serialization at %s',
    async (boundary) => {
      const user = userEvent.setup();
      const view = render(<Draft />);
      const input = screen.getByLabelText('Draft key') as HTMLInputElement;
      const secret = 'r8-standalone-typed-secret';
      await user.type(input, secret);
      await user.click(screen.getByRole('button', { name: /show draft key/i }));
      expect(input.defaultValue).toBe(secret);
      expect(input.getAttribute('value')).toBe(secret);
      if (boundary === 'reset')
        await user.click(screen.getByRole('button', { name: /clear draft/i }));
      else if (boundary === 'principal') act(() => clearCredentialDrafts());
      else view.unmount();
      expect(input.value).toBe('');
      expect(input.defaultValue).toBe('');
      expect(input.getAttribute('value') ?? '').toBe('');
      expect(input.outerHTML).not.toContain(secret);
      expect(input.type).toBe('password');
      if (boundary !== 'unmount') {
        expect(screen.getByRole('button', { name: /show draft key/i })).toHaveAttribute(
          'aria-pressed',
          'false',
        );
        await user.click(screen.getByRole('button', { name: /show draft key/i }));
        expect(input.value).toBe('');
      }
    },
  );
});
