/**
 * Typed code-run calls — part of the api/ boundary (ADR-0004). The ONLY place the
 * SPA performs code-run HTTP. Conforms to the FROZEN contract (contracts/openapi.yaml
 * §code-runs, ADR-0013 / #229):
 *
 *   GET /code-runs/{codeRunId}   → CodeRun
 *
 * Read-only inspection of one sandbox code run: the exact `code` executed, its
 * `status`, captured `stdout`/`stderr` (output-size-capped, G7), `exit_code`,
 * `duration_ms`, `resource_usage`, and the ids of any artifacts it emitted
 * (`artifact_ids`, CC-B #208). Runs are agent-mediated (the `run_python` tool) —
 * there is deliberately no public "execute arbitrary code" endpoint.
 *
 * Tenant- and owner-scoped (spec 0004 INV-1/INV-2): a run id in another tenant —
 * or one the caller does not own — surfaces as a typed 404 ApiError (existence
 * non-disclosure), never 403. A malformed (non-uuid) id is 422 (INV-8); a
 * missing/expired token is 401 (INV-4).
 */
import { request } from './client';
import type { CodeRun } from './types';

/**
 * Inspect one code run — status, code, stdout/stderr, timing, resource usage, and
 * artifact ids. A non-owned / cross-tenant / unknown id → 404 (INV-1/INV-2).
 */
export function getCodeRun(id: string, signal?: AbortSignal): Promise<CodeRun> {
  return request<CodeRun>(`/code-runs/${id}`, { signal });
}
