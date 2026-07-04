/**
 * Model picker (AC-3): the curated models from GET /models, grouped by tier
 * (frontier / fast / oss), default preselected. Setting it overrides the model
 * for the next turn (and persists to the session via PATCH at the call site).
 * A native grouped <select> keeps it keyboard- and screen-reader-accessible.
 *
 * Per-tenant provider models (PR 2a) arrive in the same GET /models list with a
 * namespaced ``provider:{id}:{model}`` id; those are grouped under their PROVIDER
 * name (after the built-in tier groups) rather than by tier, so a tenant's own
 * models are clearly separated from the built-in registry. Built-in (config) models
 * keep their tier grouping unchanged.
 *
 * Loading/empty/error are handled by the parent (it passes the resolved list);
 * this component is presentational.
 */
import type { ChatModelInfo, ModelTier } from '@/api';

const TIER_ORDER: ModelTier[] = ['frontier', 'fast', 'oss'];
const TIER_LABEL: Record<ModelTier, string> = {
  frontier: 'Frontier',
  fast: 'Fast',
  oss: 'Open source',
};

/** The id prefix marking a per-tenant provider model (mirrors the backend). */
const PROVIDER_ID_PREFIX = 'provider:';

function isProviderModel(model: ChatModelInfo): boolean {
  return model.id.startsWith(PROVIDER_ID_PREFIX);
}

export interface ModelPickerProps {
  models: ChatModelInfo[];
  /** Currently selected model id. */
  value: string;
  onChange: (modelId: string) => void;
  disabled?: boolean;
}

export function ModelPicker({ models, value, onChange, disabled = false }: ModelPickerProps) {
  // Built-in (config) models group by tier, in the fixed frontier→fast→oss order.
  const configModels = models.filter((m) => !isProviderModel(m));
  const byTier = TIER_ORDER.map((tier) => ({
    key: `tier:${tier}`,
    label: TIER_LABEL[tier],
    items: configModels.filter((m) => m.tier === tier),
  })).filter((g) => g.items.length > 0);

  // Per-tenant provider models group by provider name (first-seen order), so each
  // registered provider becomes its own optgroup after the built-in tiers.
  const providerGroups: { key: string; label: string; items: ChatModelInfo[] }[] = [];
  for (const m of models) {
    if (!isProviderModel(m)) continue;
    const existing = providerGroups.find((g) => g.label === m.provider);
    if (existing) existing.items.push(m);
    else providerGroups.push({ key: `provider:${m.provider}`, label: m.provider, items: [m] });
  }

  const groups = [...byTier, ...providerGroups];

  return (
    <label className="flex min-w-0 items-center">
      <span className="sr-only">Model</span>
      <select
        aria-label="Model"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="lc-modelpicker"
      >
        {groups.map((group) => (
          <optgroup key={group.key} label={group.label}>
            {group.items.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
                {m.is_default ? ' (default)' : ''}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </label>
  );
}
