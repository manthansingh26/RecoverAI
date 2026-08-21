import type { LucideIcon } from 'lucide-react';

interface SummaryCardProps {
  title: string;
  value: number;
  icon: LucideIcon;
  color: string;
  subtitle?: string;
}

export default function SummaryCard({
  title,
  value,
  icon: Icon,
  color,
  subtitle,
}: SummaryCardProps) {
  return (
    <div className="rounded-xl border border-border bg-bg-card p-5 transition-colors hover:border-border-light">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wider text-text-muted">
            {title}
          </p>
          <p className={`mt-2 text-3xl font-bold tracking-tight ${color}`}>
            {value.toLocaleString()}
          </p>
          {subtitle && (
            <p className="mt-1 text-xs text-text-muted">{subtitle}</p>
          )}
        </div>
        <div
          className={`flex h-10 w-10 items-center justify-center rounded-lg ${color.replace('text-', 'bg-')}/10`}
        >
          <Icon className={`h-5 w-5 ${color}`} />
        </div>
      </div>
    </div>
  );
}
