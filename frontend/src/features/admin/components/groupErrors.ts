/**
 * Human copy for a failed group WRITE (#540, ADR-0022). PanelState's own
 * `describeError` covers READ failures ("Couldn't load this panel"); a write
 * that fails needs its own sentence next to the control that failed, so an
 * admin learns what to do rather than watching the pane change.
 *
 * The 409s are the interesting ones: the service distinguishes three conflicts
 * by RFC-9457 `code`, and status alone cannot tell them apart.
 */
import { ApiError } from '@/api';

/** The tenant's derived group is managed by the server, not by an admin. */
const SYSTEM_IMMUTABLE = 'The tenant-wide group is managed automatically and can’t be changed.';

export function describeGroupWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'You need the admin role to manage groups.';
    if (error.status === 401) return 'Your session has expired. Sign in again.';
    if (error.status === 404) return 'That group no longer exists.';
    if (error.status === 409) {
      if (error.problem?.code === 'group_name_taken') {
        return 'A group with that name already exists.';
      }
      if (error.problem?.code === 'group_name_reserved') {
        return '“All members” is reserved for the tenant-wide group.';
      }
      if (error.problem?.code === 'system_group_immutable') return SYSTEM_IMMUTABLE;
    }
    if (error.status === 422) return 'Enter a group name between 1 and 120 characters.';
    return error.displayMessage;
  }
  if (error instanceof Error) return error.message;
  return 'Something went wrong. Please try again.';
}

/** Same mapping, but the 404 means the *user* — a member write names two things. */
export function describeMemberWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return 'That group or user no longer exists in this tenant.';
    if (error.status === 409 && error.problem?.code === 'system_group_immutable') {
      return 'Everyone in the tenant is already in the tenant-wide group.';
    }
  }
  return describeGroupWriteError(error);
}
