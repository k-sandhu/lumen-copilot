/**
 * Account preferences slice (spec 0005, epic #144) — the caller's per-user
 * settings (currently the default chat model). Server state via TanStack Query;
 * consumed by the chat model picker (and a future settings surface).
 */
export { usePreferences, useUpdatePreferences, preferencesKey } from './model/queries';
