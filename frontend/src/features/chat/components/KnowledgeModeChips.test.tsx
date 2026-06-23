/**
 * KnowledgeModeChips (#89): the grounding-scope trust signal on the composer.
 * "Company sources" is the default; "Selected sources" is disabled until a
 * collection scope is supplied (we never imply an unwired capability), and the
 * group is keyboard-accessible.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { KnowledgeModeChips } from './KnowledgeModeChips';

describe('KnowledgeModeChips', () => {
  it('marks the active mode via aria-pressed', () => {
    render(<KnowledgeModeChips value="company" onChange={() => {}} />);
    expect(screen.getByRole('button', { name: /company sources/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByRole('button', { name: /selected sources/i })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('disables "Selected sources" until a collection scope is available', () => {
    render(<KnowledgeModeChips value="company" onChange={() => {}} />);
    expect(screen.getByRole('button', { name: /selected sources/i })).toBeDisabled();
  });

  it('enables "Selected sources" and changes mode when a scope is available', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<KnowledgeModeChips value="company" onChange={onChange} selectedAvailable />);
    const selected = screen.getByRole('button', { name: /selected sources/i });
    expect(selected).not.toBeDisabled();
    await user.click(selected);
    expect(onChange).toHaveBeenCalledWith('selected');
  });

  it('disables the whole group while streaming', () => {
    render(<KnowledgeModeChips value="company" onChange={() => {}} selectedAvailable disabled />);
    expect(screen.getByRole('button', { name: /company sources/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /selected sources/i })).toBeDisabled();
  });
});
