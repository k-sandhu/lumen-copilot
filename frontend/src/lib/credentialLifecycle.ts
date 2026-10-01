import { useEffect, useRef } from 'react';

type CredentialClearer = () => void;

/**
 * Credential drafts are deliberately component-local, but authentication can
 * change while a form remains mounted (for example, logout in another shell
 * control). This tiny registry broadcasts that boundary transition without
 * retaining any credential value itself.
 */
const credentialClearers = new Set<CredentialClearer>();

export function clearCredentialDrafts(): void {
  for (const clear of [...credentialClearers]) clear();
}

/** Register a form-local wipe function for logout / principal transitions. */
export function useCredentialClearer(clear: CredentialClearer): void {
  const clearRef = useRef(clear);
  clearRef.current = clear;

  useEffect(() => {
    const registered = () => clearRef.current();
    credentialClearers.add(registered);
    return () => {
      credentialClearers.delete(registered);
    };
  }, []);
}
