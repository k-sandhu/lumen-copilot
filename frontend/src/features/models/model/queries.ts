/**
 * Server-state hook for the curated chat-model registry (GET /models) — TanStack
 * Query, one cached copy shared by every consumer.
 *
 * WHY ITS OWN SLICE (FE-9 / #494 AC-5). The registry is genuinely cross-feature:
 * chat's per-turn picker, the preferences default-model control, and the
 * assistants editor all read it. It used to live in `features/chat/model/queries`
 * and be re-exported from the chat barrel, so `preferences/DefaultModelSetting`
 * — which the app shell renders eagerly — imported `@/features/chat` just to get
 * this one hook. That dragged `ChatView → MessageBubble → lib/markdown` into the
 * app-shell (entry) module graph AND closed a barrel cycle back through
 * `ChatView → @/features/preferences`, which Rollup reported as
 * CIRCULAR_DEPENDENCY / CYCLIC_CROSS_CHUNK_REEXPORT and which made the entry
 * chunk statically import the whole `markdown` chunk on first paint.
 *
 * Owning the registry here keeps it barrel-importable by all three slices with no
 * slice depending on another. `chat-pipeline-not-in-entry.test.ts` fails if the
 * cycle comes back.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { listModels } from '@/api';
import type { ModelList } from '@/api';

/** Cache key for the registry — one entry, shared by every consumer. */
export const modelsKey = ['models'] as const;

/** The model-picker registry. Stable for the session; cache generously. */
export function useModels(): UseQueryResult<ModelList> {
  return useQuery<ModelList>({
    queryKey: modelsKey,
    queryFn: ({ signal }) => listModels(signal),
    staleTime: 5 * 60_000,
  });
}
