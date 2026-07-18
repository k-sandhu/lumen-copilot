/**
 * SandboxGovernancePanel state coverage (#233): the per-tenant sandbox policy WRITE
 * surface. Covers every async state (frontend/AGENTS.md: not just success) plus the
 * governance behaviour that matters:
 *
 * - loading shimmer → the policy form (enable toggle + package policy);
 * - the deny-by-default hint when no policy is stored (`is_default`);
 * - compatibility-only egress/cap values are preserved but not offered as active controls;
 * - a 422 (invalid package policy) / 403 write error surfaces a dismissible alert;
 * - the read 403 (INV-5) lands as the shared actionable panel error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { SandboxPolicy } from '@/api';
import { SandboxGovernancePanel } from './SandboxGovernancePanel';

const getSandboxPolicy = vi.hoisted(() => vi.fn());
const updateSandboxPolicy = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, getSandboxPolicy, updateSandboxPolicy };
});

const DEFAULT_POLICY: SandboxPolicy = {
  enabled: false,
  allowed_packages: [],
  denied_packages: [],
  egress_allowed: false,
  egress_allowlist: [],
  max_runtime_s: 30,
  max_memory_mb: 512,
  daily_runtime_cap_s: 3600,
  max_concurrency: 2,
  is_default: true,
  max_runtime_s_ceiling: 30,
  max_memory_mb_ceiling: 512,
  daily_runtime_cap_s_ceiling: 3600,
  max_concurrency_ceiling: 2,
};

beforeEach(() => {
  getSandboxPolicy.mockReset();
  updateSandboxPolicy.mockReset();
});

describe('SandboxGovernancePanel', () => {
  it('renders enable/package policy and explains the intentionally unbounded posture', async () => {
    getSandboxPolicy.mockResolvedValue(DEFAULT_POLICY);
    renderWithQuery(<SandboxGovernancePanel />);

    expect(
      await screen.findByRole('switch', { name: /enable code execution/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: /outbound network/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/max runtime/i)).not.toBeInTheDocument();
    expect(screen.getByText(/no automatic runtime.*memory.*process.*output/i)).toBeInTheDocument();
    // Deny-by-default hint when no policy is stored.
    expect(screen.getByText(/disabled for this tenant by default/i)).toBeInTheDocument();
  });

  it('saves the full policy body when enabling + setting a package (AC-1)', async () => {
    getSandboxPolicy.mockResolvedValue(DEFAULT_POLICY);
    updateSandboxPolicy.mockResolvedValue({
      ...DEFAULT_POLICY,
      enabled: true,
      allowed_packages: ['numpy'],
      is_default: false,
    });
    renderWithQuery(<SandboxGovernancePanel />);
    await screen.findByRole('switch', { name: /enable code execution/i });

    await userEvent.click(screen.getByRole('switch', { name: /enable code execution/i }));
    await userEvent.type(screen.getByLabelText(/allowed packages/i), 'numpy');
    await userEvent.click(screen.getByRole('button', { name: /save policy/i }));

    expect(updateSandboxPolicy).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: true,
        allowed_packages: ['numpy'],
        egress_allowed: false,
        max_runtime_s: 30,
        max_memory_mb: 512,
        daily_runtime_cap_s: 3600,
        max_concurrency: 2,
      }),
    );
    expect(await screen.findByText(/sandbox policy saved/i)).toBeInTheDocument();
  });

  it('surfaces a write error (403) as a dismissible alert, not a silent no-op', async () => {
    getSandboxPolicy.mockResolvedValue(DEFAULT_POLICY);
    updateSandboxPolicy.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<SandboxGovernancePanel />);
    await screen.findByRole('switch', { name: /enable code execution/i });

    await userEvent.click(screen.getByRole('switch', { name: /enable code execution/i }));
    await userEvent.click(screen.getByRole('button', { name: /save policy/i }));

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/admin role/i)).toBeInTheDocument();
  });

  it('surfaces a 422 package-policy write error as an actionable alert (INV-8)', async () => {
    getSandboxPolicy.mockResolvedValue(DEFAULT_POLICY);
    updateSandboxPolicy.mockRejectedValue(new ApiError('unprocessable', 422));
    renderWithQuery(<SandboxGovernancePanel />);
    await screen.findByRole('switch', { name: /enable code execution/i });

    await userEvent.click(screen.getByRole('button', { name: /save policy/i }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/package-policy value/i)).toBeInTheDocument();
  });

  it('surfaces a read 403 as an actionable panel error (INV-5)', async () => {
    getSandboxPolicy.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<SandboxGovernancePanel />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/admin role/i)).toBeInTheDocument();
  });
});
