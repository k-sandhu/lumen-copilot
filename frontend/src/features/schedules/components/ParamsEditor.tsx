/**
 * ParamsEditor (#237) — edits the assistant's `input_params` for each fire as
 * key/value rows (the wire shape is a free-form JSON object; the UI keeps it
 * simple string key/value pairs, which cover the prompt-template values the
 * scheduled assistant expects). Add/remove rows; empty keys are dropped on submit
 * by the form mapper.
 */
import { Icon } from '@/ui';
import type { ParamRow } from '../model/form';

interface ParamsEditorProps {
  rows: ParamRow[];
  onChange: (rows: ParamRow[]) => void;
  disabled?: boolean;
}

export function ParamsEditor({ rows, onChange, disabled }: ParamsEditorProps) {
  const setRow = (index: number, patch: Partial<ParamRow>) =>
    onChange(rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  const addRow = () => onChange([...rows, { key: '', value: '' }]);
  const removeRow = (index: number) => onChange(rows.filter((_, i) => i !== index));

  return (
    <div className="space-y-2">
      {rows.length === 0 ? (
        <p className="text-xs text-foreground-muted">
          No inputs — the assistant runs with its defaults. Add a value to override one.
        </p>
      ) : (
        <ul className="space-y-2">
          {rows.map((row, i) => (
            <li key={i} className="flex flex-wrap items-center gap-2">
              <input
                type="text"
                aria-label={`Input ${i + 1} key`}
                value={row.key}
                disabled={disabled}
                onChange={(e) => setRow(i, { key: e.target.value })}
                placeholder="key"
                className="w-40 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
              />
              <input
                type="text"
                aria-label={`Input ${i + 1} value`}
                value={row.value}
                disabled={disabled}
                onChange={(e) => setRow(i, { value: e.target.value })}
                placeholder="value"
                className="min-w-0 flex-1 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
              />
              <button
                type="button"
                onClick={() => removeRow(i)}
                disabled={disabled}
                aria-label={`Remove input ${i + 1}`}
                className="rounded-md border border-border p-1.5 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
              >
                <Icon name="trash" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        onClick={addRow}
        disabled={disabled}
        className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
      >
        <Icon name="plus" className="shrink-0" />
        Add input
      </button>
    </div>
  );
}
