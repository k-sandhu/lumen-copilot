/**
 * Tests for the audit filter bar (#86) — the draft→wire conversion (incl. the
 * datetime-local → ISO-8601 normalization the frozen contract expects) and the
 * interactive form (apply / clear, labelled controls).
 */
import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AuditFilters } from './AuditFilters';
import {
  EMPTY_DRAFT,
  draftToFilters,
  isEmptyDraft,
  type AuditFilterDraft,
} from '../model/filterDraft';

describe('draftToFilters', () => {
  it('trims text and drops empties', () => {
    const draft: AuditFilterDraft = {
      actor: '  dana@acme  ',
      event_type: 'permission.denied',
      resource_id: ' doc-9 ',
      from: '',
      to: '',
    };
    const filters = draftToFilters(draft);
    expect(filters.actor).toBe('dana@acme');
    expect(filters.event_type).toBe('permission.denied');
    expect(filters.resource_id).toBe('doc-9');
    expect(filters.from).toBeUndefined();
    expect(filters.to).toBeUndefined();
  });

  it('converts datetime-local values to ISO-8601 for the wire', () => {
    const filters = draftToFilters({
      ...EMPTY_DRAFT,
      from: '2026-06-01T08:30',
    });
    expect(filters.from).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
    expect(filters.from?.endsWith('Z')).toBe(true);
  });
});

describe('isEmptyDraft', () => {
  it('is true for the empty draft and false once any field is set', () => {
    expect(isEmptyDraft(EMPTY_DRAFT)).toBe(true);
    expect(isEmptyDraft({ ...EMPTY_DRAFT, actor: 'x' })).toBe(false);
  });
});

describe('AuditFilters form', () => {
  function setup(initial: AuditFilterDraft = EMPTY_DRAFT) {
    const onChange = vi.fn();
    const onApply = vi.fn();
    const onClear = vi.fn();
    render(
      <AuditFilters draft={initial} onChange={onChange} onApply={onApply} onClear={onClear} />,
    );
    return { onChange, onApply, onClear };
  }

  it('exposes labelled controls for every filter', () => {
    setup();
    expect(screen.getByLabelText(/^actor$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/event type/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^resource$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^from$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^to$/i)).toBeInTheDocument();
  });

  it('emits a change when a field is edited', async () => {
    const { onChange } = setup();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/^actor$/i), 'a');
    expect(onChange).toHaveBeenCalled();
  });

  it('applies on submit', async () => {
    const { onApply } = setup();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /apply/i }));
    expect(onApply).toHaveBeenCalledTimes(1);
  });

  it('disables Clear when no filter is set and enables it once one is', () => {
    setup();
    expect(screen.getByRole('button', { name: /clear/i })).toBeDisabled();

    render(
      <AuditFilters
        draft={{ ...EMPTY_DRAFT, actor: 'x' }}
        onChange={vi.fn()}
        onApply={vi.fn()}
        onClear={vi.fn()}
      />,
    );
    expect(
      screen.getAllByRole('button', { name: /clear/i }).some((b) => !b.hasAttribute('disabled')),
    ).toBe(true);
  });
});
