import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModeToggle } from './ModeToggle';

describe('ModeToggle', () => {
  it('renders three appearance options in a labelled group', () => {
    render(<ModeToggle mode="system" onChange={() => {}} />);
    const group = screen.getByRole('group', { name: /appearance mode/i });
    expect(group).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Light' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Dark' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'System' })).toBeInTheDocument();
  });

  it('marks the current mode with aria-pressed', () => {
    render(<ModeToggle mode="dark" onChange={() => {}} />);
    expect(screen.getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Light' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('calls onChange with the selected mode (controlled)', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ModeToggle mode="system" onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: 'Light' }));
    expect(onChange).toHaveBeenCalledWith('light');
  });
});
