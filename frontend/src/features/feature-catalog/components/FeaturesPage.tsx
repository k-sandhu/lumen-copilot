import { Link } from 'react-router-dom';
import { ScrollArea } from '@/components/ScrollArea';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { StatusBadge } from '@/components/StatusBadge';
import { CATALOG, groupByArea } from '../catalog';
import { FeatureCard } from './FeatureCard';

const docsLink = (
  <Link
    to="/docs"
    className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted"
  >
    📖 Docs
  </Link>
);

/**
 * `/features` — standalone catalog of what has shipped (../catalog), grouped by
 * area as status-badged cards. Curated + grounded: each entry links to the ADR/PR
 * it came from. Separate from the chat shell, like the docs viewer.
 */
export function FeaturesPage() {
  const groups = groupByArea(CATALOG);

  return (
    <PageChrome title="Features built" actions={docsLink}>
      <ScrollArea viewportClassName="px-6 py-6">
        <div className="mx-auto max-w-5xl space-y-8">
          <header className="space-y-3">
            <p className="text-sm text-foreground-muted">
              A grounded snapshot of what&apos;s been built so far — each entry links to the ADR,
              spec or PR it came from. Updated as features land.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge tone="ok">Shipped</StatusBadge>
              <StatusBadge tone="degraded" pulse>
                In progress
              </StatusBadge>
              <StatusBadge tone="pending">Planned</StatusBadge>
            </div>
          </header>

          {groups.length === 0 ? (
            <p className="text-sm text-foreground-muted">No features catalogued yet.</p>
          ) : (
            groups.map((group) => (
              <section key={group.area} aria-label={group.area}>
                <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-foreground-muted">
                  {group.area}
                </h2>
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {group.entries.map((entry) => (
                    <ErrorBoundary key={entry.id} label={entry.title}>
                      <FeatureCard entry={entry} />
                    </ErrorBoundary>
                  ))}
                </div>
              </section>
            ))
          )}
        </div>
      </ScrollArea>
    </PageChrome>
  );
}
