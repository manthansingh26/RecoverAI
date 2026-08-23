import { useNavigate } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowRight,
  Ban,
  CheckCircle2,
  Clock,
  Loader2,
  Shield,
  Sparkles,
  UserCheck,
  XCircle,
  Zap,
} from 'lucide-react';
import type { ActivityItem } from '../types';
import { formatCurrency, timeAgo } from '../utils/format';

interface ActivityFeedProps {
  items: ActivityItem[];
  loading?: boolean;
  onSimulateClick?: () => void;
}

function getActivityConfig(type: string): {
  icon: React.ElementType;
  iconColor: string;
  bgColor: string;
  borderColor: string;
} {
  switch (type) {
    case 'PAYMENT_RECOVERED':
      return {
        icon: CheckCircle2,
        iconColor: 'text-emerald-400',
        bgColor: 'bg-emerald-500/15',
        borderColor: 'border-emerald-500/30',
      };
    case 'EXECUTION_SUCCESS':
      return {
        icon: CheckCircle2,
        iconColor: 'text-green-400',
        bgColor: 'bg-green-500/10',
        borderColor: 'border-green-500/20',
      };

    case 'EXECUTION_FAILED':
      return {
        icon: XCircle,
        iconColor: 'text-red-400',
        bgColor: 'bg-red-500/10',
        borderColor: 'border-red-500/20',
      };
    case 'EXECUTION_BLOCKED':
      return {
        icon: Ban,
        iconColor: 'text-amber-400',
        bgColor: 'bg-amber-500/10',
        borderColor: 'border-amber-500/20',
      };
    case 'EXECUTION_PENDING':
      return {
        icon: Loader2,
        iconColor: 'text-cyan-400',
        bgColor: 'bg-cyan-500/10',
        borderColor: 'border-cyan-500/20',
      };
    case 'CASE_CREATED':
      return {
        icon: Zap,
        iconColor: 'text-accent-blue',
        bgColor: 'bg-accent-blue/10',
        borderColor: 'border-accent-blue/20',
      };
    case 'STRATEGY_ASSIGNED':
      return {
        icon: Sparkles,
        iconColor: 'text-purple-400',
        bgColor: 'bg-purple-500/10',
        borderColor: 'border-purple-500/20',
      };
    case 'HUMAN_REVIEW_REQUIRED':
      return {
        icon: AlertTriangle,
        iconColor: 'text-purple-400',
        bgColor: 'bg-purple-500/15',
        borderColor: 'border-purple-500/30',
      };
    case 'HUMAN_REVIEW_APPROVED':
      return {
        icon: UserCheck,
        iconColor: 'text-green-400',
        bgColor: 'bg-green-500/10',
        borderColor: 'border-green-500/20',
      };
    case 'HUMAN_REVIEW_REJECTED':
      return {
        icon: XCircle,
        iconColor: 'text-red-400',
        bgColor: 'bg-red-500/10',
        borderColor: 'border-red-500/20',
      };
    default:
      return {
        icon: Shield,
        iconColor: 'text-text-muted',
        bgColor: 'bg-bg-hover',
        borderColor: 'border-border',
      };
  }
}

export default function ActivityFeed({
  items,
  loading = false,
  onSimulateClick,
}: ActivityFeedProps) {
  const navigate = useNavigate();

  if (items.length === 0 && !loading) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-border bg-bg-card p-8 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent-blue/10">
          <Clock className="h-6 w-6 text-accent-blue" />
        </div>
        <div>
          <p className="text-sm font-semibold text-text-primary">
            No recovery activity yet
          </p>
          <p className="mt-1 text-xs text-text-muted">
            New activities will appear automatically as failure events arrive.
          </p>
        </div>
        {onSimulateClick && (
          <button
            onClick={onSimulateClick}
            className="mt-2 inline-flex items-center gap-2 rounded-lg bg-accent-blue/15 px-3.5 py-2 text-xs font-semibold text-accent-blue transition-colors hover:bg-accent-blue/25"
          >
            <Sparkles className="h-3.5 w-3.5" />
            Simulate Payment Failure
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-border bg-bg-card p-5">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Clock className="h-4 w-4 text-accent-cyan" />
          <h3 className="text-sm font-semibold uppercase tracking-wider text-text-muted">
            Live Recovery Activity ({items.length})
          </h3>
        </div>
        <span className="text-xs text-text-muted">Newest events first</span>
      </div>

      <div className="max-h-[460px] space-y-3 overflow-y-auto pr-1">
        {items.map((item) => {
          const config = getActivityConfig(item.type);
          const Icon = config.icon;
          const isClickable = !!item.recovery_case_id;

          return (
            <div
              key={item.id}
              onClick={() => {
                if (isClickable) {
                  navigate(`/recovery-cases/${item.recovery_case_id}`);
                }
              }}
              className={`group flex items-start gap-3.5 rounded-xl border p-3.5 transition-all ${config.borderColor} ${config.bgColor} ${
                isClickable
                  ? 'cursor-pointer hover:border-border-light hover:bg-bg-hover'
                  : ''
              }`}
            >
              {/* Event Icon */}
              <div
                className={`mt-0.5 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-bg-primary/60 ${config.iconColor}`}
              >
                <Icon className="h-4 w-4" />
              </div>

              {/* Event Content */}
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-xs font-semibold text-text-primary">
                    {item.title}
                  </p>
                  <span className="text-[11px] text-text-muted">
                    {timeAgo(item.occurred_at)}
                  </span>
                </div>

                <p className="mt-0.5 text-xs text-text-secondary">
                  {item.description}
                </p>

                {/* Metadata Pills */}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {item.amount_paise !== null && item.amount_paise > 0 && (
                    <span className="rounded bg-bg-primary/80 px-2 py-0.5 font-mono text-[10px] font-semibold text-text-primary">
                      {formatCurrency(item.amount_paise)}
                    </span>
                  )}
                  {item.payment_id && (
                    <span className="rounded bg-bg-primary/80 px-2 py-0.5 font-mono text-[10px] text-text-muted">
                      {item.payment_id}
                    </span>
                  )}
                  {item.strategy && (
                    <span className="rounded bg-accent-blue/15 px-2 py-0.5 text-[10px] font-medium text-accent-blue">
                      {item.strategy.replace(/_/g, ' ')}
                    </span>
                  )}
                  {isClickable && (
                    <span className="ml-auto inline-flex items-center gap-1 text-[11px] font-medium text-accent-blue opacity-0 transition-opacity group-hover:opacity-100">
                      View Case
                      <ArrowRight className="h-3 w-3" />
                    </span>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
