import { Inbox } from 'lucide-react';

interface EmptyStateProps {
  title?: string;
  description?: string;
}

export default function EmptyState({
  title = 'No data available',
  description = 'There are no items to display.',
}: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-4 rounded-xl border border-border bg-bg-card py-16 px-6">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-bg-hover">
        <Inbox className="h-6 w-6 text-text-muted" />
      </div>
      <div className="text-center">
        <h3 className="text-sm font-semibold text-text-secondary">{title}</h3>
        <p className="mt-1 text-xs text-text-muted">{description}</p>
      </div>
    </div>
  );
}
