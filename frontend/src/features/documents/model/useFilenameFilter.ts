import { useEffect, useState } from 'react';
import type { DocumentStatus } from '@/api';

/** Filename typing settles after 250ms; explicit scope changes apply immediately. */
export const FILENAME_DEBOUNCE_MS = 250;

export function useFilenameFilter(
  draft: string,
  collectionId: string,
  status: DocumentStatus | 'all',
): string | undefined {
  const normalized = draft.trim();
  const [settled, setSettled] = useState({ value: normalized, collectionId, status });
  const scopeChanged = settled.collectionId !== collectionId || settled.status !== status;

  useEffect(() => {
    // Clear and explicit status/collection changes supersede pending typing. The
    // return value below applies them in this render, before effects run, so a
    // new collection never requests the previous scope's settled filename.
    if (scopeChanged || (!normalized && settled.value)) {
      setSettled({ value: normalized, collectionId, status });
      return;
    }
    if (normalized === settled.value) return;
    const timer = setTimeout(() => {
      setSettled({ value: normalized, collectionId, status });
    }, FILENAME_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [normalized, collectionId, status, scopeChanged, settled.value]);

  return (scopeChanged || !normalized ? normalized : settled.value) || undefined;
}
