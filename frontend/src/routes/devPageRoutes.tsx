import { lazy, Suspense } from 'react';

/**
 * Lazy-loaded route components for the standalone developer pages. Kept separate
 * from router.tsx so that module exports only the (non-component) `router` — which
 * keeps react-refresh happy and keeps the docs/catalog bundles out of the main
 * app chunk until visited.
 */
const DocsPage = lazy(() => import('@/features/docs').then((m) => ({ default: m.DocsPage })));
const FeaturesPage = lazy(() =>
  import('@/features/feature-catalog').then((m) => ({ default: m.FeaturesPage })),
);

function RouteFallback({ label }: { label: string }) {
  return (
    <div className="flex h-screen items-center justify-center bg-surface text-sm text-foreground-muted">
      Loading {label}…
    </div>
  );
}

/** `/docs/*` — standalone documentation viewer. */
export function DocsRoute() {
  return (
    <Suspense fallback={<RouteFallback label="documentation" />}>
      <DocsPage />
    </Suspense>
  );
}

/** `/features` — standalone features catalog. */
export function FeaturesRoute() {
  return (
    <Suspense fallback={<RouteFallback label="features" />}>
      <FeaturesPage />
    </Suspense>
  );
}
