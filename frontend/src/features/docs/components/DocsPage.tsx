import { useCallback, useMemo, useState } from 'react';
import { Link, Navigate, useParams } from 'react-router-dom';
import { MarkdownView } from '@/lib/markdown';
import { ScrollArea } from '@/components/ScrollArea';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { cn } from '@/lib/cn';
import { DOCS } from '../content';
import { buildPathIndex, resolveDocLink } from '../model';
import { DocsNav } from './DocsNav';

/** Canonical landing doc: the root README if present, else the first entry. */
function defaultSlug(): string {
  return DOCS.find((d) => d.slug === 'README')?.slug ?? DOCS[0]?.slug ?? '';
}

const featuresLink = (
  <Link
    to="/features"
    className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted"
  >
    ✨ Features
  </Link>
);

/**
 * `/docs/*` — standalone documentation viewer. Bundles every repo doc (see
 * ../content) and renders the selected one through the shared sanitized markdown
 * pipeline, with doc-to-doc links routed in-app. Covers empty / not-found /
 * success; nav and content panes scroll independently (frontend/AGENTS.md).
 */
export function DocsPage() {
  const splat = useParams()['*'] ?? '';
  const [navOpen, setNavOpen] = useState(false);

  const pathIndex = useMemo(() => buildPathIndex(DOCS), []);
  const entry = useMemo(() => DOCS.find((d) => d.slug === splat), [splat]);
  const currentPath = entry?.path ?? '';
  const resolver = useCallback(
    (href: string) => resolveDocLink(currentPath, href, pathIndex),
    [currentPath, pathIndex],
  );

  // Empty — nothing bundled (shouldn't happen, but never leave a blank pane).
  if (DOCS.length === 0) {
    return (
      <PageChrome title="Documentation" actions={featuresLink}>
        <div className="p-8 text-sm text-foreground-muted">
          No documentation found. Markdown under <code>docs/</code> is bundled at build time.
        </div>
      </PageChrome>
    );
  }

  // Index → redirect to the canonical landing doc so URLs are always deep-linkable.
  if (splat === '') {
    return <Navigate to={`/docs/${defaultSlug()}`} replace />;
  }

  return (
    <PageChrome
      title="Documentation"
      actions={featuresLink}
      onToggleNav={() => setNavOpen((v) => !v)}
      navOpen={navOpen}
    >
      <div className="flex min-h-0 flex-1">
        <aside
          className={cn(
            'min-h-0 shrink-0 border-r border-border md:block md:w-72',
            navOpen ? 'block w-full' : 'hidden',
          )}
        >
          <DocsNav entries={DOCS} currentSlug={splat} />
        </aside>

        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          {entry ? (
            <ScrollArea viewportClassName="px-6 py-6">
              <article className="mx-auto max-w-3xl">
                <ErrorBoundary label="Document">
                  <MarkdownView resolveInternalLink={resolver}>{entry.content}</MarkdownView>
                </ErrorBoundary>
              </article>
            </ScrollArea>
          ) : (
            <NotFound slug={splat} />
          )}
        </main>
      </div>
    </PageChrome>
  );
}

function NotFound({ slug }: { slug: string }) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center p-8">
      <div
        role="alert"
        className="max-w-md rounded-md border border-border bg-surface-muted p-6 text-center"
      >
        <p className="text-sm font-semibold">Document not found</p>
        <p className="mt-1 text-sm text-foreground-muted">
          No document matches <code className="rounded bg-surface px-1">{slug}</code>.
        </p>
        <Link
          to="/docs"
          className="mt-3 inline-block text-sm text-accent underline underline-offset-2"
        >
          Back to documentation
        </Link>
      </div>
    </div>
  );
}
