import { Link } from 'react-router-dom';
import { Card, CardBody, CardHeader, CardTitle } from '@/components/Card';
import { StatusBadge, type StatusTone } from '@/components/StatusBadge';
import { isInternalLink, type CatalogEntry, type FeatureStatus } from '../catalog';

const STATUS_META: Record<FeatureStatus, { tone: StatusTone; label: string; pulse: boolean }> = {
  shipped: { tone: 'ok', label: 'Shipped', pulse: false },
  'in-progress': { tone: 'degraded', label: 'In progress', pulse: true },
  planned: { tone: 'pending', label: 'Planned', pulse: false },
};

const LINK_CLASS =
  'inline-block rounded-md border border-border px-2 py-0.5 text-xs text-foreground hover:bg-surface-muted';

export function FeatureCard({ entry }: { entry: CatalogEntry }) {
  const meta = STATUS_META[entry.status];

  return (
    <Card className="flex h-full flex-col">
      <CardHeader className="flex items-start justify-between gap-3">
        <CardTitle>{entry.title}</CardTitle>
        <StatusBadge tone={meta.tone} pulse={meta.pulse}>
          {meta.label}
        </StatusBadge>
      </CardHeader>
      <CardBody className="flex flex-1 flex-col gap-3">
        <p className="text-sm text-foreground-muted">{entry.summary}</p>
        {entry.links.length > 0 ? (
          <ul className="mt-auto flex flex-wrap gap-2 pt-1">
            {entry.links.map((link) => (
              <li key={link.href}>
                {isInternalLink(link.href) ? (
                  <Link to={link.href} className={LINK_CLASS}>
                    {link.label}
                  </Link>
                ) : (
                  <a
                    href={link.href}
                    target="_blank"
                    rel="noreferrer noopener"
                    className={LINK_CLASS}
                  >
                    {link.label} ↗
                  </a>
                )}
              </li>
            ))}
          </ul>
        ) : null}
      </CardBody>
    </Card>
  );
}
