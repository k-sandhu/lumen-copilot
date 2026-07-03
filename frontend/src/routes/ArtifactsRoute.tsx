/**
 * `/artifacts` screen element (#222) — the artifact panel (list / preview /
 * download / delete of agent-produced files). The app shell (chrome) + auth gate
 * live in the LAYOUT route (`routes/router.tsx`), so this just renders the panel
 * into the shell's main `<Outlet/>`.
 *
 * Reads the `?artifact=<id>` deep-link param the code-run inspector chips point at
 * (features/artifacts/model/href.ts), so opening one selects that file on load.
 * `min-h-0` lets the panel own its independent scroll regions inside the shell.
 */
import { useSearchParams } from 'react-router-dom';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { ArtifactPanel, ARTIFACT_PARAM } from '@/features/artifacts';

export function ArtifactsRoute() {
  const [params] = useSearchParams();
  const initial = params.get(ARTIFACT_PARAM) ?? undefined;
  return (
    <div className="min-h-0 flex-1 bg-surface p-4 text-foreground">
      <ErrorBoundary label="Artifacts">
        <ArtifactPanel {...(initial ? { initialArtifactId: initial } : {})} />
      </ErrorBoundary>
    </div>
  );
}
