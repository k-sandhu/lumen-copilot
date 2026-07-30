/**
 * ToolGovernancePanel — the per-tenant tool-governance surface (#223, area:ui).
 *
 * The one WRITE panel on the admin console: it lists every registered tool with its
 * static risk tier, an enable/disable toggle, and — for write-tier tools — a
 * "requires approval" control. Flipping a control PATCHes /admin/tool-policy (the
 * mutation), which is the switch the backend's policy-driven approval gate consults:
 * turning a gated tool ON and clearing "requires approval" pre-approves it for the
 * tenant (the run_python unlock, AC-2). All admin-only + tenant-scoped: a non-admin
 * caller (403, INV-5) or expired session (401, INV-4) surfaces as the shared panel
 * error, never a blank pane.
 *
 * Every async state is handled (frontend/AGENTS.md: "every state, not just success"):
 * loading shimmer, an actionable read error with Retry, an empty state, and — for the
 * write — a per-row saving indicator plus a dismissible success/error toast so an
 * audited action's outcome is always visible. A read-only (T0) tool cannot require
 * approval, so its approval control is not offered (it would be a no-op the backyard
 * gate never consults).
 */
import { useState } from 'react';
import { RiskTierBadge, type RiskTier as KitRiskTier } from '@/ui';
import { ApiError } from '@/api';
import type { RiskTierId, ToolPolicyEntry } from '@/api';
import { useToolPolicy, useUpdateToolPolicy } from '../model/queries';
import { PanelBody } from './PanelState';

/** The contract's RiskTierId is a structural subset of the kit's RiskTier. */
function toBadgeTier(id: RiskTierId): KitRiskTier {
  return id;
}

/** Best human-readable message for a write error (prefers the RFC-9457 Problem). */
function describeWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'You need the admin role to change tool policy.';
    if (error.status === 401) return 'Your session has expired. Sign in again.';
    if (error.status === 422) return 'That tool is not recognised.';
    return error.displayMessage;
  }
  if (error instanceof Error) return error.message;
  return 'Could not save the tool policy.';
}

/** A small accessible on/off switch mirroring the MCP-servers enable toggle. */
function ToggleSwitch({
  checked,
  disabled,
  label,
  onToggle,
}: {
  checked: boolean;
  disabled: boolean;
  label: string;
  onToggle: () => void;
}) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2">
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        aria-label={label}
        onChange={onToggle}
        className="h-4 w-4 accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
      />
      <span className="text-xs font-medium text-foreground-muted">{checked ? 'On' : 'Off'}</span>
    </label>
  );
}

function ToolRow({
  tool,
  savingTool,
  onSet,
}: {
  tool: ToolPolicyEntry;
  savingTool: string | null;
  onSet: (next: { tool_name: string; enabled: boolean; requires_approval: boolean }) => void;
}) {
  const saving = savingTool === tool.tool_name;
  // A read-only tool can never require approval — the approval control is only
  // meaningful for a write-tier (non-read-only) tool, so we hide it otherwise.
  const canRequireApproval = !tool.read_only;
  return (
    <tr className="border-b border-border/60 align-top last:border-0">
      <td className="px-4 py-3">
        <div className="font-medium text-foreground">{tool.tool_name}</div>
        {tool.is_default ? (
          <div className="mt-0.5 text-xs text-foreground-muted">default</div>
        ) : null}
      </td>
      <td className="px-4 py-3">
        <RiskTierBadge tier={toBadgeTier(tool.risk_tier)} />
      </td>
      <td className="px-4 py-3">
        <ToggleSwitch
          checked={tool.enabled}
          disabled={saving}
          label={`${tool.enabled ? 'Disable' : 'Enable'} ${tool.tool_name}`}
          onToggle={() =>
            onSet({
              tool_name: tool.tool_name,
              enabled: !tool.enabled,
              requires_approval: tool.requires_approval,
            })
          }
        />
      </td>
      <td className="px-4 py-3">
        {canRequireApproval ? (
          <>
            <ToggleSwitch
              checked={tool.requires_approval}
              disabled={saving}
              label={`${
                tool.requires_approval ? 'Pre-approve' : 'Require approval for'
              } ${tool.tool_name}`}
              onToggle={() =>
                onSet({
                  tool_name: tool.tool_name,
                  enabled: tool.enabled,
                  requires_approval: !tool.requires_approval,
                })
              }
            />
            {/*
              Issue #500: there is no in-session approver anywhere in the product yet
              (the gate refuses rather than prompting — `services/tools/gate.py`), so
              leaving this ON does NOT mean "ask me": it refuses every call, for every
              user in the tenant, until an admin clears it. Saying so AT THE CONTROL is
              the point — the admin deciding now is the actor who gets misled, not the
              operator who reads the refusal hours later. Remove this warning when the
              interactive approval flow (#501) ships.
            */}
            {tool.requires_approval ? (
              <p className="mt-1 max-w-[22rem] text-xs text-warning" role="note">
                No approver exists yet, so this <strong>refuses every call</strong> for the whole
                tenant. Clear it to pre-approve the tool.
              </p>
            ) : null}
          </>
        ) : (
          <span className="text-xs text-foreground-muted">n/a</span>
        )}
      </td>
      <td className="px-4 py-3 text-right text-xs text-foreground-muted">
        {saving ? 'Saving…' : null}
      </td>
    </tr>
  );
}

