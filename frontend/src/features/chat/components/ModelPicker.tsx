/**
 * Model picker (AC-3): the curated models from GET /models, grouped by tier
 * (frontier / fast / oss), default preselected. Setting it overrides the model
 * for the next turn (and persists to the session via PATCH at the call site).
 * A native grouped <select> keeps it keyboard- and screen-reader-accessible.
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

export interface ModelPickerProps {
  models: ChatModelInfo[];
  /** Currently selected model id. */
  value: string;
  onChange: (modelId: string) => void;
  disabled?: boolean;
}

export function ModelPicker({ models, value, onChange, disabled = false }: ModelPickerProps) {
  const byTier = TIER_ORDER.map((tier) => ({
    tier,
    items: models.filter((m) => m.tier === tier),
  })).filter((g) => g.items.length > 0);

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
        {byTier.map((group) => (
          <optgroup key={group.tier} label={TIER_LABEL[group.tier]}>
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
