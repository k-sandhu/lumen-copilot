/**
 * Opaque browser auth-slot selection for the HttpOnly refresh cookie.
 *
 * A slot is deliberately NOT a credential: possession of it grants nothing
 * without the independently random HttpOnly refresh token. Giving each login
 * intent a distinct cookie name means an old response can mutate only its own
 * cookie; it cannot overwrite a newer principal's refresh cookie when network
 * responses arrive out of order. The selected slot is persisted so a reload can
 * find the right HttpOnly cookie without exposing that token to JavaScript.
 */

export const AUTH_SLOT_HEADER = 'X-Lumen-Auth-Slot';
export const PREVIOUS_AUTH_SLOT_HEADER = 'X-Lumen-Previous-Auth-Slot';
const ACTIVE_AUTH_SLOT_STORAGE_KEY = 'lumen.active-auth-slot';
const AUTH_REFRESH_REVISION_STORAGE_KEY = 'lumen.auth-refresh-revision';
const AUTH_SLOT_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
type AuthSlotListener = (slot: string | null, previousSlot: string | null) => void;
type RefreshRevisionListener = (slot: string | null, revision: string) => void;
const listeners = new Set<AuthSlotListener>();
const refreshRevisionListeners = new Set<RefreshRevisionListener>();

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    const activeStorage = storage();
    if (!activeStorage || event.storageArea !== activeStorage) return;
    if (event.key === ACTIVE_AUTH_SLOT_STORAGE_KEY) {
      const selected = isAuthSlot(event.newValue) ? event.newValue : null;
      const previous = isAuthSlot(event.oldValue) ? event.oldValue : null;
      for (const listener of listeners) listener(selected, previous);
      return;
    }
    if (event.key === AUTH_REFRESH_REVISION_STORAGE_KEY) {
      const notice = parseRefreshRevision(event.newValue);
      if (notice) {
        for (const listener of refreshRevisionListeners) {
          listener(notice.slot, notice.revision);
        }
      }
    }
  });
}

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function isAuthSlot(value: string | null | undefined): value is string {
  return typeof value === 'string' && AUTH_SLOT_PATTERN.test(value);
}

/** Generate an unpredictable RFC-4122 v4 slot identifier. */
export function createAuthSlot(): string {
  if (typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }

  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(
    16,
    20,
  )}-${hex.slice(20)}`;
}

/** The slot selected by the last successful session establishment. */
export function getActiveAuthSlot(): string | null {
  try {
    const activeStorage = storage();
    const value = activeStorage?.getItem(ACTIVE_AUTH_SLOT_STORAGE_KEY) ?? null;
    if (isAuthSlot(value)) return value;
    if (value !== null) activeStorage?.removeItem(ACTIVE_AUTH_SLOT_STORAGE_KEY);
  } catch {
    // Storage is optional; a blocked store only removes reload persistence.
  }
  return null;
}

export function setActiveAuthSlot(slot: string): void {
  if (!isAuthSlot(slot)) throw new Error('Invalid auth slot');
  try {
    storage()?.setItem(ACTIVE_AUTH_SLOT_STORAGE_KEY, slot);
  } catch {
    // Keep the live in-memory session usable when persistence is unavailable.
  }
}

/** Do not let an old tab/logout clear a newer tab's selected slot. */
export function clearActiveAuthSlotIfMatches(slot: string | null): void {
  try {
    const activeStorage = storage();
    const current = activeStorage?.getItem(ACTIVE_AUTH_SLOT_STORAGE_KEY) ?? null;
    if (slot === null || current === slot) {
      activeStorage?.removeItem(ACTIVE_AUTH_SLOT_STORAGE_KEY);
    }
  } catch {
    // Best effort only; the HttpOnly token remains the actual credential.
  }
}

export function authSlotHeaders(slot: string | null): HeadersInit | undefined {
  return slot ? { [AUTH_SLOT_HEADER]: slot } : undefined;
}

export function authLoginSlotHeaders(slot: string, previousSlot: string | null): HeadersInit {
  const headers: Record<string, string> = { [AUTH_SLOT_HEADER]: slot };
  if (previousSlot) headers[PREVIOUS_AUTH_SLOT_HEADER] = previousSlot;
  return headers;
}

/** Observe another tab selecting/clearing this browser's active session. */
export function subscribeActiveAuthSlot(listener: AuthSlotListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

interface RefreshRevision {
  slot: string | null;
  revision: string;
}

function parseRefreshRevision(value: string | null): RefreshRevision | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Record<string, unknown>;
    let slot: string | null;
    if (parsed.slot === null) slot = null;
    else if (typeof parsed.slot === 'string' && isAuthSlot(parsed.slot)) slot = parsed.slot;
    else return null;
    const revision = typeof parsed.revision === 'string' ? parsed.revision : null;
    if (revision) return { slot, revision };
  } catch {
    // Corrupt coordination metadata is ignored; it grants no authority.
  }
  return null;
}

/** Current non-secret completion marker for one shared-cookie rotation lane. */
export function getAuthRefreshRevision(slot: string | null): string | null {
  try {
    const notice = parseRefreshRevision(
      storage()?.getItem(AUTH_REFRESH_REVISION_STORAGE_KEY) ?? null,
    );
    return notice?.slot === slot ? notice.revision : null;
  } catch {
    return null;
  }
}

/** Notify other documents only after a generation-checked refresh commits. */
export function publishAuthRefresh(slot: string | null): void {
  const revision = createAuthSlot();
  try {
    storage()?.setItem(
      AUTH_REFRESH_REVISION_STORAGE_KEY,
      JSON.stringify({ slot, revision } satisfies RefreshRevision),
    );
  } catch {
    // Optional coordination only; the server remains replay-safe without it.
  }
}

/** Wait for a winning tab, or briefly yield before a safe one-time retry. */
export function waitForAuthRefresh(
  slot: string | null,
  previousRevision: string | null,
  timeoutMs = 300,
): Promise<void> {
  if (getAuthRefreshRevision(slot) !== previousRevision) return Promise.resolve();
  return new Promise((resolve) => {
    const listener: RefreshRevisionListener = (noticeSlot, revision) => {
      if (noticeSlot !== slot || revision === previousRevision) return;
      finish();
    };
    const finish = () => {
      clearTimeout(timer);
      refreshRevisionListeners.delete(listener);
      resolve();
    };
    refreshRevisionListeners.add(listener);
    const timer = setTimeout(finish, timeoutMs);
  });
}

/**
 * Cross-document serialization when the browser supports Web Locks. The server
 * protocol is independently correct when this API is absent or a tab crashes;
 * a lock is only a latency/thundering-herd optimization.
 */
export function withAuthRefreshLock<T>(slot: string | null, run: () => Promise<T>): Promise<T> {
  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks;
  if (!locks) return run();
  return locks.request(`lumen-auth-refresh:${slot ?? 'legacy'}`, { mode: 'exclusive' }, run);
}
