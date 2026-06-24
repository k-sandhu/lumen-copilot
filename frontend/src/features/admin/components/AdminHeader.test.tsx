/**
 * AdminHeader coverage (#122): the tenant-scoped header shows the REAL principal's
 * tenant id (GET /auth/me) — never a fabricated company name — and degrades
 * gracefully while loading or when the principal is unavailable (every async
 * surface resolves to legible text, frontend/AGENTS.md).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { CurrentUser } from '@/api';

const useCurrentUser = vi.hoisted(() => vi.fn());
vi.mock('@/features/auth', () => ({ useCurrentUser }));

import { AdminHeader } from './AdminHeader';

const PRINCIPAL: CurrentUser = {
  id: 'u1',
  email: 'admin@acme.test',
  tenant_id: 'acme-tenant-1234',
  roles: ['admin'],
  created_at: '2026-01-01T00:00:00Z',
};

beforeEach(() => useCurrentUser.mockReset());

describe('AdminHeader', () => {
  it('shows the real tenant id, not a fabricated company name', () => {
    useCurrentUser.mockReturnValue({ data: PRINCIPAL, isLoading: false, isError: false });
    render(<AdminHeader />);
    expect(screen.getByRole('heading', { level: 1, name: 'Admin' })).toBeInTheDocument();
    expect(screen.getByText('acme-tenant-1234')).toBeInTheDocument();
    expect(screen.getByText(/governance, models, and data controls/i)).toBeInTheDocument();
    expect(screen.queryByText(/northwind/i)).not.toBeInTheDocument();
  });

  it('shows a loading placeholder while the principal is in flight', () => {
    useCurrentUser.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    render(<AdminHeader />);
    expect(screen.getByText(/loading tenant/i)).toBeInTheDocument();
  });

  it('degrades to "tenant unavailable" on error', () => {
    useCurrentUser.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    render(<AdminHeader />);
    expect(screen.getByText(/tenant unavailable/i)).toBeInTheDocument();
  });

  it('states the read-only scope', () => {
    useCurrentUser.mockReturnValue({ data: PRINCIPAL, isLoading: false, isError: false });
    render(<AdminHeader />);
    expect(screen.getByText(/not available here/i)).toBeInTheDocument();
  });
});
