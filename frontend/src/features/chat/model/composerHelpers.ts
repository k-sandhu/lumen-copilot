/**
 * Pure helpers for the spec 0006/0007 composer + context surfaces (#429/#432).
 * Kept out of component files so react-refresh sees component-only modules.
 */

/** A document pinned into the next answer's retrieval scope (spec 0007 #432). */
export interface PinnedDocument {
  id: string;
  name: string;
}

/** The active @-mention token at the END of the draft, or null (spec 0007). */
export function mentionQueryOf(draft: string): string | null {
  const match = /(?:^|\s)@([^\s@]*)$/.exec(draft);
  return match ? (match[1] ?? '') : null;
}

/** 12_345 → "12.3k"; 999 → "999" (the context meter's unit, spec 0007 AC-1). */
export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}
