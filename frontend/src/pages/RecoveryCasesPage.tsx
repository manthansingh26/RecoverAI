import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronLeft, ChevronRight, ExternalLink } from 'lucide-react';
import { listRecoveryCases } from '../api/recoveryCases';
import { useApi } from '../hooks/useApi';
import PageHeader from '../components/PageHeader';
import StatusBadge from '../components/StatusBadge';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import EmptyState from '../components/EmptyState';
import { getStrategyLabel } from '../utils/status';
import { formatDate } from '../utils/format';

const STATUS_OPTIONS = [
  '',
  'RECEIVED',
  'PENDING_EXECUTION',
  'REQUIRES_HUMAN_REVIEW',
  'EXECUTING',
  'RESOLVED_SUCCESS',
  'RESOLVED_FAILED',
  'BLOCKED',
];

const STRATEGY_OPTIONS = [
  '',
  'IMMEDIATE_RETRY',
  'DELAYED_RETRY',
  'ALTERNATE_METHOD',
  'ESCALATE_HUMAN',
  'ABANDON',
];

export default function RecoveryCasesPage() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('');
  const [strategyFilter, setStrategyFilter] = useState('');
  const pageSize = 20;

  const { data, loading, error, refetch } = useApi(
    () =>
      listRecoveryCases({
        status: statusFilter || undefined,
        strategy: strategyFilter || undefined,
        page,
        page_size: pageSize,
      }),
    [page, statusFilter, strategyFilter],
  );

  const handleStatusChange = (value: string) => {
    setStatusFilter(value);
    setPage(1);
  };

  const handleStrategyChange = (value: string) => {
    setStrategyFilter(value);
    setPage(1);
  };

  const selectClass =
    'rounded-lg border border-border bg-bg-card px-3 py-2 text-sm text-text-secondary outline-none transition-colors focus:border-accent-blue focus:ring-1 focus:ring-accent-blue/30';

  return (
    <div>
      <PageHeader
        title="Recovery Cases"
        description="Monitor and manage payment recovery cases"
        onRefresh={refetch}
        loading={loading}
      />

      {/* Filters */}
      <div className="mb-4 flex flex-wrap gap-3">
        <select
          value={statusFilter}
          onChange={(e) => handleStatusChange(e.target.value)}
          className={selectClass}
        >
          <option value="">All Statuses</option>
          {STATUS_OPTIONS.filter(Boolean).map((s) => (
            <option key={s} value={s}>
              {s.replace(/_/g, ' ')}
            </option>
          ))}
        </select>
        <select
          value={strategyFilter}
          onChange={(e) => handleStrategyChange(e.target.value)}
          className={selectClass}
        >
          <option value="">All Strategies</option>
          {STRATEGY_OPTIONS.filter(Boolean).map((s) => (
            <option key={s} value={s}>
              {s.replace(/_/g, ' ')}
            </option>
          ))}
        </select>
      </div>

      {loading && <LoadingSpinner text="Loading recovery cases..." />}

      {error && <ErrorMessage message={error} onRetry={refetch} />}

      {data && (
        <>
          {data.items.length === 0 ? (
            <EmptyState
              title="No recovery cases found"
              description={
                statusFilter || strategyFilter
                  ? 'Try adjusting your filters.'
                  : 'Cases will appear here once payment events are received.'
              }
            />
          ) : (
            <>
              {/* Table */}
              <div className="overflow-x-auto rounded-xl border border-border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border bg-bg-secondary">
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-muted">
                        Case ID
                      </th>
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-muted">
                        Status
                      </th>
                      <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-muted md:table-cell">
                        Strategy
                      </th>
                      <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-muted sm:table-cell">
                        Retries
                      </th>
                      <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-muted lg:table-cell">
                        Human Approval
                      </th>
                      <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-muted xl:table-cell">
                        Next Run
                      </th>
                      <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-muted xl:table-cell">
                        Created
                      </th>
                      <th className="px-4 py-3 text-right text-xs font-semibold uppercase tracking-wider text-text-muted">
                        Action
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {data.items.map((item) => (
                      <tr
                        key={item.recovery_case_id}
                        className="cursor-pointer transition-colors hover:bg-bg-hover"
                        onClick={() =>
                          navigate(
                            `/recovery-cases/${item.recovery_case_id}`,
                          )
                        }
                      >
                        <td className="px-4 py-3">
                          <span className="font-mono text-xs text-text-secondary">
                            {item.recovery_case_id.slice(0, 8)}…
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <StatusBadge status={item.status} />
                        </td>
                        <td className="hidden px-4 py-3 text-text-secondary md:table-cell">
                          {getStrategyLabel(item.recommended_strategy)}
                        </td>
                        <td className="hidden px-4 py-3 font-mono text-text-secondary sm:table-cell">
                          {item.retry_count}
                        </td>
                        <td className="hidden px-4 py-3 lg:table-cell">
                          {item.requires_human_approval ? (
                            <span
                              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                                item.approved_by_human === true
                                  ? 'bg-green-500/15 text-green-400'
                                  : item.approved_by_human === false
                                    ? 'bg-red-500/15 text-red-400'
                                    : 'bg-purple-500/15 text-purple-400'
                              }`}
                            >
                              {item.approved_by_human === true
                                ? 'Approved'
                                : item.approved_by_human === false
                                  ? 'Rejected'
                                  : 'Pending'}
                            </span>
                          ) : (
                            <span className="text-xs text-text-muted">—</span>
                          )}
                        </td>
                        <td className="hidden px-4 py-3 text-xs text-text-muted xl:table-cell">
                          {item.next_run_at
                            ? formatDate(item.next_run_at)
                            : '—'}
                        </td>
                        <td className="hidden px-4 py-3 text-xs text-text-muted xl:table-cell">
                          {formatDate(item.created_at)}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              navigate(
                                `/recovery-cases/${item.recovery_case_id}`,
                              );
                            }}
                            className="inline-flex items-center gap-1 rounded-lg bg-accent-blue/10 px-2.5 py-1.5 text-xs font-medium text-accent-blue transition-colors hover:bg-accent-blue/20"
                          >
                            <ExternalLink className="h-3 w-3" />
                            View
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Pagination */}
              <div className="mt-4 flex items-center justify-between">
                <p className="text-xs text-text-muted">
                  Showing {(data.pagination.page - 1) * data.pagination.page_size + 1}–
                  {Math.min(
                    data.pagination.page * data.pagination.page_size,
                    data.pagination.total,
                  )}{' '}
                  of {data.pagination.total} cases
                </p>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page <= 1}
                    className="flex items-center gap-1 rounded-lg border border-border bg-bg-card px-3 py-1.5 text-xs font-medium text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
                  >
                    <ChevronLeft className="h-3.5 w-3.5" />
                    Prev
                  </button>
                  <span className="px-3 text-xs text-text-muted">
                    Page {data.pagination.page} of {data.pagination.total_pages}
                  </span>
                  <button
                    onClick={() =>
                      setPage((p) =>
                        Math.min(data.pagination.total_pages, p + 1),
                      )
                    }
                    disabled={page >= data.pagination.total_pages}
                    className="flex items-center gap-1 rounded-lg border border-border bg-bg-card px-3 py-1.5 text-xs font-medium text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
                  >
                    Next
                    <ChevronRight className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
