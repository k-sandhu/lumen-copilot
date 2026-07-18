/**
 * Unit tests for the cadence + timezone model (#237, AC-4). Covers the friendly
 * validation messages, the draft↔wire round-trip, and the human summary — the pure
 * core the schedule form relies on.
 */
import { describe, it, expect } from 'vitest';
import {
  cadenceToDraft,
  describeCadence,
  draftToCadence,
  emptyCadence,
  validateCron,
  validateTimezone,
  validateTimeOfDay,
  validateCadence,
} from './cadence';

describe('validateCron', () => {
  it('accepts a well-formed 5-field expression', () => {
    expect(validateCron('0 8 * * 1')).toBeNull();
    expect(validateCron('*/15 * * * *')).toBeNull();
    expect(validateCron('0 9-17 * * 1-5')).toBeNull();
  });

  it('rejects an empty expression with a friendly message', () => {
    expect(validateCron('   ')).toMatch(/enter a cron/i);
  });

  it('rejects the wrong field count with the count in the message', () => {
    expect(validateCron('0 8 * *')).toMatch(/5 fields/i);
    expect(validateCron('0 8 * * 1 2')).toMatch(/5 fields/i);
  });

  it('rejects an illegal character in a field, naming the field', () => {
    expect(validateCron('0 8 * * mon')).toMatch(/day-of-week/i);
  });
});

describe('validateTimeOfDay', () => {
  it('accepts HH:MM', () => {
    expect(validateTimeOfDay('08:00')).toBeNull();
    expect(validateTimeOfDay('23:59')).toBeNull();
  });
  it('rejects a malformed time with a friendly message', () => {
    expect(validateTimeOfDay('8am')).toMatch(/HH:MM/);
    expect(validateTimeOfDay('25:00')).toMatch(/HH:MM/);
  });
});

describe('validateTimezone', () => {
  it('accepts a known IANA zone', () => {
    expect(validateTimezone('America/New_York')).toBeNull();
    expect(validateTimezone('UTC')).toBeNull();
  });
  it('rejects an unknown zone with a friendly message', () => {
    expect(validateTimezone('Mars/Phobos')).toMatch(/isn.t a known IANA timezone/i);
  });
  it('rejects an empty zone', () => {
    expect(validateTimezone('')).toMatch(/pick a timezone/i);
  });
});

describe('draft ↔ wire round-trip', () => {
  it('maps a structured weekly cadence to the wire and back', () => {
    const draft = {
      ...emptyCadence(),
      mode: 'structured' as const,
      every: 'week' as const,
      at: '09:30',
      dayOfWeek: 3,
    };
    const wire = draftToCadence(draft);
    expect(wire).toEqual({ structured: { every: 'week', at: '09:30', day_of_week: 3 } });
    const back = cadenceToDraft(wire);
    expect(back.mode).toBe('structured');
    expect(back.every).toBe('week');
    expect(back.dayOfWeek).toBe(3);
    expect(back.at).toBe('09:30');
  });

  it('maps a cron cadence to the wire and back', () => {
    const draft = { ...emptyCadence(), mode: 'cron' as const, cron: '0 8 * * 1' };
    const wire = draftToCadence(draft);
    expect(wire).toEqual({ cron: '0 8 * * 1' });
    expect(cadenceToDraft(wire)).toMatchObject({ mode: 'cron', cron: '0 8 * * 1' });
  });

  it('omits day_of_week/day_of_month for a daily cadence', () => {
    const draft = {
      ...emptyCadence(),
      mode: 'structured' as const,
      every: 'day' as const,
      at: '06:00',
    };
    expect(draftToCadence(draft)).toEqual({ structured: { every: 'day', at: '06:00' } });
  });

  it('throws on an invalid cron when converting', () => {
    const draft = { ...emptyCadence(), mode: 'cron' as const, cron: 'nope' };
    expect(() => draftToCadence(draft)).toThrow();
  });
});

describe('validateCadence', () => {
  it('validates the active mode only', () => {
    expect(validateCadence({ ...emptyCadence(), mode: 'cron', cron: 'bad' })).not.toBeNull();
    expect(validateCadence({ ...emptyCadence(), mode: 'structured', at: '08:00' })).toBeNull();
  });
});

describe('describeCadence', () => {
  it('describes each structured period', () => {
    expect(describeCadence({ structured: { every: 'day', at: '08:00' } })).toBe(
      'Every day at 08:00',
    );
    expect(describeCadence({ structured: { every: 'week', at: '08:00', day_of_week: 1 } })).toMatch(
      /Monday/,
    );
    expect(
      describeCadence({ structured: { every: 'month', at: '08:00', day_of_month: 15 } }),
    ).toMatch(/day 15/);
  });
  it('describes a cron cadence verbatim', () => {
    expect(describeCadence({ cron: '0 8 * * 1' })).toBe('cron: 0 8 * * 1');
  });
});