function ToolPolicyTable({
  tools,
  savingTool,
  onSet,
}: {
  tools: ToolPolicyEntry[];
  savingTool: string | null;
  onSet: (next: { tool_name: string; enabled: boolean; requires_approval: boolean }) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">Per-tenant tool governance policy</caption>
        <thead>
          <tr className="border-b border-border text-xs uppercase tracking-wide text-foreground-muted">
            <th scope="col" className="px-4 py-2 font-medium">
              Tool
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Risk tier
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Enabled
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Requires approval
            </th>
            <th scope="col" className="px-4 py-2 font-medium text-right">
              <span className="sr-only">Status</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {tools.map((tool) => (
            <ToolRow key={tool.tool_name} tool={tool} savingTool={savingTool} onSet={onSet} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ToolGovernancePanel() {
  const query = useToolPolicy();
  const mutation = useUpdateToolPolicy();
  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null);
  const tools = query.data?.items ?? [];

  const handleSet = (next: { tool_name: string; enabled: boolean; requires_approval: boolean }) => {
    setToast(null);
    mutation.mutate(next, {
      onSuccess: () => setToast({ kind: 'ok', message: `Updated ${next.tool_name}.` }),
      onError: (error) => setToast({ kind: 'error', message: describeWriteError(error) }),
    });
  };

  const savingTool = mutation.isPending ? (mutation.variables?.tool_name ?? null) : null;

  return (
    <section aria-labelledby="admin-tools-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-tools-heading" className="text-sm font-semibold text-foreground">
          Tool governance
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Enable or disable each tool for this tenant, and set whether a write-tier tool still
          requires approval. A gated tool runs only when it is enabled and pre-approved.
        </p>
      </header>
      {toast ? (
        <div
          role={toast.kind === 'error' ? 'alert' : 'status'}
          className={
            toast.kind === 'error'
              ? 'mx-4 mt-3 rounded-md border border-danger/40 bg-danger/5 p-3 text-sm'
              : 'mx-4 mt-3 rounded-md border border-border bg-surface-muted p-3 text-sm'
          }
        >
          <div className="flex items-center justify-between gap-3">
            <span className="text-foreground">{toast.message}</span>
            <button
              type="button"
              onClick={() => setToast(null)}
              className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-surface"
            >
              Dismiss
            </button>
          </div>
        </div>
      ) : null}
      <PanelBody
        label="tool policy"
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={tools.length === 0}
        emptyMessage="No tools are registered."
        onRetry={() => void query.refetch()}
        loadingRows={5}
      >
        <ToolPolicyTable tools={tools} savingTool={savingTool} onSet={handleSet} />
      </PanelBody>
    </section>
  );
}
