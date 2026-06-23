import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PermissionPill } from './PermissionPill';

describe('PermissionPill', () => {
  it('renders the granted variant with its default label and a status role', () => {
    render(<PermissionPill level="granted" />);
    const pill = screen.getByRole('status', { name: /you have access/i });
    expect(pill).toHaveClass('lc-perm');
    expect(pill).not.toHaveClass('lc-perm--restricted');
  });

  it('applies the restricted modifier and amber semantics', () => {
    render(<PermissionPill level="restricted" />);
    expect(screen.getByRole('status')).toHaveClass('lc-perm--restricted');
  });

  it('applies the denied modifier', () => {
    render(<PermissionPill level="denied" />);
    expect(screen.getByRole('status')).toHaveClass('lc-perm--denied');
  });

  it('supports a custom label and accessible title', () => {
    render(
      <PermissionPill
        level="restricted"
        label="Restricted · masked fields"
        title="3 fields hidden"
      />,
    );
    expect(screen.getByText('Restricted · masked fields')).toBeInTheDocument();
    expect(screen.getByRole('status', { name: '3 fields hidden' })).toBeInTheDocument();
  });
});
