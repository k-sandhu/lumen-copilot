/**
 * ModelPicker (AC-3): grouped frontier/fast/oss, default preselected, change sets
 * the model. Accessible (a labelled <select> with <optgroup> groups).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelPicker } from './ModelPicker';
import type { ChatModelInfo } from '@/api';

const MODELS: ChatModelInfo[] = [
  {
    id: 'anthropic/opus',
    label: 'Claude Opus',
    provider: 'anthropic',
    tier: 'frontier',
    is_default: true,
  },
  { id: 'openai/gpt', label: 'GPT', provider: 'openai', tier: 'frontier', is_default: false },
  {
    id: 'anthropic/haiku',
    label: 'Claude Haiku',
    provider: 'anthropic',
    tier: 'fast',
    is_default: false,
  },
  { id: 'qwen/qwen', label: 'Qwen', provider: 'qwen', tier: 'oss', is_default: false },
];

describe('ModelPicker', () => {
  it('renders models grouped by tier (AC-3)', () => {
    render(<ModelPicker models={MODELS} value="anthropic/opus" onChange={() => {}} />);
    const select = screen.getByRole('combobox', { name: /model/i });
    const groups = within(select).getAllByRole('group');
    expect(groups.map((g) => g.getAttribute('label'))).toEqual(['Frontier', 'Fast', 'Open source']);
  });

  it('preselects the provided (default) value (AC-3)', () => {
    render(<ModelPicker models={MODELS} value="anthropic/opus" onChange={() => {}} />);
    expect(screen.getByRole('combobox', { name: /model/i })).toHaveValue('anthropic/opus');
    expect(screen.getByRole('option', { name: /claude opus \(default\)/i })).toBeInTheDocument();
  });

  it('calls onChange with the chosen model id', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ModelPicker models={MODELS} value="anthropic/opus" onChange={onChange} />);
    await user.selectOptions(screen.getByRole('combobox', { name: /model/i }), 'qwen/qwen');
    expect(onChange).toHaveBeenCalledWith('qwen/qwen');
  });

  it('omits empty tiers', () => {
    const onlyFast = MODELS.filter((m) => m.tier === 'fast');
    render(<ModelPicker models={onlyFast} value="anthropic/haiku" onChange={() => {}} />);
    const groups = within(screen.getByRole('combobox', { name: /model/i })).getAllByRole('group');
    expect(groups).toHaveLength(1);
    expect(groups[0]).toHaveAttribute('label', 'Fast');
  });

  it('groups per-tenant provider models under their provider name (PR 2a)', () => {
    // A namespaced provider model arrives in the same /models list; it renders as a
    // selectable option in its own provider-named group, after the built-in tiers.
    const withProvider: ChatModelInfo[] = [
      ...MODELS,
      {
        id: 'provider:3f2504e0-4f89-41d3-9a0c-0305e82c3301:openai/gpt-4o',
        label: 'GPT-4o · Acme OpenAI',
        provider: 'Acme OpenAI',
        tier: 'frontier',
        is_default: false,
      },
    ];
    render(<ModelPicker models={withProvider} value="anthropic/opus" onChange={() => {}} />);
    const select = screen.getByRole('combobox', { name: /model/i });
    const groups = within(select).getAllByRole('group');
    // Built-in tiers first, then the provider group.
    expect(groups.map((g) => g.getAttribute('label'))).toEqual([
      'Frontier',
      'Fast',
      'Open source',
      'Acme OpenAI',
    ]);
    // The provider model is a selectable option carrying its namespaced id.
    const option = screen.getByRole('option', { name: /GPT-4o · Acme OpenAI/i });
    expect(option).toHaveValue('provider:3f2504e0-4f89-41d3-9a0c-0305e82c3301:openai/gpt-4o');
  });

  it('lets the user select a provider model by its namespaced id', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    const withProvider: ChatModelInfo[] = [
      ...MODELS,
      {
        id: 'provider:abc:openai/gpt-4o',
        label: 'GPT-4o · Acme',
        provider: 'Acme',
        tier: 'frontier',
        is_default: false,
      },
    ];
    render(<ModelPicker models={withProvider} value="anthropic/opus" onChange={onChange} />);
    await user.selectOptions(
      screen.getByRole('combobox', { name: /model/i }),
      'provider:abc:openai/gpt-4o',
    );
    expect(onChange).toHaveBeenCalledWith('provider:abc:openai/gpt-4o');
  });
});
