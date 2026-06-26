import { Link, useLocation } from 'react-router-dom';
import { PageChrome } from '@/components/PageChrome';

export function NotFoundPage() {
  const { pathname } = useLocation();

  return (
    <PageChrome title="Page not found">
      <div className="flex min-h-0 flex-1 items-center justify-center p-8">
        <div
          role="alert"
          className="max-w-md rounded-md border border-border bg-surface-muted p-6 text-center"
        >
          <p className="text-sm font-semibold">Page not found</p>
          <p className="mt-1 text-sm text-foreground-muted">
            No page matches <code className="rounded bg-surface px-1">{pathname}</code>.
          </p>
          <Link
            to="/"
            className="mt-3 inline-block text-sm text-accent underline underline-offset-2"
          >
            Back to assistant
          </Link>
        </div>
      </div>
    </PageChrome>
  );
}
