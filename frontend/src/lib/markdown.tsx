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
import { Link } from 'react-router-dom';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import rehypeHighlight from 'rehype-highlight';
import type { Options as SanitizeSchema } from 'rehype-sanitize';

// Extend the safe default schema to allow highlight.js class names on
// <code>/<span> (rehype-highlight emits `hljs-*` classes) and the disabled
// checkbox <input>s GFM emits for task lists (`- [x]`, used heavily in our
// docs' status markers). Everything else stays locked to the conservative
// default (no scripts, no event handlers).
const schema: SanitizeSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames ?? []), 'input'],
  attributes: {
    ...defaultSchema.attributes,
    code: [...(defaultSchema.attributes?.code ?? []), ['className']],
    span: [...(defaultSchema.attributes?.span ?? []), ['className']],
    input: [...(defaultSchema.attributes?.input ?? []), 'type', 'checked', 'disabled'],
  },
};

export interface MarkdownProps {
  /** Raw markdown source (may be partial during streaming). */
  children: string;
  className?: string;
  /**
   * Optional: map a link href to an in-app route. Return a path to render a
   * client-side <Link> (the docs viewer uses this for doc-to-doc links); return
   * null to keep the default safe new-tab <a>. Omitted ⇒ every link opens in a
   * new tab — the chat behavior, unchanged.
   */
  resolveInternalLink?: (href: string) => string | null;
}

function MarkdownViewComponent({ children, className, resolveInternalLink }: MarkdownProps) {
  return (
    <div className={`prose-md ${className ?? ''}`.trim()}>
      <Markdown
        // Order matters: sanitize AFTER gfm/highlight so their output is also cleaned.
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight, [rehypeSanitize, schema]]}
        components={{
          a: ({ children: linkChildren, href }) => {
            const to = href ? (resolveInternalLink?.(href) ?? null) : null;
            // Internal doc link → client-side navigation inside the viewer.
            if (to !== null) return <Link to={to}>{linkChildren}</Link>;
            // External/unknown links open safely in a new tab; rel guards tab-nabbing.
            return (
              <a href={href} target="_blank" rel="noreferrer noopener">
                {linkChildren}
              </a>
            );
          },
        }}
      >
        {children}
      </Markdown>
    </div>
  );
}

/** Memoized so streaming re-renders only when the source text changes. */
export const MarkdownView = memo(MarkdownViewComponent);
