/**
 * Pure display-helper tests for the assistant slice (#212) — status tones, model
 * and owner labels, and the graceful id fallback when no human label is available.
 */
import { describe, it, expect } from 'vitest';
import type { ChatModelInfo, Member } from '@/api';
import { autonomyLabel, modelLabel, ownerLabel, shortId, statusTone } from './presentation';

const models: ChatModelInfo[] = [
  {
    id: 'anthropic/claude',
    label: 'Claude',
    provider: 'anthropic',
    tier: 'frontier',
    is_default: true,
  },
];
const members: Member[] = [
  { id: 'u-123456789', email: 'ada@acme.com', role: ['member'], email_attested_at: null },
];

describe('statusTone', () => {
  it('maps each lifecycle status to a tone', () => {
    expect(statusTone('published')).toBe('ok');
    expect(statusTone('draft')).toBe('pending');
    expect(statusTone('disabled')).toBe('danger');
  });
});

describe('modelLabel', () => {
  it('resolves a known id to its label', () => {
    expect(modelLabel('anthropic/claude', models)).toBe('Claude');
  });
  it('shows "Smart default" when the model is null', () => {
    expect(modelLabel(null, models)).toBe('Smart default');
  });
  it('shows an unknown id verbatim', () => {
    expect(modelLabel('mystery/model', models)).toBe('mystery/model');
  });
});

describe('ownerLabel', () => {
  it('resolves a known owner to their email', () => {
    expect(ownerLabel('u-123456789', members)).toBe('ada@acme.com');
  });
  it('falls back to a short id when the roster is unavailable', () => {
    expect(ownerLabel('u-123456789', undefined)).toBe(shortId('u-123456789'));
  });
});

describe('autonomyLabel', () => {
  it('labels each autonomy level', () => {
    expect(autonomyLabel('suggest')).toBe('Suggest');
    expect(autonomyLabel('act_with_approval')).toBe('Act with approval');
  });
});
