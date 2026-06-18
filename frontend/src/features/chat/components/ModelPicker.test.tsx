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
  { id: 'anthropic/opus', label: 'Claude Opus', provider: 'anthropic', tier: 'frontier', is_default: true },
  { id: 'openai/gpt', label: 'GPT', provider: 'openai', tier: 'frontier', is_default: false },
  { id: 'anthropic/haiku', label: 'Claude Haiku', provider: 'anthropic', tier: 'fast', is_default: false },
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
});
