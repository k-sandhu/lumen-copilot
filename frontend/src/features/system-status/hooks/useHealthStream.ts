/**
 * React hook wrapping the typed WsClient (api/ boundary). Features consume THIS,
 * never the raw socket (frontend/AGENTS.md). Connects to VITE_WS_BASE_URL +
 * '/health', tracks connection lifecycle, and surfaces the latest envelope's
 * `seq` (the "ping seq" the status indicator shows).
 *
 * The connection is opened on mount and CLOSED on unmount — which also cancels
 * any pending reconnect (quality bar: cancellable / no leaks).
 */
import { useEffect, useRef, useState } from 'react';
import { WsClient } from '@/api';
import type { WsConnectionState, WsEnvelope } from '@/api';

export interface HealthStreamState {
  connection: WsConnectionState;
  /** seq of the most recently received envelope, or null before any arrive. */
  lastSeq: number | null;
  /** type of the most recently received envelope, for display. */
  lastType: WsEnvelope['type'] | null;
}

export function useHealthStream(): HealthStreamState {
  const [state, setState] = useState<HealthStreamState>({
    connection: 'connecting',
    lastSeq: null,
    lastType: null,
  });
  const clientRef = useRef<WsClient | null>(null);

  useEffect(() => {
    const client = new WsClient({
      path: '/health',
      onStateChange: (connection) => setState((s) => ({ ...s, connection })),
      onEnvelope: (envelope) =>
        setState((s) => ({ ...s, lastSeq: envelope.seq, lastType: envelope.type })),
    });
    clientRef.current = client;
    client.connect();

    return () => {
      client.close();
      clientRef.current = null;
    };
  }, []);

  return state;
}
