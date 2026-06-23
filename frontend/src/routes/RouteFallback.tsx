/**
 * Shared Suspense fallback for lazy-loaded feature routes. Each feature's
 * `route.tsx` wraps its lazy page in `<Suspense fallback={<RouteFallback … />}>`
 * so the standalone pages keep their out-of-main-chunk lazy loading (ADR-0008 §3
 * preserves the existing route behavior).
 */
export function RouteFallback({ label }: { label: string }) {
  return (
    <div className="flex h-screen items-center justify-center bg-surface text-sm text-foreground-muted">
      Loading {label}…
    </div>
  );
}
