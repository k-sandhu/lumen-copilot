/**
 * WS connection indicator — demonstrates the streaming-UX bar on the skeleton:
 * surfaces connecting / open / closed and the latest ping `seq`. Every state is
 * represented (no perpetual spinner): connecting pulses, open is steady, closed
 * is an actionable-looking warning the reconnect-with-backoff is already retrying.
 */
import { useHealthStream } from '../hooks/useHealthStream';
import { StatusBadge } from '@/components/StatusBadge';
import type { StatusTone } from '@/components/StatusBadge';
import type { WsConnectionState } from '@/api';

const labelByState: Record<WsConnectionState, string> = {
  connecting: 'Connecting',
  open: 'Live',
  closing: 'Closing',
  closed: 'Reconnecting',
};

const toneByState: Record<WsConnectionState, StatusTone> = {
  connecting: 'pending',
  open: 'ok',
  closing: 'pending',
  closed: 'degraded',
};

export function WsStatusIndicator() {
  const { connection, lastSeq, lastType } = useHealthStream();
  const pulse = connection === 'connecting' || connection === 'closed';

  return (
    <div className="flex items-center gap-2" aria-live="polite">
      <span className="text-xs text-foreground-muted">Realtime</span>
      <StatusBadge
        tone={toneByState[connection]}
        pulse={pulse}
        detail={
          connection === 'closed'
            ? 'Disconnected — retrying with backoff.'
            : `WebSocket ${connection}`
        }
      >
        {labelByState[connection]}
      </StatusBadge>
      {lastSeq !== null && (
        <span className="font-mono text-xs text-foreground-muted">
          {lastType ?? 'msg'} #{lastSeq}
        </span>
      )}
    </div>
  );
}
