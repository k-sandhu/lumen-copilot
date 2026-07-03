/**
 * The code-run view-model — collapsing the live-stream activity and the fetched
 * record into one shape, and merging them so the inline panel is live during the
 * run then settles on the durable record (#232).
 */
import { describe, it, expect } from 'vitest';
import type { CodeRun } from '@/api';
import type { CodeRunActivity } from '@/features/chat/model/streamReducer';
import { mergeView, viewFromActivity, viewFromRecord } from './view';

const ACTIVITY: CodeRunActivity = {
  runId: 'run-1',
  callId: 'call-1',
  status: 'running',
  stdout: 'live stdout\n',
  stderr: '',
  artifactIds: [],
};

const RECORD: CodeRun = {
  id: 'run-1',
  status: 'running',
  code: 'print("hi")',
  stdout: '',
  stderr: '',
  artifact_ids: [],
  created_at: '2026-07-02T00:00:00Z',
  image_digest: 'sha256:abc',
};

describe('viewFromActivity', () => {
  it('has no code (not on the stream) and is streaming while running', () => {
    const v = viewFromActivity(ACTIVITY);
    expect(v.code).toBeNull();
    expect(v.stdout).toBe('live stdout\n');
    expect(v.streaming).toBe(true);
  });
});

describe('viewFromRecord', () => {
  it('carries the durable code + digest and maps snake_case fields', () => {
    const v = viewFromRecord({ ...RECORD, status: 'succeeded', exit_code: 0, duration_ms: 90 });
    expect(v.code).toBe('print("hi")');
    expect(v.imageDigest).toBe('sha256:abc');
    expect(v.exitCode).toBe(0);
    expect(v.durationMs).toBe(90);
    expect(v.streaming).toBe(false);
  });
});

describe('mergeView', () => {
  it('while the record is non-terminal, live output leads but the code comes from the record', () => {
    const v = mergeView(ACTIVITY, RECORD);
    expect(v.code).toBe('print("hi")'); // durable field from the record
    expect(v.stdout).toBe('live stdout\n'); // live stream is ahead
    expect(v.streaming).toBe(true);
  });

  it('a live terminal status wins while the record still lags behind', () => {
    const live: CodeRunActivity = {
      ...ACTIVITY,
      status: 'succeeded',
      exitCode: 0,
      durationMs: 100,
      artifactIds: ['art-1'],
    };
    const v = mergeView(live, RECORD); // record still says running
    expect(v.status).toBe('succeeded');
    expect(v.exitCode).toBe(0);
    expect(v.artifactIds).toEqual(['art-1']);
    expect(v.streaming).toBe(false);
  });

  it('once the record is terminal it is authoritative (output-capped truth)', () => {
    const record: CodeRun = {
      ...RECORD,
      status: 'succeeded',
      stdout: 'final stdout\n',
      exit_code: 0,
      duration_ms: 90,
      resource_usage: { peak_memory_bytes: 2048 },
    };
    const v = mergeView(ACTIVITY, record);
    expect(v.status).toBe('succeeded');
    expect(v.stdout).toBe('final stdout\n');
    expect(v.resourceUsage).toEqual({ peak_memory_bytes: 2048 });
    expect(v.streaming).toBe(false);
  });
});
