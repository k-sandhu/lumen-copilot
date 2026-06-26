/**
 * MarkdownView (issue #163, per-code-block copy): the sanitized markdown pipeline
 * now renders a Copy button on every fenced code block (reusing the AnswerFooter
 * clipboard idiom), while inline code is untouched. These tests also guard the
 * invariants the pipeline must never break — output stays sanitized (no raw HTML
 * injection) — and that the override is resilient to a partial code fence
 * mid-stream (the chat renders partial markdown while the answer streams).
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { MarkdownView } from './markdown';

function renderMd(src: string) {
  return render(<MarkdownView>{src}</MarkdownView>, { wrapper: MemoryRouter });
}

describe('MarkdownView code blocks', () => {
  it('renders a working Copy button on a fenced code block', async () => {
    // user-event installs its own clipboard stub on setup.
    const user = userEvent.setup();
    const code = 'const x = 1;\nconsole.log(x);';
    renderMd('```js\n' + code + '\n```');

    const copy = screen.getByRole('button', { name: /copy code/i });
    await user.click(copy);

    // The button flips to a "copied" confirmation once the write resolves…
    expect(await screen.findByRole('button', { name: /code copied/i })).toBeInTheDocument();
    // …and the block text (across the highlighted spans) landed on the clipboard.
    // A fenced block carries a trailing newline, like every code block; assert the
    // copied text contains the source verbatim.
    const copied = await navigator.clipboard.readText();
    expect(copied.trimEnd()).toBe(code);
  });

  it('does NOT add a copy button to inline code', () => {
    renderMd('Use the `useFocusTrap` hook here.');
    // The inline `code` renders, but there is no per-block copy affordance.
    expect(screen.getByText('useFocusTrap').tagName).toBe('CODE');
    expect(screen.queryByRole('button', { name: /copy code/i })).not.toBeInTheDocument();
  });

  it('keeps output sanitized — a raw <script> in the source is not executed/injected', () => {
    const { container } = renderMd('Hello <script>alert(1)</script> world');
    // rehype-sanitize strips the script element entirely (no raw HTML injection).
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByText(/hello/i)).toBeInTheDocument();
  });

  it('is resilient to a partial (unterminated) code fence mid-stream', async () => {
    // While streaming, the source can arrive with an open fence and no closing
    // ``` yet — the override must still render a copyable block without throwing.
    const user = userEvent.setup();
    renderMd('```py\nprint("hi")');

    const copy = screen.getByRole('button', { name: /copy code/i });
    await user.click(copy);
    expect(await navigator.clipboard.readText()).toContain('print("hi")');
  });
});
