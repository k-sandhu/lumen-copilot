/**
 * Local type surface for the Sources slice — re-exports the FROZEN wire types from
 * the api/ boundary (never re-declared here; frontend/AGENTS.md "don't re-declare
 * wire shapes") plus the kit's StatusTone, so presentation helpers and components
 * import their types from one slice-local module.
 */
export type {
  Source,
  WebSource,
  GdriveSource,
  SourceList,
  SourceCreate,
  WebSourceCreate,
  GdriveSourceCreate,
  SourceStatus,
  SourceType,
  WebSourceMode,
  SourceConfig,
  WebSourceConfig,
  GdriveSourceConfig,
  GdriveMyDriveConfig,
  GdriveFolderConfig,
  GdriveSharedDriveConfig,
  ConnectedAccount,
  SourceConnectResponse,
  ConnectErrorReason,
} from '@/api';
export type { StatusTone } from '@/ui';
