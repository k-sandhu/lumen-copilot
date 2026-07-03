/**
 * The normalized code-run view-model the inspector renders (#232). A code run is
 * inspectable from two sources — the LIVE chat stream (a `CodeRunActivity` folded
 * from code_output/code_result, which has streamed stdout/stderr + the terminal
 * outcome but not the full record), and the READ endpoint (a `CodeRun` with code,
 * resource usage, image digest, timestamps). This module collapses both into one
 * shape so the presentational `CodeRunInspector` has a single contract, and merges
 * them when both are present (the fetched record is authoritative for the code +
 * usage; the live stream can be ahead for still-arriving output).
 */
import type { CodeRun, CodeRunStatus, ResourceUsage } from '@/api';
import type { CodeRunActivity } from '@/features/chat/model/streamReducer';

export interface CodeRunView {
  runId: string;
  status: CodeRunStatus;
  /** The exact source executed — present once the read endpoint has loaded. */
  code: string | null;
  stdout: string;
  stderr: string;
  exitCode: number | null;
  durationMs: number | null;
  resourceUsage: ResourceUsage | null;
  artifactIds: string[];
  imageDigest: string | null;
  createdAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  /** True while output may still be arriving on the live stream (drives the caret). */
  streaming: boolean;
}

/** Build a view from the live-stream activity alone (before the record loads). */
export function viewFromActivity(activity: CodeRunActivity): CodeRunView {
  return {
    runId: activity.runId,
    status: activity.status,
    code: null,
    stdout: activity.stdout,
    stderr: activity.stderr,
    exitCode: activity.exitCode ?? null,
    durationMs: activity.durationMs ?? null,
    resourceUsage: null,
    artifactIds: activity.artifactIds,
    imageDigest: null,
    createdAt: null,
    startedAt: null,
    finishedAt: null,
    streaming: activity.status === 'running' || activity.status === 'queued',
  };
}

/** Build a view from the fetched full record alone (after-the-fact inspection). */
export function viewFromRecord(run: CodeRun): CodeRunView {
  return {
    runId: run.id,
    status: run.status,
    code: run.code,
    stdout: run.stdout,
    stderr: run.stderr,
    exitCode: run.exit_code ?? null,
    durationMs: run.duration_ms ?? null,
    resourceUsage: run.resource_usage ?? null,
    artifactIds: run.artifact_ids,
    imageDigest: run.image_digest ?? null,
    createdAt: run.created_at,
    startedAt: run.started_at ?? null,
    finishedAt: run.finished_at ?? null,
    streaming: run.status === 'running' || run.status === 'queued',
  };
}

/**
 * Merge the live-stream activity with the fetched record. The record is
 * authoritative for the durable fields (code, resource usage, image digest,
 * timestamps). For the streamed fields (status, stdout/stderr, exit/duration,
 * artifacts) we prefer whichever side is FURTHER ALONG:
 *  - once the record is terminal, it wins (the persisted, output-capped truth);
 *  - while the record is still non-terminal, the live stream can be ahead
 *    (output already flowing / a code_result already seen), so we take the live
 *    output and — if the live side has finalized — its terminal status/exit.
 * This keeps the inline panel live during the run, then settles on the durable
 * record, with no flicker between `code_result` and the read-endpoint refetch.
 */
export function mergeView(activity: CodeRunActivity, run: CodeRun): CodeRunView {
  const record = viewFromRecord(run);
  const recordTerminal =
    run.status !== 'queued' && run.status !== 'running';
  if (recordTerminal) {
    // Prefer the record's captured output unless it is empty and the live stream
    // has some (a race where the result landed before the record backfilled).
    return {
      ...record,
      stdout: record.stdout || activity.stdout,
      stderr: record.stderr || activity.stderr,
      streaming: false,
    };
  }
  const liveTerminal =
    activity.status !== 'queued' && activity.status !== 'running';
  return {
    ...record,
    status: liveTerminal ? activity.status : record.status,
    stdout: activity.stdout || record.stdout,
    stderr: activity.stderr || record.stderr,
    exitCode: activity.exitCode ?? record.exitCode,
    durationMs: activity.durationMs ?? record.durationMs,
    artifactIds: activity.artifactIds.length ? activity.artifactIds : record.artifactIds,
    streaming: !liveTerminal,
  };
}
