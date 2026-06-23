/**
 * Deterministic DOM ids wiring an admin tab to its tabpanel (#122). Kept in its
 * own module (no component export) so the tab component stays fast-refreshable and
 * callers can derive `aria-controls` / `aria-labelledby` without re-deriving the
 * convention.
 */
export function adminTabIds(idPrefix: string, tabId: string) {
  return { tab: `${idPrefix}-tab-${tabId}`, panel: `${idPrefix}-panel-${tabId}` };
}
