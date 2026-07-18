import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SandboxSession } from '@/api';
import { renderWithQuery } from '@/test/renderWithQuery';

const { getSandboxSession, resetSandboxSession, closeSandboxSession } = vi.hoisted(() => ({
  getSandboxSession: vi.fn<() => Promise<SandboxSession>>(),
  resetSandboxSession: vi.fn<() => Promise<SandboxSession>>(),
  closeSandboxSession: vi.fn<() => Promise<void>>(),
}));

vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api');
  return { ...actual, getSandboxSession, resetSandboxSession, closeSandboxSession };
});

import { SandboxSessionControl } from './SandboxSessionControl';

beforeEach(() => {
  getSandboxSession.mockReset();
  resetSandboxSession.mockReset();
  closeSandboxSession.mockReset();
});

describe('SandboxSessionControl', () => {
  it('shows an active root-inside generation and closes it', async () => {
    getSandboxSession.mockResolvedValue({
      status: 'active',
      enabled: true,
      root_access: true,
      sandbox_session_id: 'sandbox-1',
      generation: 3,
    });
    closeSandboxSession.mockResolvedValue();
    const user = userEvent.setup();
    renderWithQuery(<SandboxSessionControl sessionId="chat-1" />);

    expect(await screen.findByText(/generation 3.*root inside container/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /close sandbox/i }));
    expect(closeSandboxSession).not.toHaveBeenCalled();
    expect(screen.getByText(/closing deletes installed packages/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /delete state and close/i }));
    expect(closeSandboxSession).toHaveBeenCalledOnce();
    expect(await screen.findByText(/sandbox: closed/i)).toBeInTheDocument();
  });

  it('requires an explicit destructive confirmation before reset', async () => {
    getSandboxSession.mockResolvedValue({
      status: 'not_created',
      enabled: true,
      root_access: true,
    });
    resetSandboxSession.mockResolvedValue({
      status: 'active',
      enabled: true,
      root_access: true,
      sandbox_session_id: 'sandbox-1',
      generation: 1,
    });
    const user = userEvent.setup();
    renderWithQuery(<SandboxSessionControl sessionId="chat-1" />);

    await user.click(await screen.findByRole('button', { name: /reset sandbox/i }));
    expect(resetSandboxSession).not.toHaveBeenCalled();
    expect(screen.getByText(/deletes installed packages/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /delete state and reset/i }));
    expect(resetSandboxSession).toHaveBeenCalledOnce();
    expect(await screen.findByText(/generation 1.*root inside container/i)).toBeInTheDocument();
  });

  it('does not offer lifecycle controls when deploy or tenant policy disables execution', async () => {
    getSandboxSession.mockResolvedValue({
      status: 'not_created',
      enabled: false,
      root_access: true,
    });
    renderWithQuery(<SandboxSessionControl sessionId="chat-1" />);

    expect(await screen.findByText(/sandbox: unavailable/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /reset sandbox/i })).not.toBeInTheDocument();
  });
});
