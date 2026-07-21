/**
 * Pure-helper tests for the version-history panel (#214) — the per-version config
 * summary and the field-level diff vs. the current head. Order-insensitivity for
 * tools/scope is asserted so a reorder is NOT a spurious change.
 */
import { describe, it, expect } from 'vitest';
import type { AssistantVersionConfig, ChatModelInfo } from '@/api';
import { configDiff, configSummary, scopeSummary } from './versionSummary';

const models: ChatModelInfo[] = [
  {
    id: 'anthropic/claude',
    label: 'Claude',
    provider: 'anthropic',
    tier: 'frontier',
    is_default: true,
  },
];

function config(overrides: Partial<AssistantVersionConfig> = {}): AssistantVersionConfig {
  return {
    name: 'Benefits helper',
    description: 'Answers HR questions',
    instructions: 'Be concise.',
    model: 'anthropic/claude',
    knowledgeScope: { collectionIds: [], sourceIds: [], modes: ['company'] },
    toolAllowlist: ['search_text'],
    autonomyLevel: 'suggest',
    ...overrides,
  };
}

describe('configSummary', () => {
  it('produces a labelled row per config field in display order', () => {
    const rows = configSummary(config(), models);
    expect(rows.map((r) => r.key)).toEqual([
      'name',
      'description',
      'instructions',
      'model',
      'autonomyLevel',
      'toolAllowlist',
      'knowledgeScope',
    ]);
  });

  it('resolves the model id to its label and autonomy to a label', () => {
    const rows = configSummary(config(), models);
    expect(rows.find((r) => r.key === 'model')?.value).toBe('Claude');
    expect(rows.find((r) => r.key === 'autonomyLevel')?.value).toBe('Suggest');
  });

  it('shows "Smart default" for a null model and a placeholder for empty fields', () => {
    const rows = configSummary(
      config({ model: null, description: null, instructions: null }),
      models,
    );
    expect(rows.find((r) => r.key === 'model')?.value).toBe('Smart default');
    expect(rows.find((r) => r.key === 'description')?.value).toBe('—');
    expect(rows.find((r) => r.key === 'instructions')?.value).toBe('—');
  });

  it('summarises an empty tool allowlist and empty scope without a blank', () => {
    const rows = configSummary(
      config({
        toolAllowlist: [],
        knowledgeScope: { collectionIds: [], sourceIds: [], modes: [] },
      }),
      models,
    );
    expect(rows.find((r) => r.key === 'toolAllowlist')?.value).toBe('No tools');
    expect(rows.find((r) => r.key === 'knowledgeScope')?.value).toBe('No scope set');
  });
});

describe('scopeSummary', () => {
  it('joins mode labels with collection + source counts', () => {
    expect(
      scopeSummary({
        modes: ['company', 'uploaded'],
        collectionIds: ['c1', 'c2'],
        sourceIds: ['s1'],
      }),
    ).toBe('Company sources, Uploaded files · 2 collections · 1 source');
  });
});

describe('configDiff', () => {
  it('returns the changed fields with both from/to display values', () => {
    const version = config({ model: null, toolAllowlist: ['search_text'] });
    const head = config({
      model: 'anthropic/claude',
      toolAllowlist: ['search_text', 'run_python'],
    });
    const diff = configDiff(version, head, models);
    const keys = diff.map((d) => d.key);
    expect(keys).toContain('model');
    expect(keys).toContain('toolAllowlist');
    const modelDiff = diff.find((d) => d.key === 'model');
    expect(modelDiff?.from).toBe('Smart default');
    expect(modelDiff?.to).toBe('Claude');
  });

  it('is EMPTY when the two configs are equal (a rollback restored the head)', () => {
    expect(configDiff(config(), config(), models)).toEqual([]);
  });

  it('ignores incidental tool ORDER — a reorder is not a change', () => {
    const version = config({ toolAllowlist: ['a', 'b'] });
    const head = config({ toolAllowlist: ['b', 'a'] });
    expect(configDiff(version, head, models)).toEqual([]);
  });

  it('ignores incidental scope order but flags a real scope change', () => {
    const reordered = configDiff(
      config({
        knowledgeScope: { collectionIds: ['c1', 'c2'], sourceIds: [], modes: ['company'] },
      }),
      config({
        knowledgeScope: { collectionIds: ['c2', 'c1'], sourceIds: [], modes: ['company'] },
      }),
      models,
    );
    expect(reordered).toEqual([]);

    const changed = configDiff(
      config({ knowledgeScope: { collectionIds: ['c1'], sourceIds: [], modes: ['company'] } }),
      config({
        knowledgeScope: { collectionIds: ['c1'], sourceIds: [], modes: ['company', 'web'] },
      }),
      models,
    );
    expect(changed.map((d) => d.key)).toEqual(['knowledgeScope']);
  });
});
