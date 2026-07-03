/**
 * Form-model tests for the assistant editor (#212) — the pure mappers between the
 * client draft and the wire bodies (AssistantCreate / AssistantUpdate), plus the
 * publish-gating rule (owner + distinct backup owner required, ADR-0011 §4).
 */
import { describe, it, expect } from 'vitest';
import type { Assistant } from '@/api';
import {
  canPublish,
  emptyForm,
  formFromAssistant,
  toCreateBody,
  toUpdateBody,
} from './form';

function makeAssistant(overrides: Partial<Assistant> = {}): Assistant {
  return {
    id: 'a1',
    name: 'Benefits helper',
    description: 'Answers HR questions',
    instructions: 'Be concise.',
    model: 'anthropic/claude',
    knowledgeScope: { collectionIds: ['c1'], sourceIds: [], modes: ['company'] },
    toolAllowlist: ['search_text'],
    autonomyLevel: 'suggest',
    owner: 'u1',
    backupOwner: 'u2',
    status: 'draft',
    certificationState: 'none',
    featured: false,
    version: null,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  };
}

describe('formFromAssistant / round-trip', () => {
  it('hydrates every field, coalescing null → empty string', () => {
    const form = formFromAssistant(makeAssistant({ description: null, model: null }));
    expect(form.name).toBe('Benefits helper');
    expect(form.description).toBe('');
    expect(form.model).toBe('');
    expect(form.owner).toBe('u1');
    expect(form.backupOwner).toBe('u2');
    expect(form.knowledgeScope.modes).toEqual(['company']);
  });
});

describe('toCreateBody', () => {
  it('sends only meaningfully-set fields; owner is server-assigned (omitted)', () => {
    const body = toCreateBody(emptyForm());
    expect(body).toEqual({ name: '', autonomyLevel: 'suggest' });
    expect('owner' in body).toBe(false);
  });

  it('includes description/instructions/model/scope/tools/backup when set', () => {
    const form = { ...emptyForm(), name: 'A', description: 'd', model: 'm', backupOwner: 'u2' };
    form.toolAllowlist = ['search_text'];
    form.knowledgeScope = { collectionIds: ['c1'], sourceIds: [], modes: [] };
    const body = toCreateBody(form);
    expect(body.description).toBe('d');
    expect(body.model).toBe('m');
    expect(body.toolAllowlist).toEqual(['search_text']);
    expect(body.knowledgeScope).toEqual({ collectionIds: ['c1'], sourceIds: [], modes: [] });
    expect(body.backupOwner).toBe('u2');
  });
});

describe('toUpdateBody', () => {
  it('maps empty strings back to null for nullable fields', () => {
    const body = toUpdateBody(emptyForm());
    expect(body.description).toBeNull();
    expect(body.instructions).toBeNull();
    expect(body.model).toBeNull();
    expect(body.backupOwner).toBeNull();
    expect(body.toolAllowlist).toEqual([]);
  });
});

describe('canPublish (ADR-0011 §4)', () => {
  it('is false without an owner', () => {
    expect(canPublish({ ...emptyForm(), name: 'A', backupOwner: 'u2' })).toBe(false);
  });
  it('is false without a backup owner', () => {
    expect(canPublish({ ...emptyForm(), name: 'A', owner: 'u1' })).toBe(false);
  });
  it('is false when owner and backup are the same person', () => {
    expect(canPublish({ ...emptyForm(), name: 'A', owner: 'u1', backupOwner: 'u1' })).toBe(false);
  });
  it('is true with a name, an owner, and a distinct backup owner', () => {
    expect(canPublish({ ...emptyForm(), name: 'A', owner: 'u1', backupOwner: 'u2' })).toBe(true);
  });
});
