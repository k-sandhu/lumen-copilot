/**
 * Sanitized markdown renderer — the ONE markdown pipeline for the app
 * (frontend/AGENTS.md "Rendered, never raw"). Model/markdown output ALWAYS goes
 * through this: react-markdown + remark-gfm (tables, lists, strikethrough) +
 * rehype-sanitize (XSS-safe) + rehype-highlight (code highlighting).
 *
 * NEVER `dangerouslySetInnerHTML` raw content. Only lightly used today (the rail
 * welcome note), but it establishes the pattern the chat UI will reuse for
 * streamed assistant responses.
 */
import { memo } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import rehypeHighlight from 'rehype-highlight';
import type { Options as SanitizeSchema } from 'rehype-sanitize';

// Extend the safe default schema to allow highlight.js class names on
// <code>/<span> (rehype-highlight emits `hljs-*` classes). Everything else
// stays locked to the conservative default (no scripts, no event handlers).
const schema: SanitizeSchema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    code: [...(defaultSchema.attributes?.code ?? []), ['className']],
    span: [...(defaultSchema.attributes?.span ?? []), ['className']],
  },
};

export interface MarkdownProps {
  /** Raw markdown source (may be partial during streaming). */
  children: string;
  className?: string;
}

function MarkdownViewComponent({ children, className }: MarkdownProps) {
  return (
    <div className={`prose-md ${className ?? ''}`.trim()}>
      <Markdown
        // Order matters: sanitize AFTER gfm/highlight so their output is also cleaned.
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight, [rehypeSanitize, schema]]}
        components={{
          // Links open safely in a new tab; rel guards against tab-nabbing.
          a: ({ children: linkChildren, href }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {linkChildren}
            </a>
          ),
        }}
      >
        {children}
      </Markdown>
    </div>
  );
}

/** Memoized so streaming re-renders only when the source text changes. */
export const MarkdownView = memo(MarkdownViewComponent);
