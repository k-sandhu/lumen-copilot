/**
 * ToolActivity (#280): renders the known retrieval tools' labels, and — for a
 * tool value outside the closed union (a renamed/new backend tool ahead of a
 * types.ts update, or a model-hallucinated name) — a generic fallback rather
 * than the literal "undefined…".
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ToolActivity } from './ToolActivity';
import type { ToolActivity as ToolActivityItem } from '../model/streamReducer';

function item(over: Partial<ToolActivityItem> = {}): ToolActivityItem {
  return { callId: 'c1', tool: 'search_text', status: 'running', ...over };
}

describe('ToolActivity', () => {
  it('labels a known running tool', () => {
    render(<ToolActivity tools={[item({ tool: 'search_text', status: 'running' })]} />);
    expect(screen.getByText('Searching documents…')).toBeInTheDocument();
  });

  it('falls back to the raw tool name for an unknown tool — never "undefined" (#280, #377)', () => {
    // A tool outside the ChatTool union reaches the reducer (asToolCall only
    // checks typeof tool === 'string') — and persisted invocations (#377) carry
    // arbitrary governed tool names (run_python, MCP tools). Showing the actual
    // name is honest; the guard against a literal "undefined…" stays.
    render(
      <ToolActivity tools={[item({ tool: 'run_python' as ToolActivityItem['tool'] })]} />,
    );
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(screen.getByText('run_python…')).toBeInTheDocument();
  });

  it('falls back for an unknown DONE tool too (with the passage count)', () => {
    render(
      <ToolActivity
        tools={[item({ tool: 'search_web' as ToolActivityItem['tool'], status: 'done', hitCount: 3 })]}
      />,
    );
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(screen.getByText('search_web — 3 passages')).toBeInTheDocument();
  });

  it('renders a failed/denied invocation with a danger badge (#377)', () => {
    const { container } = render(
      <ToolActivity
        tools={[item({ status: 'done', ok: false, summary: 'failed (tool_denied)' })]}
      />,
    );
    expect(screen.getByText('Searching documents — failed (tool_denied)')).toBeInTheDocument();
    expect(container.querySelector('.text-danger')).toBeTruthy();
  });

  it('keeps the ok badge for a successful settled invocation', () => {
    const { container } = render(
      <ToolActivity tools={[item({ status: 'done', ok: true, summary: '12 ms' })]} />,
    );
    expect(screen.getByText('Searching documents — 12 ms')).toBeInTheDocument();
    expect(container.querySelector('.text-danger')).toBeNull();
  });
});
