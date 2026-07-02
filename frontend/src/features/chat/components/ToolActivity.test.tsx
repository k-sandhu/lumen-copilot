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

  it('falls back to a generic label for an unknown tool — never "undefined" (#280)', () => {
    // A tool outside the ChatTool union reaches the reducer (asToolCall only
    // checks typeof tool === 'string') and must not render "undefined…".
    render(
      <ToolActivity tools={[item({ tool: 'search_web' as ToolActivityItem['tool'] })]} />,
    );
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(screen.getByText('Searching sources…')).toBeInTheDocument();
  });

  it('falls back for an unknown DONE tool too (with the passage count)', () => {
    render(
      <ToolActivity
        tools={[item({ tool: 'search_web' as ToolActivityItem['tool'], status: 'done', hitCount: 3 })]}
      />,
    );
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(screen.getByText('Searching sources — 3 passages')).toBeInTheDocument();
  });
});
