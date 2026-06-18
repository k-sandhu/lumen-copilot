/**
 * One chat turn (user or assistant). Assistant content is ALWAYS rendered
 * through the sanitized markdown pipeline (frontend/AGENTS.md "Rendered, never
 * raw") — never a raw string, never dangerouslySetInnerHTML. Citations render as
 * a footer of clickable references (AC-2); a zero-citation assistant answer says
 * so honestly rather than fabricating references (AC-5).
 *
 * Pure/presentational: streaming state, tool activity, and citation collection
 * are owned by the hook/parent; this component just renders what it is given.
 */
import { memo } from 'react';
import { MarkdownView } from '@/lib/markdown';
import { cn } from '@/lib/cn';
import type { MessageRole } from '@/api';
import type { UiCitation } from '../model/citation';
import { CitationRef } from './CitationRef';
import { ToolActivity } from './ToolActivity';
import type { ToolActivity as ToolActivityItem } from '../model/streamReducer';

export interface MessageBubbleProps {
  role: MessageRole;
  content: string;
  model?: string | undefined;
  citations: UiCitation[];
  /** Live tool activity (assistant streaming turn only). */
  tools?: ToolActivityItem[];
  /** True while this assistant turn is still streaming (shows a caret). */
  streaming?: boolean;
  /** True once a completed assistant turn produced zero citations (AC-5). */
  showNoCitationsNotice?: boolean;
  onOpenCitation: (citation: UiCitation) => void;
}

function MessageBubbleComponent({
  role,
  content,
  model,
  citations,
  tools = [],
  streaming = false,
  showNoCitationsNotice = false,
  onOpenCitation,
}: MessageBubbleProps) {
  const isUser = role === 'user';

  return (
    <article
      className={cn('flex flex-col gap-1', isUser ? 'items-end' : 'items-stretch')}
      aria-label={`${role} message`}
    >
      <div
        className={cn(
          'max-w-[85ch] rounded-lg px-3 py-2 text-sm',
          isUser
            ? 'bg-accent/15 text-foreground'
            : 'border border-border bg-surface text-foreground',
        )}
      >
        {tools.length > 0 && <ToolActivity tools={tools} />}

        {isUser ? (
          // User input is plain text — render literally, no markdown surprises.
          <p className="whitespace-pre-wrap break-words">{content}</p>
        ) : (
          <div className="min-w-0 break-words">
            <MarkdownView>{content}</MarkdownView>
            {streaming && (
              <span
                className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-foreground-muted align-middle"
                aria-hidden="true"
              />
            )}
          </div>
        )}

        {!isUser && citations.length > 0 && (
          <footer className="mt-2 border-t border-border pt-2">
            <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-foreground-muted">
              Sources
            </p>
            <ol className="flex flex-col gap-1">
              {citations.map((citation, i) => (
                <li key={citation.id} className="flex items-start gap-1.5 text-xs">
                  <CitationRef
                    index={i + 1}
                    documentName={citation.documentName}
                    snippet={citation.snippet}
                    onOpen={() => onOpenCitation(citation)}
                  />
                  <button
                    type="button"
                    onClick={() => onOpenCitation(citation)}
                    className="truncate text-left text-foreground-muted hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    {citation.documentName}
                  </button>
                </li>
              ))}
            </ol>
          </footer>
        )}

        {!isUser && showNoCitationsNotice && (
          <p className="mt-2 border-t border-border pt-2 text-xs italic text-foreground-muted">
            No sources were cited for this answer.
          </p>
        )}
      </div>

      {!isUser && model && (
        <span className="text-[11px] text-foreground-muted">{model}</span>
      )}
    </article>
  );
}

export const MessageBubble = memo(MessageBubbleComponent);
