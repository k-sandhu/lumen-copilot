import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import type { Message } from '@/api';
import { ChatThread, type ChatThreadProps, type LiveAnswer } from './ChatThread';

const originalScrollIntoView = Element.prototype.scrollIntoView;

afterEach(() => {
  if (originalScrollIntoView) {
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      value: originalScrollIntoView,
    });
  } else {
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
  }
});

function mockScrollIntoView() {
  const scrollIntoView = vi.fn();
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    configurable: true,
    value: scrollIntoView,
  });
  return scrollIntoView;
}

function message(id: string, content: string): Message {
  return {
    id,
    session_id: 'sess-1',
    role: 'user',
    content,
    citations: [],
    created_at: '2026-06-18T00:01:00Z',
  };
}

function live(text: string): LiveAnswer {
  return {
    phase: 'streaming',
    text,
    citations: [],
    tools: [],
    codeRuns: [],
    problem: null,
    model: 'frontier/opus',
  };
}

function thread(overrides: Partial<ChatThreadProps> = {}) {
  const props: ChatThreadProps = {
    messages: [],
    isLoading: false,
    isError: false,
    error: null,
    onRetryLoad: () => {},
    live: null,
    onRetryStream: () => {},
    onOpenCitation: () => {},
    ...overrides,
  };
  return <ChatThread {...props} />;
}

function getViewport(container: HTMLElement): HTMLDivElement {
  const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
  expect(viewport).toBeInstanceOf(HTMLDivElement);
  return viewport as HTMLDivElement;
}

function setScrollMetrics(
  viewport: HTMLDivElement,
  metrics: { scrollHeight: number; clientHeight: number; scrollTop: number },
) {
  Object.defineProperty(viewport, 'scrollHeight', {
    configurable: true,
    value: metrics.scrollHeight,
  });
  Object.defineProperty(viewport, 'clientHeight', {
    configurable: true,
    value: metrics.clientHeight,
  });
  Object.defineProperty(viewport, 'scrollTop', {
    configurable: true,
    writable: true,
    value: metrics.scrollTop,
  });
}

describe('ChatThread', () => {
  it('releases sticky autoscroll when the user scrolls the Radix viewport up', () => {
    const scrollIntoView = mockScrollIntoView();
    const messages = [
      message('m1', 'Question one'),
      message('m2', 'Question two'),
      message('m3', 'Question three'),
    ];
    const { container, rerender } = render(thread({ messages, live: live('Streaming answer') }));
    const viewport = getViewport(container);
    scrollIntoView.mockClear();

    setScrollMetrics(viewport, { scrollHeight: 1000, clientHeight: 300, scrollTop: 100 });
    act(() => {
      viewport.dispatchEvent(new Event('scroll'));
    });

    rerender(
      thread({
        messages: [...messages, message('m4', 'A newer question')],
        live: live('Streaming answer with another token'),
      }),
    );

    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it('resumes sticky autoscroll when the viewport returns near the bottom', () => {
    const scrollIntoView = mockScrollIntoView();
    const messages = [message('m1', 'Question one'), message('m2', 'Question two')];
    const { container, rerender } = render(thread({ messages, live: live('Streaming answer') }));
    const viewport = getViewport(container);
    scrollIntoView.mockClear();

    setScrollMetrics(viewport, { scrollHeight: 1000, clientHeight: 300, scrollTop: 100 });
    act(() => {
      viewport.dispatchEvent(new Event('scroll'));
    });
    rerender(thread({ messages, live: live('Streaming answer with a paused token') }));
    expect(scrollIntoView).not.toHaveBeenCalled();

    setScrollMetrics(viewport, { scrollHeight: 1000, clientHeight: 300, scrollTop: 700 });
    act(() => {
      viewport.dispatchEvent(new Event('scroll'));
    });
    rerender(thread({ messages, live: live('Streaming answer with a followed token') }));

    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it('renders loading, error, and empty states', () => {
    const { rerender } = render(thread({ isLoading: true }));
    expect(screen.getByRole('status')).toHaveTextContent(/loading conversation/i);

    rerender(thread({ isError: true, error: new Error('Nope') }));
    expect(screen.getByRole('alert')).toHaveTextContent(/could not load this chat/i);
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();

    rerender(thread());
    expect(screen.getByText(/ask a question to start/i)).toBeInTheDocument();
  });

  it('renders persisted tool invocations on a reloaded assistant message (#377)', () => {
    mockScrollIntoView();
    const assistant: Message = {
      ...message('a1', 'You have 13 documents.'),
      role: 'assistant',
      tool_invocations: [
        {
          id: 'i1',
          tool_name: 'list_documents',
          ok: true,
          duration_ms: 12,
          created_at: '2026-07-15T00:00:00Z',
        },
        {
          id: 'i2',
          tool_name: 'run_python',
          ok: false,
          error: 'tool_denied',
          duration_ms: 0,
          created_at: '2026-07-15T00:00:01Z',
        },
      ],
    };
    render(thread({ messages: [assistant] }));
    // The settled turn shows WHICH tools ran and their outcome — not only live.
    expect(screen.getByText('Listing documents — 12 ms')).toBeInTheDocument();
    expect(screen.getByText('run_python — failed (tool_denied)')).toBeInTheDocument();
  });

  it('keeps a persisted user message free of tool badges (#377)', () => {
    mockScrollIntoView();
    const user: Message = {
      ...message('u1', 'list my docs'),
      tool_invocations: [],
    };
    render(thread({ messages: [user] }));
    expect(screen.queryByRole('list', { name: /tool activity/i })).not.toBeInTheDocument();
  });
});
