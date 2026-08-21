import {
  FolderSearch,
  Clock,
  UserCheck,
  CheckCircle2,
  XCircle,
  Activity,
  PlayCircle,
  Ban,
} from 'lucide-react';
import { fetchDashboardSummary } from '../api/dashboard';
import { useApi } from '../hooks/useApi';
import PageHeader from '../components/PageHeader';
import SummaryCard from '../components/SummaryCard';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import EmptyState from '../components/EmptyState';

export default function DashboardPage() {
  const { data, loading, error, refetch } = useApi(
    () => fetchDashboardSummary(),
    [],
  );

  return (
    <div>
      <PageHeader
        title="Dashboard"
        description="Recovery operations overview"
        onRefresh={refetch}
        loading={loading}
      />

      {loading && <LoadingSpinner text="Loading dashboard metrics..." />}

      {error && <ErrorMessage message={error} onRetry={refetch} />}

      {data && (
        <>
          {data.total_cases === 0 ? (
            <EmptyState
              title="No recovery cases yet"
              description="Cases will appear here once payment events are received."
            />
          ) : (
            <>
              {/* Case Metrics */}
              <div className="mb-2">
                <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-text-muted">
                  Case Overview
                </h2>
              </div>
              <div className="mb-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                <SummaryCard
                  title="Total Cases"
                  value={data.total_cases}
                  icon={FolderSearch}
                  color="text-accent-blue"
                />
                <SummaryCard
                  title="Received"
                  value={data.received_cases}
                  icon={FolderSearch}
                  color="text-blue-400"
                />
                <SummaryCard
                  title="Pending Execution"
                  value={data.pending_execution_cases}
                  icon={Clock}
                  color="text-amber-400"
                />
                <SummaryCard
                  title="Requires Human Review"
                  value={data.requires_human_cases}
                  icon={UserCheck}
                  color="text-purple-400"
                />
                <SummaryCard
                  title="Awaiting Human Review"
                  value={data.awaiting_human_review}
                  icon={Clock}
                  color="text-purple-300"
                />
                <SummaryCard
                  title="Approved"
                  value={data.approved_cases}
                  icon={CheckCircle2}
                  color="text-green-400"
                />
                <SummaryCard
                  title="Resolved Success"
                  value={data.resolved_success_cases}
                  icon={CheckCircle2}
                  color="text-green-500"
                />
                <SummaryCard
                  title="Resolved Failed"
                  value={data.resolved_failed_cases}
                  icon={XCircle}
                  color="text-red-400"
                />
              </div>

              {/* Execution Metrics */}
              <div className="mb-2">
                <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-text-muted">
                  Execution Metrics
                </h2>
              </div>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <SummaryCard
                  title="Total Executions"
                  value={data.total_execution_attempts}
                  icon={Activity}
                  color="text-accent-cyan"
                />
                <SummaryCard
                  title="Successful"
                  value={data.successful_executions}
                  icon={PlayCircle}
                  color="text-green-400"
                />
                <SummaryCard
                  title="Failed"
                  value={data.failed_executions}
                  icon={XCircle}
                  color="text-red-400"
                />
                <SummaryCard
                  title="Blocked"
                  value={data.blocked_executions}
                  icon={Ban}
                  color="text-gray-400"
                />
              </div>

              {/* Simulation Notice */}
              <div className="mt-8 rounded-xl border border-amber-500/20 bg-amber-500/5 p-4">
                <div className="flex items-start gap-3">
                  <div className="mt-0.5 flex h-6 w-6 items-center justify-center rounded-full bg-amber-500/15">
                    <Activity className="h-3.5 w-3.5 text-amber-400" />
                  </div>
                  <div>
                    <p className="text-sm font-semibold text-amber-400">
                      Simulation Mode
                    </p>
                    <p className="mt-0.5 text-xs text-amber-400/70">
                      This dashboard operates in simulation mode. No real
                      financial actions are performed. All execution results
                      represent simulated outcomes for development and testing
                      purposes.
                    </p>
                  </div>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
