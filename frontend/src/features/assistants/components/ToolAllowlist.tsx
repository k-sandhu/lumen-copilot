/**
 * ToolAllowlist (#212) — a checkbox list of the tools an assistant's runs may use
 * (persisted into `toolAllowlist: string[]`). Driven by the STATIC known-tools
 * stub (`model/tools.ts`) — see the TODO there: swap to the tool-registry endpoint
 * when F-ADMIN-TOOLS (#223) / CC-A (#207) land. An empty list means "the default
 * retrieval tool set" per the contract, so leaving everything unticked is valid.
 *
 * A11y: a real <fieldset>/<legend> with native checkboxes — fully keyboard- and
 * screen-reader-navigable, each labelled and described by its summary.
 */
import { useId } from 'react';
import { KNOWN_TOOLS } from '../model/tools';

interface ToolAllowlistProps {
  /** Currently-allowed tool names. */
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}

export function ToolAllowlist({ value, onChange, disabled = false }: ToolAllowlistProps) {
  const groupId = useId();
  const selected = new Set(value);

  const toggle = (name: string) => {
    const next = new Set(selected);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    onChange([...next]);
  };

  return (
    <fieldset disabled={disabled} className="min-w-0">
      <legend className="text-sm font-medium">Tools</legend>
      <p className="mt-0.5 text-xs text-foreground-muted">
        Which tools this assistant’s runs may use. Leaving all unticked uses the default retrieval
        tools.
      </p>
      {/* TODO(F-ADMIN-TOOLS #223): source these from the tool-registry endpoint. */}
      <ul className="mt-2 space-y-2" aria-label="Allowed tools">
        {KNOWN_TOOLS.map((tool) => {
          const id = `${groupId}-${tool.name}`;
          return (
            <li key={tool.name}>
              <label htmlFor={id} className="flex cursor-pointer items-start gap-2">
                <input
                  id={id}
                  type="checkbox"
                  checked={selected.has(tool.name)}
                  onChange={() => toggle(tool.name)}
                  className="mt-0.5 h-4 w-4 shrink-0 accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                />
                <span className="min-w-0">
                  <span className="block text-sm">{tool.label}</span>
                  <span className="block text-xs text-foreground-muted">{tool.description}</span>
                </span>
              </label>
            </li>
          );
        })}
      </ul>
    </fieldset>
  );
}
