import { beforeEach, describe, expect, it } from 'vitest';
import {
  clearActiveAuthSlotIfMatches,
  createAuthSlot,
  getActiveAuthSlot,
  isAuthSlot,
  setActiveAuthSlot,
} from './authSlot';

const SLOT_A = '11111111-1111-4111-8111-111111111111';
const SLOT_B = '22222222-2222-4222-8222-222222222222';

beforeEach(() => localStorage.clear());

describe('auth-slot routing metadata', () => {
  it('generates and admits only RFC-4122 version-4 UUID selectors', () => {
    expect(isAuthSlot(createAuthSlot())).toBe(true);
    expect(isAuthSlot(SLOT_A)).toBe(true);
    expect(isAuthSlot('11111111-1111-1111-8111-111111111111')).toBe(false);
    expect(isAuthSlot('not-a-uuid')).toBe(false);
    expect(isAuthSlot('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')).toBe(true);
    expect(isAuthSlot('AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA')).toBe(false);
    expect(isAuthSlot(SLOT_A.replaceAll('-', ''))).toBe(false);
    expect(isAuthSlot(`{${SLOT_A}}`)).toBe(false);
    expect(isAuthSlot(`urn:uuid:${SLOT_A}`)).toBe(false);
  });

  it('discards malformed persisted metadata instead of sending an arbitrary cookie name', () => {
    localStorage.setItem('lumen.active-auth-slot', '../lumen_refresh_token');
    expect(getActiveAuthSlot()).toBeNull();
    expect(localStorage.getItem('lumen.active-auth-slot')).toBeNull();
  });

  it('does not let an old tab/logout clear a newer tab selection', () => {
    setActiveAuthSlot(SLOT_A);
    setActiveAuthSlot(SLOT_B);
    clearActiveAuthSlotIfMatches(SLOT_A);
    expect(getActiveAuthSlot()).toBe(SLOT_B);

    clearActiveAuthSlotIfMatches(SLOT_B);
    expect(getActiveAuthSlot()).toBeNull();
  });
});
