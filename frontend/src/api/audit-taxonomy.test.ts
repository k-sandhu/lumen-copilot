/**
 * The hand-authored `AuditEventType` union must match the contract (#545).
 *
 * `src/api/types.ts` is a hand-authored mirror of `contracts/openapi.yaml`, kept
 * so the app type-checks before `pnpm gen:api` has produced the (gitignored)
 * generated schema. A hand-authored mirror drifts silently: this one had 14 of the
 * 84 declared actions, so most of the taxonomy was untypeable in the frontend and
 * unfilterable through `GET /audit?type=`, with nothing failing to say so.
 *
 * Reading the YAML directly is the point — comparing the mirror against itself, or
 * against the generated file, would prove nothing about the contract.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = dirname(fileURLToPath(import.meta.url));
const CONTRACT = resolve(HERE, '../../../contracts/openapi.yaml');
const TYPES = resolve(HERE, 'types.ts');

/**
 * Pull the `AuditEventType` enum out of the contract without a YAML dependency:
 * the block is a flat list of `- value` lines under `enum:`, ending at the next
 * key at the same indent.
 */
function contractActions(): string[] {
  const yaml = readFileSync(CONTRACT, 'utf8');
  const start = yaml.indexOf('    AuditEventType:');
  expect(start, 'AuditEventType is missing from the contract').toBeGreaterThan(-1);
  const block = yaml.slice(start);
  const enumAt = block.indexOf('      enum:');
  const lines = block.slice(enumAt).split('\n').slice(1);
  const out: string[] = [];
  for (const line of lines) {
    const match = /^\s+- (\S+)\s*$/.exec(line);
    const value = match?.[1];
    if (value === undefined) break;
    out.push(value);
  }
  return out;
}

/** The string literals of the `AuditEventType` union in the hand-authored mirror. */
function mirrorActions(): string[] {
  const source = readFileSync(TYPES, 'utf8');
  const start = source.indexOf('export type AuditEventType =');
  expect(start, 'AuditEventType is missing from types.ts').toBeGreaterThan(-1);
  const block = source.slice(start, source.indexOf(';', start));
  return [...block.matchAll(/'([^']+)'/g)]
    .map((m) => m[1])
    .filter((v): v is string => v !== undefined);
}

describe('AuditEventType stays in lockstep with the contract', () => {
  it('declares every action the contract does, and no others', () => {
    const contract = contractActions();
    const mirror = mirrorActions();

    // Sanity: a parser that silently returned nothing would make this test vacuous
    // and it would pass forever.
    expect(contract.length).toBeGreaterThan(20);

    expect([...mirror].sort()).toEqual([...contract].sort());
  });
});
