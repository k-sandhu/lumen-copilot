/**
 * AuditRow — one line in the audit log (DESIGN.md §1 "auditable", §6 "audit row
 * + provenance drawer"). Every retrieval / answer / access decision emits an
 * event; this row is the entry point. Clicking it opens the ProvenanceDrawer
 * with the full proof. It is a real <button> so it is keyboard-operable.
 */
import { cn } from '@/lib/cn';
import { Icon, type IconName } from './Icon';

export type AuditKind = 'retrieval' | 'answer' | 'access' | 'action';

export interface AuditEvent {
  /** Short event id, e.g. "evt_9f3a". */
  id: string;
  kind: AuditKind;
  /** Human action summary, e.g. "Answered: Q3 revenue by region". */
  action: string;
  /** Secondary line, e.g. "dana@acme · Assistant". */
  detail?: string;
  /** Display timestamp, e.g. "14:02:11". */
  time?: string;
}

const KIND_ICON: Record<AuditKind, IconName> = {
  retrieval: 'search',
  answer: 'message-square',
  access: 'lock',
  action: 'corner-down-left',
};

interface AuditRowProps {
  event: AuditEvent;
  onSelect?: (event: AuditEvent) => void;
  className?: string;
}

export function AuditRow({ event, onSelect, className }: AuditRowProps) {
  return (
    <button
      type="button"
      className={cn('lc-audit', className)}
      onClick={() => onSelect?.(event)}
      aria-label={`Audit event ${event.id}: ${event.action}`}
    >
      <Icon name={KIND_ICON[event.kind]} />
      <span className="lc-audit__id lc-mono">{event.id}</span>
      <span className="lc-audit__main">
        <span className="lc-audit__action">{event.action}</span>
        {event.detail ? <span className="lc-audit__sub">{event.detail}</span> : null}
      </span>
      {event.time ? <span className="lc-audit__time">{event.time}</span> : null}
    </button>
  );
}
