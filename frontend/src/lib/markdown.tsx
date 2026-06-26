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
import {
  isValidElement,
  memo,
  useCallback,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from 'react';
import { Link } from 'react-router-dom';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import rehypeHighlight from 'rehype-highlight';
import type { Options as SanitizeSchema } from 'rehype-sanitize';
import { cn } from './cn';

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

/**
 * Recursively read the plain text out of a rendered markdown subtree. A fenced
 * block's text is split across the highlighted `<span>`s rehype-highlight emits,
 * so we walk the React children rather than assuming a single string child.
 */
function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (isValidElement(node)) {
    return textOf((node.props as { children?: ReactNode }).children);
  }
  return '';
}

/** Copy / check glyphs (the shared kit Icon set carries `check` but no `copy`). */
function CopyGlyph({ name }: { name: 'copy' | 'check' }): ReactNode {
  const common = {
    viewBox: '0 0 24 24',
    className: 'lc-icon',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.8,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
    focusable: false as const,
  };
  return name === 'copy' ? (
    <svg {...common}>
      <path d="M9 9h10v12H9V9Zm0 0V3h6l4 4M5 15H3V3h12v2" />
    </svg>
  ) : (
    <svg {...common}>
      <path d="m5 12 5 5 9-11" />
    </svg>
  );
}

/**
 * A fenced code block (the `pre` override) with a per-block Copy button. It owns
 * its own `copied` state in this CHILD component so the memoized `MarkdownView`
 * (and #166's memoization work) keeps holding — the parent never re-renders when
 * a single block's copy state flips. The copy idiom (clipboard write, transient
 * "copied", silent catch on denial) mirrors `AnswerFooter.onCopy` verbatim.
 */
function CodeBlock({ children, ...rest }: ComponentPropsWithoutRef<'pre'>) {
  const [copied, setCopied] = useState(false);
  const resetRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(textOf(children));
      setCopied(true);
      if (resetRef.current) clearTimeout(resetRef.current);
      resetRef.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard denied/unavailable: leave the icon unchanged — no false success.
      setCopied(false);
    }
  }, [children]);

  return (
    <div className="lc-codeblock">
      <button
        type="button"
        className={cn('lc-iconbtn lc-codeblock__copy', copied && 'lc-act-btn--ok')}
        aria-label={copied ? 'Code copied' : 'Copy code'}
        title={copied ? 'Copied' : 'Copy'}
        onClick={() => void onCopy()}
      >
        <CopyGlyph name={copied ? 'check' : 'copy'} />
      </button>
      <pre {...rest}>{children}</pre>
    </div>
  );
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
          // Fenced code blocks (always wrapped in <pre>) get a per-block Copy
          // button. Inline `code` has no surrounding <pre>, so it is untouched
          // and renders as today.
          pre: CodeBlock,
        }}
      >
        {children}
      </Markdown>
    </div>
  );
}

/** Memoized so streaming re-renders only when the source text changes. */
export const MarkdownView = memo(MarkdownViewComponent);
