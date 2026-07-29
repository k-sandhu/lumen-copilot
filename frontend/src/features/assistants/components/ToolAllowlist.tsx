/**
 * ToolAllowlist (#212 → #505) — the tools an assistant's runs may use, persisted
 * into `toolAllowlist: string[]`.
 *
 * The candidate list is now the SERVER's tool catalogue (`GET /tools`), not a
 * hardcoded stub. The stub listed four retrieval tools, which meant `run_python` —
 * a real, registered T2 tool the API has always accepted here — was ungrantable
 * from the UI; the one assistant that had it was seeded straight into the database
 * (#505). Reading the catalogue also means a newly registered backend tool shows up
 * without a frontend release.
 *
 * **Governance is displayed, never bypassed.** Ticking a box is a configuration-time
 * grant, not an authorisation: the tenant's tool policy and the run-time approval
 * gate still decide whether an allowed tool executes. So the picker states what it
 * knows instead of pretending every tool is equal:
 *
 * - a write-tier (non-`T0`) tool carries its risk-tier badge and a "side-effecting"
 *   note — `run_python` is T2, not an ordinary read-only helper;
 * - an approval-gated tool says an admin still has to pre-approve it, so a builder
 *   is not surprised by a refusal at run time;
 * - a tool the tenant's admin DISABLED renders as **unavailable** and cannot be
 *   ticked, rather than offering a control whose selection would silently fail.
 *   One exception: if such a tool is already granted it stays untickable-but-
 *   removable, so an owner can always repair a config they can see.
 *
 * Every async state is handled (frontend/AGENTS.md): a loading placeholder, an
 * actionable error with Retry, and an empty state — no blank pane.
 *
 * A11y: a real <fieldset>/<legend> with native checkboxes, each labelled by its tool
 * name and described by its summary + governance notes; fully keyboard-navigable.
 */
import { useId } from 'react';
import { ApiError } from '@/api';
import { RiskTierBadge } from '@/ui';
import { useToolCatalog } from '../model/queries';
import { toToolOptions, type ToolOption } from '../model/tools';

interface ToolAllowlistProps {
  /** Currently-allowed tool names. */
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}

export function ToolAllowlist({ value, onChange, disabled = false }: ToolAllowlistProps) {
  const groupId = useId();
  const catalog = useToolCatalog();
  const selected = new Set(value);

  const toggle = (name: string) => {
    const next = new Set(selected);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    onChange([...next]);
  };

  const options = toToolOptions(catalog.data?.items ?? [], value);

  return (
    <fieldset disabled={disabled} className="min-w-0">
      <legend className="text-sm font-medium">Tools</legend>
      <p className="mt-0.5 text-xs text-foreground-muted">
        Which tools this assistant’s runs may use. Leaving all unticked uses the default retrieval
        tools. Allowing a tool does not bypass your tenant’s policy — a governed tool still runs
        only when an admin has enabled and approved it.
      </p>

      {catalog.isPending ? (
        <ToolsLoading />
      ) : catalog.isError ? (
        <ToolsError error={catalog.error} onRetry={() => void catalog.refetch()} />
      ) : options.length === 0 ? (
        <p className="mt-3 rounded-md border border-border bg-surface-muted p-3 text-xs text-foreground-muted">
          No tools are available for this tenant yet.
        </p>
      ) : (
        <ul className="mt-2 space-y-2" aria-label="Allowed tools">
          {options.map((tool) => (
            <ToolRow
              key={tool.name}
              tool={tool}
              idPrefix={groupId}
              checked={selected.has(tool.name)}
              onToggle={() => toggle(tool.name)}
            />
          ))}
        </ul>
      )}
    </fieldset>
  );
}

/**
 * One tool row: the checkbox, its label + registry summary, and the governance
 * affordances. A disabled-by-policy tool is untickable but stays removable when it
 * is already granted (an owner must always be able to clear a stale grant).
 */
function ToolRow({
  tool,
  idPrefix,
  checked,
  onToggle,
}: {
  tool: ToolOption;
  idPrefix: string;
  checked: boolean;
  onToggle: () => void;
}) {
  const id = `${idPrefix}-${tool.name}`;
  const labelId = `${id}-label`;
  const descId = `${id}-desc`;
  const unavailable = !tool.enabled;
  // Untickable while unavailable — unless it is already granted, in which case the
  // only useful action left is removing it.
  const locked = unavailable && !checked;

  return (
    <li aria-label={tool.label}>
      <label htmlFor={id} className="flex cursor-pointer items-start gap-2">
        <input
          id={id}
          type="checkbox"
          checked={checked}
          disabled={locked}
          aria-labelledby={labelId}
          aria-describedby={descId}
          onChange={onToggle}
          className="mt-0.5 h-4 w-4 shrink-0 accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
        />
        <span className="min-w-0">
          <span className="flex flex-wrap items-center gap-1.5">
            <span id={labelId} className="text-sm">
              {tool.label}
            </span>
            {tool.riskTier !== 'T0' ? <RiskTierBadge tier={tool.riskTier} /> : null}
          </span>
          <span id={descId} className="block">
            <span className="block text-xs text-foreground-muted">{tool.description}</span>
            {!tool.readOnly ? (
              <span className="mt-0.5 block text-xs text-warn">
                Side-effecting: this tool acts, it does not just read.
              </span>
            ) : null}
            {unavailable ? (
              <span className="mt-0.5 block text-xs text-danger">
                Unavailable — an admin has turned this tool off for your tenant.
              </span>
            ) : tool.requiresApproval ? (
              <span className="mt-0.5 block text-xs text-foreground-muted">
                Requires approval — an admin must pre-approve it in Admin → Tool governance before a
                run can use it.
              </span>
            ) : null}
          </span>
        </span>
      </label>
    </li>
  );
}

function ToolsLoading() {
  return (
    <div role="status" aria-label="Loading tools" aria-busy="true" className="mt-3 space-y-2">
      <span className="sr-only">Loading tools…</span>
      {[0, 1, 2].map((i) => (
        <div key={i} className="lc-skeleton" style={{ width: '70%' }} aria-hidden="true" />
      ))}
    </div>
  );
}

function ToolsError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const expired = error instanceof ApiError && error.status === 401;
  return (
    <div
      role="alert"
      className="mt-3 space-y-2 rounded-md border border-danger/40 bg-danger/10 p-3 text-xs"
    >
      <p className="text-danger">
        Could not load the tool list.{' '}
        {expired
          ? 'Your session expired — sign in again.'
          : 'Existing tool choices are unchanged; try again to edit them.'}
      </p>
      {!expired ? (
        <button
          type="button"
          onClick={onRetry}
          className="rounded-md border border-border bg-surface px-2 py-1 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
