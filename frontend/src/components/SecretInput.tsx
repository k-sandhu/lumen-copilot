import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import type { InputHTMLAttributes } from 'react';
import { Icon } from '@/ui';
import { cn } from '@/lib/cn';
import { useCredentialClearer } from '@/lib/credentialLifecycle';

export interface SecretInputProps
  extends Omit<
    InputHTMLAttributes<HTMLInputElement>,
    | 'autoCapitalize'
    | 'autoComplete'
    | 'defaultValue'
    | 'name'
    | 'onChange'
    | 'spellCheck'
    | 'type'
    | 'value'
  > {
  /** Standards-correct browser meaning: real login vs a newly supplied secret. */
  purpose: 'current-password' | 'new-secret';
  /** Deliberately explicit, non-login names are required for provider secrets. */
  name: string;
  value: string;
  onValueChange: (value: string) => void;
  /** Human label used by the accessible Show/Hide control. */
  revealLabel: string;
  wrapperClassName?: string;
  revealClassName?: string;
}

export interface SecretInputHandle {
  /** Synchronously blank the live node and restore its masked presentation. */
  reset: () => void;
}

/**
 * Shared credential input with exact autocomplete semantics and an explicit,
 * keyboard-accessible reveal action. It never receives a stored server value.
 */
export const SecretInput = forwardRef<SecretInputHandle, SecretInputProps>(function SecretInput(
  {
    purpose,
    name,
    value,
    onValueChange,
    revealLabel,
    wrapperClassName,
    revealClassName,
    className,
    disabled,
    ...inputProps
  },
  forwardedRef,
) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const mounted = useRef(true);
  const [revealed, setRevealed] = useState(false);
  // React assigns null to ordinary refs before passive unmount cleanup. Keep the
  // last node long enough to blank a detached control, then let the component
  // (and this ref) be collected.
  const rememberInput = useCallback((node: HTMLInputElement | null) => {
    if (node) inputRef.current = node;
  }, []);

  const hardReset = useCallback(() => {
    if (inputRef.current) {
      inputRef.current.value = '';
      inputRef.current.type = 'password';
    }
    if (!mounted.current) return;
    onValueChange('');
    setRevealed(false);
  }, [onValueChange]);

  useImperativeHandle(forwardedRef, () => ({ reset: hardReset }), [hardReset]);
  useCredentialClearer(hardReset);

  // Clear and remask the detached DOM node too. This matters for browser
  // extensions that keep references to removed controls beyond React's
  // component lifetime. The cleanup never schedules a state update.
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (inputRef.current) {
        inputRef.current.value = '';
        inputRef.current.type = 'password';
      }
    };
  }, []);

  const action = revealed ? 'Hide' : 'Show';

  return (
    <div className={cn('relative', wrapperClassName)}>
      <input
        {...inputProps}
        ref={rememberInput}
        type={revealed ? 'text' : 'password'}
        name={name}
        autoComplete={purpose === 'current-password' ? 'current-password' : 'new-password'}
        autoCapitalize="none"
        spellCheck={false}
        value={value}
        disabled={disabled}
        onChange={(event) => onValueChange(event.target.value)}
        className={className}
      />
      <button
        type="button"
        aria-pressed={revealed}
        disabled={disabled}
        onClick={() => setRevealed((current) => !current)}
        className={cn(
          'absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-foreground-muted',
          'hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          'disabled:opacity-50',
          revealClassName,
        )}
      >
        <Icon name="eye" className="h-4 w-4" aria-hidden="true" />
        <span className="sr-only">
          {action} {revealLabel}
        </span>
      </button>
    </div>
  );
});
