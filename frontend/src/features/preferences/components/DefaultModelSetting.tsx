/**
 * Default-model preference control (epic #144, Phase 2a / issue #167). Lives in
 * the Appearance & preferences popover. Unlike the device-local appearance axes,
 * this is an ACCOUNT preference: it reads/writes the caller's server-side
 * `default_model_id` (GET/PATCH /preferences), so it follows the user across
 * devices. New chat sessions created without an explicit model start from it
 * (the backend seeds `session.model`, fail-closed).
 *
 * Options come from the same curated registry the per-turn picker uses
 * (GET /models, tier-grouped). The "use server default" choice clears the
 * override (PATCH `default_model_id: null`). An unknown id is a 422
 * `unknown_model`, surfaced inline. Loading / error / success are all handled.
 */
import { useMemo } from 'react';
import { ApiError } from '@/api';
import type { ModelTier } from '@/api';
// The registry comes from the dependency-free `models` slice, NOT the chat barrel
// (FE-9): this control is rendered eagerly by the app shell, so importing
// `@/features/chat` here would pull ChatView → lib/markdown into the entry chunk
// and close a chat↔preferences barrel cycle. Guarded by
// `buildguards/chat-pipeline-not-in-entry.test.ts`.
import { useModels } from '@/features/models';
import { usePreferences, useUpdatePreferences } from '../model/queries';

const TIER_ORDER: ModelTier[] = ['frontier', 'fast', 'oss'];
const TIER_LABEL: Record<ModelTier, string> = {
  frontier: 'Frontier',
  fast: 'Fast',
  oss: 'Open source',
};

/** Empty `<select>` value → clear the override (use the server default). */
const SERVER_DEFAULT = '';

function mutationErrorMessage(error: unknown): string | null {
  if (!error) return null;
  if (error instanceof ApiError) {
    if (error.problem?.code === 'unknown_model') {
      return "That model isn't available anymore — please choose another.";
    }
    return error.displayMessage;
  }
  return 'Could not save your default model. Please try again.';
}

export function DefaultModelSetting() {
  const prefs = usePreferences();
  const models = useModels();
  const update = useUpdatePreferences();

  const modelItems = models.data?.items;
  const serverDefault = modelItems?.find((m) => m.is_default) ?? null;
  const byTier = useMemo(() => {
    const list = modelItems ?? [];
    return TIER_ORDER.map((tier) => ({
      tier,
      items: list.filter((m) => m.tier === tier),
    })).filter((g) => g.items.length > 0);
  }, [modelItems]);

  const loadError = prefs.error ?? models.error;
  if (loadError) {
    return (
      <div className="lc-aps__section">
        <div className="lc-aps__label">Default model</div>
        <p role="alert" className="mt-1 text-xs" style={{ color: 'var(--danger, #b42318)' }}>
          Couldn't load your model preference. Please try again.
        </p>
      </div>
    );
  }

  if (prefs.isLoading || models.isLoading) {
    return (
      <div className="lc-aps__section">
        <div className="lc-aps__label">Default model</div>
        <p className="mt-1 text-xs opacity-70">Loading…</p>
      </div>
    );
  }

  // A stored model that is no longer in the registry falls back to the server
  // default (mirroring the backend's fail-closed answer-time behavior and the
  // chat model preselect), with a prompt to choose a new one.
  const storedId = prefs.data?.default_model_id ?? null;
  const storedIsValid = storedId !== null && (modelItems ?? []).some((m) => m.id === storedId);
  const staleId = storedId !== null && !storedIsValid ? storedId : null;
  const selected = storedId !== null && staleId === null ? storedId : SERVER_DEFAULT;
  const mutationError = mutationErrorMessage(update.error);

  const onChange = (value: string) => {
    update.mutate({ default_model_id: value === SERVER_DEFAULT ? null : value });
  };

  return (
    <div className="lc-aps__section">
      <div className="lc-aps__label">Default model</div>
      <label className="flex min-w-0 items-center">
        <span className="sr-only">Default model</span>
        <select
          aria-label="Default model"
          className="lc-modelpicker"
          value={selected}
          disabled={update.isPending}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value={SERVER_DEFAULT}>
            {serverDefault ? `Use server default (${serverDefault.label})` : 'Use server default'}
          </option>
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
      <p className="mt-1 text-xs opacity-70">Used for new chats · saved to your account.</p>
      {staleId ? (
        <p role="status" className="mt-1 text-xs" style={{ color: 'var(--danger, #b42318)' }}>
          Your saved model is no longer available — the server default is used until you choose a
          new one.
        </p>
      ) : null}
      {mutationError ? (
        <p role="alert" className="mt-1 text-xs" style={{ color: 'var(--danger, #b42318)' }}>
          {mutationError}
        </p>
      ) : null}
    </div>
  );
}
