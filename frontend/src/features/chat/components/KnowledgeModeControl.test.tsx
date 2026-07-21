/**
 * KnowledgeModeControl (#221, epic E3-12): the composer control surfacing the
 * four wire knowledge modes (company / uploaded / web / model). It reflects the
 * active modes, allows a per-chat override, and renders the governed WEB toggle
 * DISABLED WITH A REASON when web isn't enabled (AC-3, the negative) — never a
 * silent no-op.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { KnowledgeModeControl } from './KnowledgeModeControl';
import { resolveAvailability } from '../model/knowledgeModes';

describe('KnowledgeModeControl', () => {
  it('marks active modes via aria-pressed and offers all four (E3-12 visible)', () => {
    render(
      <KnowledgeModeControl
        value={['company', 'web']}
        availability={{ web: { available: true } }}
        onChange={() => {}}
      />,
    );
    const group = screen.getByRole('group', { name: /knowledge modes/i });
    expect(group).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Company' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Web' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Uploaded' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
    expect(screen.getByRole('button', { name: 'Model' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('toggles a mode and reports the new set in canonical order (E3-12 changeable)', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<KnowledgeModeControl value={['company']} onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: 'Model' }));
    expect(onChange).toHaveBeenCalledWith(['company', 'model']);
  });

  it('turns a mode OFF when it is already active', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<KnowledgeModeControl value={['company', 'uploaded']} onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: 'Uploaded' }));
    expect(onChange).toHaveBeenCalledWith(['company']);
  });

  it('disables Web WITH A REASON when web is not enabled (AC-3, negative)', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <KnowledgeModeControl
        value={['company']}
        availability={{ web: { available: false, reason: 'Web is off for this workspace.' } }}
        onChange={onChange}
      />,
    );
    const web = screen.getByRole('button', { name: 'Web' });
    expect(web).toBeDisabled();
    // A clear, discoverable reason (tooltip + an accessible description) — not a
    // silent no-op. Clicking does nothing.
    expect(web).toHaveAttribute('title', 'Web is off for this workspace.');
    const descId = web.getAttribute('aria-describedby');
    expect(descId).toBeTruthy();
    expect(document.getElementById(descId as string)).toHaveTextContent(
      'Web is off for this workspace.',
    );
    await user.click(web);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('fails web CLOSED: absent availability ⇒ disabled with a default reason', () => {
    render(<KnowledgeModeControl value={['company']} onChange={() => {}} />);
    const web = screen.getByRole('button', { name: 'Web' });
    expect(web).toBeDisabled();
    expect(web.getAttribute('title')).toMatch(/off/i);
  });

  it('disables the whole group while streaming', () => {
    render(
      <KnowledgeModeControl
        value={['company']}
        availability={{ web: { available: true } }}
        onChange={() => {}}
        disabled
      />,
    );
    for (const name of ['Company', 'Uploaded', 'Web', 'Model']) {
      expect(screen.getByRole('button', { name })).toBeDisabled();
    }
  });
});

describe('resolveAvailability', () => {
  it('fails web closed when unspecified', () => {
    expect(resolveAvailability('web', undefined).available).toBe(false);
    expect(resolveAvailability('web', {}).available).toBe(false);
  });

  it('respects an explicit web availability', () => {
    expect(resolveAvailability('web', { web: { available: true } }).available).toBe(true);
  });

  it('leaves non-web modes available by default', () => {
    expect(resolveAvailability('company', undefined).available).toBe(true);
    expect(resolveAvailability('uploaded', undefined).available).toBe(true);
    expect(resolveAvailability('model', undefined).available).toBe(true);
  });
});
