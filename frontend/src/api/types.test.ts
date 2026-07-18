/**
 * COMPILE-TIME contract assertions for the hand-authored wire mirror (#455,
 * ADR-0019 §5). The gdrive config variants are CLOSED shapes on the wire
 * (`additionalProperties: false`; a violating config is 422 `invalid_config`,
 * INV-8). TypeScript is structurally typed, so without `?: never` members a
 * carried forbidden id would still assign to the union — each `@ts-expect-error`
 * below FAILS THE TYPECHECK if one of the forbidden mode/id combinations ever
 * becomes assignable again. `tsc --noEmit` (the typecheck gate) enforces these;
 * the runtime test only keeps the file in the vitest suite.
 */
import { describe, it, expect } from 'vitest';
import type { GdriveSourceConfig, SourceCreate } from './types';

describe('gdrive config — the closed mode-discriminated variants (compile-time)', () => {
  it('accepts exactly the contract shapes and rejects every forbidden mode/id combo', () => {
    // Valid variants (positive controls — these must compile).
    const myDrive: GdriveSourceConfig = { mode: 'my_drive' };
    const folder: GdriveSourceConfig = { mode: 'folder', folder_id: 'f-1' };
    const folderInDrive: GdriveSourceConfig = {
      mode: 'folder',
      folder_id: 'f-1',
      drive_id: 'd-9',
    };
    const sharedDrive: GdriveSourceConfig = { mode: 'shared_drive', drive_id: 'd-9' };

    // @ts-expect-error — my_drive takes NO folder_id (closed variant).
    const badMyDriveFolder: GdriveSourceConfig = { mode: 'my_drive', folder_id: 'x' };
    // @ts-expect-error — my_drive takes NO drive_id (closed variant).
    const badMyDriveDrive: GdriveSourceConfig = { mode: 'my_drive', drive_id: 'x' };
    // @ts-expect-error — shared_drive takes NO folder_id (closed variant).
    const badShared: GdriveSourceConfig = {
      mode: 'shared_drive',
      drive_id: 'd-9',
      folder_id: 'x',
    };
    // @ts-expect-error — folder REQUIRES folder_id.
    const badFolder: GdriveSourceConfig = { mode: 'folder' };
    // @ts-expect-error — shared_drive REQUIRES drive_id.
    const badSharedMissing: GdriveSourceConfig = { mode: 'shared_drive' };

    // The request-builder boundary inherits the same closure via SourceCreate.
    const create: SourceCreate = { type: 'gdrive', config: sharedDrive };
    const badCreate: SourceCreate = {
      type: 'gdrive',
      // @ts-expect-error — a forbidden combo cannot ride through SourceCreate either.
      config: { mode: 'my_drive', drive_id: 'x' },
    };

    // Runtime no-op: reference every binding so the compile-time fixtures are
    // used (the assertions above are the test).
    expect([
      myDrive,
      folder,
      folderInDrive,
      sharedDrive,
      badMyDriveFolder,
      badMyDriveDrive,
      badShared,
      badFolder,
      badSharedMissing,
      create,
      badCreate,
    ]).toHaveLength(11);
  });
});
