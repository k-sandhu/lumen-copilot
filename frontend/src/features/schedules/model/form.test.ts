/**
 * Unit tests for the schedule form model (#237). Covers the draft↔wire mappers,
 * param-row conversion, and validation (AC-4 friendly cron/timezone gating).
 */
import { describe, it, expect } from 'vitest';
import type { Schedule } from '@/api';
import {
  emptyForm,
  formFromSchedule,
  isValid,
  paramsToRows,
  rowsToParams,
  toCreateBody,
  toUpdateBody,
  validateForm,
} from './form';

const SCHEDULE: Schedule = {
  id: 's1',
  assistant_id: 'a1',
  owner_id: 'u1',
  cadence: { structured: { every: 'day', at: '08:00' } },
  timezone: 'America/New_York',
  input_params: { topic: 'weekly digest', count: 5 },
  delivery: { inbox: true, digest: 'daily' },
  overlap_policy: 'queue',
  enabled: false,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
};

describe('param rows', () => {
  it('maps a wire input_params object to rows and back', () => {
    const rows = paramsToRows({ topic: 'x', count: 5 });
    expect(rows).toContainEqual({ key: 'topic', value: 'x' });
    expect(rows).toContainEqual({ key: 'count', value: '5' });
    expect(rowsToParams(rows)).toEqual({ topic: 'x', count: '5' });
  });
  it('drops empty keys and returns undefined for no rows', () => {
    expect(rowsToParams([{ key: '', value: 'v' }])).toBeUndefined();
    expect(rowsToParams([])).toBeUndefined();
  });
});

describe('formFromSchedule', () => {
  it('hydrates every editable field from a schedule', () => {
    const form = formFromSchedule(SCHEDULE);
    expect(form.assistantId).toBe('a1');
    expect(form.timezone).toBe('America/New_York');
    expect(form.cadence.mode).toBe('structured');
    expect(form.deliverInbox).toBe(true);
    expect(form.digest).toBe('daily');
    expect(form.overlapPolicy).toBe('queue');
    expect(form.enabled).toBe(false);
    expect(form.params).toContainEqual({ key: 'topic', value: 'weekly digest' });
  });
});

describe('validateForm', () => {
  it('requires an assistant', () => {
    const errors = validateForm({ ...emptyForm(), assistantId: '' });
    expect(errors.assistantId).toBeTruthy();
    expect(isValid(errors)).toBe(false);
  });
  it('flags a bad cron and a bad timezone with friendly messages', () => {
    const form = { ...emptyForm('a1'), timezone: 'Nowhere/Here' };
    form.cadence = { ...form.cadence, mode: 'cron', cron: 'bad' };
    const errors = validateForm(form);
    expect(errors.cadence).toBeTruthy();
    expect(errors.timezone).toBeTruthy();
    expect(isValid(errors)).toBe(false);
  });
  it('passes a well-formed form', () => {
    const errors = validateForm(emptyForm('a1'));
    expect(isValid(errors)).toBe(true);
  });
});

describe('toCreateBody', () => {
  it('builds a create body with delivery, overlap, enabled, and params', () => {
    const form = {
      ...emptyForm('a1'),
      params: [{ key: 'topic', value: 'x' }],
      digest: 'weekly' as const,
    };
    const body = toCreateBody(form);
    expect(body.assistant_id).toBe('a1');
    expect(body.timezone).toBe(form.timezone);
    expect(body.delivery).toEqual({ inbox: true, digest: 'weekly' });
    expect(body.overlap_policy).toBe('skip');
    expect(body.enabled).toBe(true);
    expect(body.input_params).toEqual({ topic: 'x' });
  });
  it('omits input_params when there are none', () => {
    expect(toCreateBody(emptyForm('a1')).input_params).toBeUndefined();
  });
});

describe('toUpdateBody', () => {
  it('sends the full editable head, with an empty params object when cleared', () => {
    const body = toUpdateBody(emptyForm('a1'));
    expect(body.input_params).toEqual({});
    expect(body.cadence).toBeDefined();
    expect(body.timezone).toBeDefined();
    expect(body.delivery).toBeDefined();
  });
});
