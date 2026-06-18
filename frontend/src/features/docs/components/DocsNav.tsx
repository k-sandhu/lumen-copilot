import { Link } from 'react-router-dom';
import { ScrollArea } from '@/components/ScrollArea';
import { cn } from '@/lib/cn';
import { groupDocs, type DocEntry } from '../model';

interface DocsNavProps {
  entries: DocEntry[];
  currentSlug: string;
}

/** Grouped, keyboard-navigable list of every bundled doc. Independently scrollable. */
export function DocsNav({ entries, currentSlug }: DocsNavProps) {
  const groups = groupDocs(entries);

  return (
    <nav aria-label="Documentation" className="h-full">
      <ScrollArea viewportClassName="p-3">
        <ul className="space-y-4">
          {groups.map((group) => (
            <li key={group.key}>
              <h2 className="px-2 pb-1 text-xs font-semibold uppercase tracking-wide text-foreground-muted">
                {group.label}
              </h2>
              <ul className="space-y-0.5">
                {group.entries.map((entry) => {
                  const active = entry.slug === currentSlug;
                  return (
                    <li key={entry.slug}>
                      <Link
                        to={`/docs/${entry.slug}`}
                        aria-current={active ? 'page' : undefined}
                        title={entry.title}
                        className={cn(
                          'block truncate rounded-md px-2 py-1.5 text-sm',
                          active
                            ? 'bg-accent/15 font-medium text-accent'
                            : 'text-foreground hover:bg-surface-muted',
                        )}
                      >
                        {entry.title}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </li>
          ))}
        </ul>
      </ScrollArea>
    </nav>
  );
}
