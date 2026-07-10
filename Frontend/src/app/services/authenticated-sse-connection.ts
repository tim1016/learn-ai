import { withDataPlaneControlIntent } from './broker-sse';

export type AuthenticatedSseStatus = 'connecting' | 'open' | 'error' | 'closed';

export interface AuthenticatedSseHandlers {
  readonly onStatus: (status: AuthenticatedSseStatus) => void;
  readonly onEvent: (event: MessageEvent<string>) => void;
  readonly onControlEvent?: (name: string, event: MessageEvent<string>) => void;
  readonly onError?: (message: string | null) => void;
}

export interface AuthenticatedSseConnection {
  readonly close: () => void;
}

export interface AuthenticatedSseOptions {
  readonly reconnect?: boolean;
}

/** Own the authenticated native EventSource lifecycle and reconnect status. */
export function openAuthenticatedSseConnection(
  url: string,
  eventName: string,
  handlers: AuthenticatedSseHandlers,
  controlEventNames: readonly string[] = [],
  options: AuthenticatedSseOptions = {},
): AuthenticatedSseConnection {
  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectDelayMs = 500;
  let closed = false;

  const connect = (): void => {
    if (closed) return;
    handlers.onStatus('connecting');
    const next = new EventSource(withDataPlaneControlIntent(url));
    source = next;
    next.addEventListener('open', () => {
      reconnectDelayMs = 500;
      handlers.onStatus('open');
    });
    next.addEventListener(eventName, (event) => {
      handlers.onEvent(event as MessageEvent<string>);
    });
    for (const name of controlEventNames) {
      next.addEventListener(name, (event) => {
        handlers.onControlEvent?.(name, event as MessageEvent<string>);
      });
    }
    next.addEventListener('error', (event) => {
      if (closed || next !== source) return;
      const data = (event as MessageEvent<string>).data;
      if (data) {
        try {
          const parsed = JSON.parse(data) as { error?: unknown };
          handlers.onError?.(typeof parsed.error === 'string' ? parsed.error : data);
        } catch {
          handlers.onError?.(data);
        }
      } else {
        handlers.onError?.(null);
      }
      handlers.onStatus('error');
      next.close();
      source = null;
      if (options.reconnect !== false) {
        reconnectTimer = setTimeout(connect, reconnectDelayMs);
        reconnectDelayMs = Math.min(reconnectDelayMs * 2, 5_000);
      }
    });
  };

  connect();
  return {
    close: () => {
      closed = true;
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      source?.close();
      source = null;
      handlers.onStatus('closed');
    },
  };
}
