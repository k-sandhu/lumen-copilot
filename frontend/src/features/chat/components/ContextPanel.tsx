/**
 * The conversation context panel (spec 0007 #432, AC-2): what this conversation
 * actually touched — the distinct documents it cited (click → the source
 * inspector), the governed tools it invoked, the artifacts it produced, and its
 * token accounting. A PROJECTION of data the wire already carries (messages +
 * artifacts + usage queries — TanStack dedupes with the thread's own reads), so
 * it can never disagree with the thread. Every section handles
 * loading/empty/error (frontend/AGENTS.md quality bar).
 *
 * Deliberately absent: retrieved-but-uncited documents — the audit log owns
 * "accessed"; this panel shows what the conversation *used* (spec 0007 §2).
 */
import { useMemo } from 'react';
import { Icon } from '@/ui';
import { useArtifacts, formatBytes } from '@/features/artifacts';
import type { Citation, Message } from '@/api';
import { useMessages, useSessionUsage } from '../model/queries';
import { formatTokens } from '../model/composerHelpers';

export interface ContextPanelProps {
  sessionId: string;
  /** Open a cited document in the existing source inspector. */
  onOpenCitation: (citation: Citation) => void;
  /** Open the artifacts pane (spec 0007 AC-3), focused on one artifact. */
  onOpenArtifact: (artifactId: string) => void;
  onClose: () => void;
}

interface DocumentRow {
  documentId: string;
  documentName: string;
  citations: number;
  first: Citation;
}

interface ToolRow {
  tool: string;
  invocations: number;
  failures: number;
}

function aggregate(messages: Message[]): { documents: DocumentRow[]; tools: ToolRow[] } {
  const docs = new Map<string, DocumentRow>();
  const tools = new Map<string, ToolRow>();
  for (const message of messages) {
    for (const citation of message.citations ?? []) {
      const row = docs.get(citation.document_id);
      if (row) {
        row.citations += 1;
      } else {
        docs.set(citation.document_id, {
          documentId: citation.document_id,
          documentName: citation.document_name,
          citations: 1,
          first: citation,
        });
      }
    }
    for (const invocation of message.tool_invocations ?? []) {
      const row = tools.get(invocation.tool_name) ?? {
        tool: invocation.tool_name,
        invocations: 0,
        failures: 0,
      };
      row.invocations += 1;
      if (!invocation.ok) row.failures += 1;
      tools.set(invocation.tool_name, row);
    }
  }
  return { documents: [...docs.values()], tools: [...tools.values()] };
}

export function ContextPanel({
  sessionId,
  onOpenCitation,
  onOpenArtifact,
  onClose,
}: ContextPanelProps) {
  const messages = useMessages(sessionId);
  const usage = useSessionUsage(sessionId);
  const artifacts = useArtifacts({ sessionId });

  const { documents, tools } = useMemo(
    () => aggregate(messages.data?.items ?? []),
    [messages.data],
  );

  return (
    <div className="lc-ctxpanel" aria-label="Conversation context">
      <div className="lc-ctxpanel__head">
        <h2 className="lc-ctxpanel__title">
          <Icon name="list" />
          Context
        </h2>
        <button
          type="button"
          className="lc-ctxpanel__close"
          aria-label="Close context panel"
          onClick={onClose}
        >
          <Icon name="x" />
        </button>
      </div>

      <div className="lc-ctxpanel__body">
        <section aria-label="Token usage" className="lc-ctxpanel__section">
          <h3 className="lc-ctxpanel__label">Usage</h3>
          {usage.isLoading && <p className="lc-ctxpanel__muted">Loading usage…</p>}
          {usage.isError && <p className="lc-ctxpanel__muted">Usage unavailable.</p>}
          {usage.data && (
            <ul className="lc-ctxpanel__stats">
              <li>
                <strong>{usage.data.totals.answers}</strong> answers
              </li>
              <li>
                <strong>{formatTokens(usage.data.totals.total_tokens)}</strong> tokens total
              </li>
              <li>
                <strong>{formatTokens(usage.data.totals.prompt_tokens)}</strong> in ·{' '}
                <strong>{formatTokens(usage.data.totals.completion_tokens)}</strong> out
              </li>
              <li>
                <strong>{formatTokens(usage.data.totals.cached_prompt_tokens)}</strong> served from
                cache
              </li>
            </ul>
          )}
        </section>

        <section aria-label="Documents used" className="lc-ctxpanel__section">
          <h3 className="lc-ctxpanel__label">Documents cited</h3>
          {messages.isLoading && <p className="lc-ctxpanel__muted">Loading…</p>}
          {messages.isError && <p className="lc-ctxpanel__muted">Could not load messages.</p>}
          {!messages.isLoading && !messages.isError && documents.length === 0 && (
            <p className="lc-ctxpanel__muted">No documents cited yet.</p>
          )}
          <ul className="lc-ctxpanel__list">
            {documents.map((doc) => (
              <li key={doc.documentId}>
                <button
                  type="button"
                  className="lc-ctxpanel__row"
                  onClick={() => onOpenCitation(doc.first)}
                  aria-label={`Open ${doc.documentName}`}
                >
                  <Icon name="file-text" />
                  <span className="lc-ctxpanel__rowmain" title={doc.documentName}>
                    {doc.documentName}
                  </span>
                  <span className="lc-ctxpanel__count">
                    {doc.citations} citation{doc.citations === 1 ? '' : 's'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section aria-label="Tools used" className="lc-ctxpanel__section">
          <h3 className="lc-ctxpanel__label">Tools</h3>
          {!messages.isLoading && tools.length === 0 && (
            <p className="lc-ctxpanel__muted">No tools invoked yet.</p>
          )}
          <ul className="lc-ctxpanel__list">
            {tools.map((tool) => (
              <li key={tool.tool} className="lc-ctxpanel__row lc-ctxpanel__row--static">
                <Icon name="database" />
                <span className="lc-ctxpanel__rowmain">{tool.tool}</span>
                <span className="lc-ctxpanel__count">
                  {tool.invocations}×{tool.failures > 0 ? ` · ${tool.failures} failed` : ''}
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section aria-label="Artifacts produced" className="lc-ctxpanel__section">
          <h3 className="lc-ctxpanel__label">Artifacts</h3>
          {artifacts.isLoading && <p className="lc-ctxpanel__muted">Loading artifacts…</p>}
          {artifacts.isError && <p className="lc-ctxpanel__muted">Could not load artifacts.</p>}
          {artifacts.data && artifacts.data.items.length === 0 && (
            <p className="lc-ctxpanel__muted">No artifacts produced in this conversation.</p>
          )}
          <ul className="lc-ctxpanel__list">
            {(artifacts.data?.items ?? []).map((artifact) => (
              <li key={artifact.id}>
                <button
                  type="button"
                  className="lc-ctxpanel__row"
                  onClick={() => onOpenArtifact(artifact.id)}
                  aria-label={`View artifact ${artifact.filename}`}
                >
                  <Icon name="package" />
                  <span className="lc-ctxpanel__rowmain" title={artifact.filename}>
                    {artifact.filename}
                  </span>
                  <span className="lc-ctxpanel__count">{formatBytes(artifact.size_bytes)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
