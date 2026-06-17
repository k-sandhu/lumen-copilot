import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Short label for what failed, shown in the fallback. */
  label?: string;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Wraps a feature root so a render-time crash doesn't wedge the whole app
 * (frontend/AGENTS.md "Resilient"). Offers a reset action — never a dead end.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Observability hook — a real logger/telemetry sink slots in here.
    console.error('Feature error boundary caught:', this.props.label, error, info);
  }

  private reset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (error) {
      return (
        <div
          role="alert"
          className="m-4 rounded-md border border-danger/40 bg-danger/10 p-4 text-sm"
        >
          <p className="font-semibold text-danger">
            {this.props.label ?? 'Something'} failed to render.
          </p>
          <p className="mt-1 text-foreground-muted">{error.message}</p>
          <button
            type="button"
            onClick={this.reset}
            className="mt-3 rounded-md border border-border bg-surface px-3 py-1.5 text-foreground hover:bg-surface-muted"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
