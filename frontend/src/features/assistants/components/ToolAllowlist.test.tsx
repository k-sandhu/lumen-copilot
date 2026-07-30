/**
 * ToolAllowlist state + governance coverage (#505).
 *
 * The defect this pins: the builder's tool picker was a hardcoded four-entry stub of
 * retrieval tools, so `run_python` — a real, registered T2 tool the API has always
 * accepted in `toolAllowlist` — could not be granted from the UI at all. The picker
 * now reads the member-visible catalogue (`GET /tools`) and must:
 *
 * - offer `run_python` and persist the selection through `onChange`;
 * - make its risk LEGIBLE (T2 badge + a side-effecting/approval-gated note) rather
 *   than presenting it as an ordinary read-only tool;
 * - show a tool the tenant's admin DISABLED as unavailable and refuse to select it,
 *   instead of offering a control the run-time gate would silently refuse;
 * - still let an already-granted, now-unavailable tool be removed (never a config
 *   the owner can see but not fix);
 * - cover every async state (frontend/AGENTS.md: not just success) — loading,
 *   an actionable error with retry, and empty.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { ToolCatalog, ToolCatalogEntry } from '@/api';
import { ToolAllowlist } from './ToolAllowlist';

const listTools = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return { ...actual, listTools };
});

function entry(overrides: Partial<ToolCatalogEntry> = {}): ToolCatalogEntry {
  return {
    tool_name: 'search_text',
    description: 'Keyword search across permitted sources.',
    risk_tier: 'T0',
    read_only: true,
    enabled: true,
    requires_approval: false,
    ...overrides,
  };
}

const RUN_PYTHON: ToolCatalogEntry = entry({
  tool_name: 'run_python',
  description: 'Write and run Python in an isolated sandbox to compute a result.',
  risk_tier: 'T2',
  read_only: false,
  enabled: true,
  requires_approval: true,
});

const CATALOG: ToolCatalog = { items: [entry(), RUN_PYTHON] };

function renderPicker(value: string[] = [], onChange = vi.fn()) {
  renderWithQuery(<ToolAllowlist value={value} onChange={onChange} />);
  return onChange;
}

beforeEach(() => {
  listTools.mockReset();
});

describe('ToolAllowlist — selecting run_python (#505)', () => {
  it('offers run_python from the catalogue and reports the selection', async () => {
    listTools.mockResolvedValue(CATALOG);
    const onChange = renderPicker([]);

    const box = await screen.findByRole('checkbox', { name: /run python/i });
    expect(box).toBeEnabled();
    await userEvent.click(box);

    expect(onChange).toHaveBeenCalledWith(['run_python']);
  });

  it('renders an already-granted run_python as checked (it persists)', async () => {
    listTools.mockResolvedValue(CATALOG);
    renderPicker(['run_python']);

    expect(await screen.findByRole('checkbox', { name: /run python/i })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /text search/i })).not.toBeChecked();
  });

  it('makes the T2 / side-effecting risk visible, not an ordinary tool', async () => {
    listTools.mockResolvedValue(CATALOG);
    renderPicker([]);

    await screen.findByRole('checkbox', { name: /run python/i });
    const row = screen.getByRole('listitem', { name: /run python/i });
    // The risk-tier badge the admin console uses, on the picker row itself.
    expect(within(row).getByLabelText(/risk tier T2/i)).toBeInTheDocument();
    expect(within(row).getByText(/side-effecting/i)).toBeInTheDocument();
    // The approval seam is stated, not hidden behind a plain checkbox.
    expect(within(row).getByText(/requires approval/i)).toBeInTheDocument();

    // A read-only retrieval tool carries neither affordance.
    const readOnlyRow = screen.getByRole('listitem', { name: /text search/i });
    expect(within(readOnlyRow).queryByText(/side-effecting/i)).not.toBeInTheDocument();
  });
});

describe('ToolAllowlist — tenant policy is reflected honestly', () => {
  it('shows an admin-disabled tool as unavailable and refuses to select it', async () => {
    listTools.mockResolvedValue({
      items: [entry(), { ...RUN_PYTHON, enabled: false }],
    });
    const onChange = renderPicker([]);

    const box = await screen.findByRole('checkbox', { name: /run python/i });
    expect(box).toBeDisabled();

    const row = screen.getByRole('listitem', { name: /run python/i });
    expect(within(row).getByText(/unavailable/i)).toBeInTheDocument();

    await userEvent.click(box);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('still lets an already-granted, now-unavailable tool be REMOVED', async () => {
    listTools.mockResolvedValue({
      items: [entry(), { ...RUN_PYTHON, enabled: false }],
    });
    const onChange = renderPicker(['run_python']);

    const box = await screen.findByRole('checkbox', { name: /run python/i });
    expect(box).toBeChecked();
    expect(box).toBeEnabled();

    await userEvent.click(box);
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it('shows a granted tool the catalogue no longer offers, so it can be cleared', async () => {
    listTools.mockResolvedValue({ items: [entry()] });
    const onChange = renderPicker(['retired_tool']);

    const box = await screen.findByRole('checkbox', { name: /retired_tool/i });
    expect(box).toBeChecked();
    await userEvent.click(box);
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it('disables every control when the form is busy', async () => {
    listTools.mockResolvedValue(CATALOG);
    renderWithQuery(<ToolAllowlist value={[]} onChange={vi.fn()} disabled />);

    expect(await screen.findByRole('checkbox', { name: /run python/i })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /text search/i })).toBeDisabled();
  });
});

describe('ToolAllowlist — async states', () => {
  it('shows a loading state while the catalogue resolves', async () => {
    listTools.mockReturnValue(new Promise<ToolCatalog>(() => {}));
    renderPicker([]);
    expect(await screen.findByRole('status', { name: /loading tools/i })).toBeInTheDocument();
  });

  it('surfaces a catalogue read failure with a retry, never a blank pane', async () => {
    listTools.mockRejectedValue(new ApiError('boom', 500));
    renderPicker([]);

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/could not load the tool list/i)).toBeInTheDocument();
    const retry = within(alert).getByRole('button', { name: /retry/i });

    listTools.mockResolvedValue(CATALOG);
    await userEvent.click(retry);
    expect(await screen.findByRole('checkbox', { name: /run python/i })).toBeInTheDocument();
  });

  it('shows an empty state when no tools are registered', async () => {
    listTools.mockResolvedValue({ items: [] });
    renderPicker([]);
    expect(await screen.findByText(/no tools are available/i)).toBeInTheDocument();
  });
});
