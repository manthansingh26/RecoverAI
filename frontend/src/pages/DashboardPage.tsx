import { useState, useCallback } from 'react';
import {
  FolderSearch,
  Clock,
  UserCheck,
  CheckCircle2,
  XCircle,
  Activity,
  PlayCircle,
  Ban,
  Sparkles,
  TrendingUp,
  DollarSign,
  ShieldCheck,
  BarChart3,
  PieChart as PieChartIcon,
  CalendarDays,
  AlertCircle,
  CreditCard,
} from 'lucide-react';
import {
  PieChart,
  Pie,
  Cell,
  Tooltip,
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  AreaChart,
  Area,
} from 'recharts';
import {
  fetchDashboardSummary,
  fetchDashboardAnalytics,
  fetchDashboardActivity,
} from '../api/dashboard';
import { usePolling } from '../hooks/usePolling';
import type {
  ActivityFeed as ActivityFeedType,
  DashboardAnalytics as DashboardAnalyticsType,
  DashboardSummary as DashboardSummaryType,
} from '../types';
import PageHeader from '../components/PageHeader';
import SummaryCard from '../components/SummaryCard';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import SimulationModal from '../components/SimulationModal';
import RazorpayCheckoutModal from '../components/RazorpayCheckoutModal';
import LiveStatusIndicator from '../components/LiveStatusIndicator';
import ActivityFeed from '../components/ActivityFeed';
import { formatCurrency } from '../utils/format';
import { getStatusLabel, getStrategyLabel } from '../utils/status';

// --- Chart color palette (dark theme) ---
const STATUS_COLORS: Record<string, string> = {
  RECEIVED: '#60a5fa',
  DECISION_PENDING: '#a78bfa',
  PENDING_EXECUTION: '#fbbf24',
  REQUIRES_HUMAN: '#c084fc',
  EXECUTING: '#22d3ee',
  RESOLVED_SUCCESS: '#4ade80',
  RESOLVED_FAILED: '#f87171',
};

const STRATEGY_COLORS: Record<string, string> = {
  WAIT_AND_RETRY: '#3b82f6',
  CREATE_PAYMENT_LINK: '#8b5cf6',
  HUMAN_REVIEW: '#f59e0b',
  STOP_RECOVERY: '#ef4444',
};

const CHART_FILL_GRADIENT = '#3b82f6';

function getStatusColor(status: string): string {
  return STATUS_COLORS[status] ?? '#6b7280';
}

function getStrategyColor(strategy: string): string {
  return STRATEGY_COLORS[strategy] ?? '#6b7280';
}

interface CombinedDashboardData {
  summary: DashboardSummaryType;
  analytics: DashboardAnalyticsType;
  activity: ActivityFeedType;
}

// --- Custom Recharts tooltip ---
function CustomTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { value: number; name?: string; payload?: Record<string, unknown> }[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-border bg-bg-secondary px-3 py-2 shadow-lg">
      {label && (
        <p className="mb-1 text-xs font-medium text-text-muted">{label}</p>
      )}
      {payload.map((entry, i) => (
        <p key={i} className="text-sm font-semibold text-text-primary">
          {entry.value?.toLocaleString()}
        </p>
      ))}
    </div>
  );
}

// --- Section wrapper ---
function AnalyticsSection({
  icon: Icon,
  title,
  children,
}: {
  icon: React.ElementType;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-8">
      <div className="mb-4 flex items-center gap-2">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent-blue/10">
          <Icon className="h-4 w-4 text-accent-blue" />
        </div>
        <h2 className="text-sm font-semibold uppercase tracking-widest text-text-muted">
          {title}
        </h2>
      </div>
      {children}
    </div>
  );
}

// --- Success Rate Gauge ---
function SuccessRateGauge({ rate }: { rate: number }) {
  const circumference = 2 * Math.PI * 54;
  const progress = (rate / 100) * circumference;
  const color =
    rate >= 70 ? '#4ade80' : rate >= 40 ? '#fbbf24' : rate > 0 ? '#f87171' : '#5c5e72';

  return (
    <div className="flex flex-col items-center justify-center">
      <div className="relative h-32 w-32">
        <svg className="h-full w-full -rotate-90" viewBox="0 0 120 120">
          <circle
            cx="60"
            cy="60"
            r="54"
            fill="none"
            stroke="#2a2b38"
            strokeWidth="8"
          />
          <circle
            cx="60"
            cy="60"
            r="54"
            fill="none"
            stroke={color}
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={`${progress} ${circumference}`}
            className="transition-all duration-700 ease-out"
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-2xl font-bold text-text-primary">
            {rate.toFixed(1)}%
          </span>
          <span className="text-[10px] font-medium text-text-muted">
            Success Rate
          </span>
        </div>
      </div>
    </div>
  );
}

// --- Metric card (smaller, for analytics) ---
function MetricCard({
  label,
  value,
  color = 'text-text-primary',
  subtitle,
}: {
  label: string;
  value: string;
  color?: string;
  subtitle?: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-primary p-4">
      <p className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
        {label}
      </p>
      <p className={`mt-1 text-xl font-bold tracking-tight ${color}`}>{value}</p>
      {subtitle && (
        <p className="mt-0.5 text-[10px] text-text-muted">{subtitle}</p>
      )}
    </div>
  );
}

// --- Empty chart state ---
function ChartEmptyState({ message }: { message?: string }) {
  return (
    <div className="flex h-full min-h-[200px] flex-col items-center justify-center gap-2 rounded-lg border border-border bg-bg-primary p-6">
      <AlertCircle className="h-8 w-8 text-text-muted/40" />
      <p className="text-center text-xs text-text-muted">
        {message ?? 'No data available yet'}
      </p>
      <p className="text-center text-[10px] text-text-muted/60">
        Use "Simulate Payment Failure" to generate recovery data
      </p>
    </div>
  );
}

export default function DashboardPage() {
  const fetchAll = useCallback(async (): Promise<CombinedDashboardData> => {
    const [summary, analytics, activity] = await Promise.all([
      fetchDashboardSummary(),
      fetchDashboardAnalytics(),
      fetchDashboardActivity(25),
    ]);
    return { summary, analytics, activity };
  }, []);

  const {
    data,
    loading,
    error,
    lastUpdated,
    pollingStatus,
    refetch,
  } = usePolling(fetchAll, [], { intervalMs: 15000 });

  const [simulationOpen, setSimulationOpen] = useState(false);
  const [razorpayTestOpen, setRazorpayTestOpen] = useState(false);

  const handleSimulationSuccess = useCallback(() => {
    refetch();
  }, [refetch]);

  const summary = data?.summary;
  const analytics = data?.analytics;
  const activityItems = data?.activity?.items ?? [];

  const hasCases = summary && summary.total_cases > 0;
  const hasAnalytics = analytics && analytics.performance.total_cases > 0;

  // Prepare chart data
  const statusChartData =
    analytics?.status_distribution
      .filter((item) => item.count > 0)
      .map((item) => ({
        name: getStatusLabel(item.status),
        value: item.count,
        status: item.status,
      })) ?? [];

  const strategyChartData =
    analytics?.strategy_distribution
      .filter((item) => item.count > 0)
      .map((item) => ({
        name: getStrategyLabel(item.strategy),
        value: item.count,
        strategy: item.strategy,
      })) ?? [];

  const activityChartData =
    analytics?.daily_activity.map((item) => ({
      date: new Date(item.date).toLocaleDateString('en-IN', {
        month: 'short',
        day: 'numeric',
      }),
      count: item.count,
    })) ?? [];

  return (
    <div>
      <PageHeader
        title="Dashboard"
        description="Live Recovery Intelligence & Operations"
        onRefresh={refetch}
        loading={pollingStatus === 'refreshing'}
        actions={
          <div className="flex flex-wrap items-center gap-3">
            <LiveStatusIndicator
              status={pollingStatus}
              lastUpdated={lastUpdated}
              intervalSec={15}
            />
            <button
              onClick={() => setRazorpayTestOpen(true)}
              className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3.5 py-2 text-sm font-semibold text-emerald-400 transition-all hover:bg-emerald-500/20 active:scale-[0.98]"
              title="Creates a Razorpay Test Mode payment to test the real payment.failed → webhook → recovery pipeline."
            >
              <CreditCard className="h-3.5 w-3.5" />
              Test Real Razorpay Payment
            </button>
            <button
              onClick={() => setSimulationOpen(true)}
              className="flex items-center gap-2 rounded-lg bg-accent-blue px-3.5 py-2 text-sm font-semibold text-white transition-all hover:bg-accent-blue/90 active:scale-[0.98]"
            >
              <Sparkles className="h-3.5 w-3.5" />
              Simulate Payment Failure
            </button>
          </div>
        }
      />

      {loading && <LoadingSpinner text="Loading live recovery operations..." />}

      {error && !data && <ErrorMessage message={error} onRetry={refetch} />}

      {data && summary && (
        <>
          {!hasCases ? (
            <div className="flex flex-col items-center justify-center gap-6 rounded-xl border border-border bg-bg-card py-20 px-6">
              <div className="flex h-16 w-16 items-center justify-center rounded-full bg-accent-blue/10">
                <BarChart3 className="h-8 w-8 text-accent-blue/60" />
              </div>
              <div className="text-center">
                <h3 className="text-lg font-semibold text-text-primary">
                  No recovery activity yet
                </h3>
                <p className="mt-2 max-w-md text-sm text-text-muted">
                  Use{' '}
                  <button
                    onClick={() => setRazorpayTestOpen(true)}
                    className="font-semibold text-emerald-400 hover:underline"
                  >
                    Test Real Razorpay Payment
                  </button>{' '}
                  or{' '}
                  <button
                    onClick={() => setSimulationOpen(true)}
                    className="font-semibold text-accent-blue hover:underline"
                  >
                    Simulate Payment Failure
                  </button>{' '}
                  to run a safe end-to-end RecoverAI scenario and generate real
                  dashboard analytics.
                </p>
              </div>
            </div>
          ) : (
            <>
              {/* ======= SECTION 1: Case Overview ======= */}
              <div className="mb-2">
                <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-text-muted">
                  Case Overview
                </h2>
              </div>
              <div className="mb-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                <SummaryCard
                  title="Total Cases"
                  value={summary.total_cases}
                  icon={FolderSearch}
                  color="text-accent-blue"
                />
                <SummaryCard
                  title="Received"
                  value={summary.received_cases}
                  icon={FolderSearch}
                  color="text-blue-400"
                />
                <SummaryCard
                  title="Pending Execution"
                  value={summary.pending_execution_cases}
                  icon={Clock}
                  color="text-amber-400"
                />
                <SummaryCard
                  title="Requires Human Review"
                  value={summary.requires_human_cases}
                  icon={UserCheck}
                  color="text-purple-400"
                />
                <SummaryCard
                  title="Awaiting Human Review"
                  value={summary.awaiting_human_review}
                  icon={Clock}
                  color="text-purple-300"
                />
                <SummaryCard
                  title="Approved"
                  value={summary.approved_cases}
                  icon={CheckCircle2}
                  color="text-green-400"
                />
                <SummaryCard
                  title="Resolved Success"
                  value={summary.resolved_success_cases}
                  icon={CheckCircle2}
                  color="text-green-500"
                />
                <SummaryCard
                  title="Resolved Failed"
                  value={summary.resolved_failed_cases}
                  icon={XCircle}
                  color="text-red-400"
                />
              </div>

              {/* ======= SECTION 2: Execution Metrics ======= */}
              <div className="mb-2">
                <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-text-muted">
                  Execution Metrics
                </h2>
              </div>
              <div className="mb-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <SummaryCard
                  title="Total Executions"
                  value={summary.total_execution_attempts}
                  icon={Activity}
                  color="text-accent-cyan"
                />
                <SummaryCard
                  title="Successful"
                  value={summary.successful_executions}
                  icon={PlayCircle}
                  color="text-green-400"
                />
                <SummaryCard
                  title="Failed"
                  value={summary.failed_executions}
                  icon={XCircle}
                  color="text-red-400"
                />
                <SummaryCard
                  title="Blocked"
                  value={summary.blocked_executions}
                  icon={Ban}
                  color="text-gray-400"
                />
              </div>

              {/* ======= SECTION 3: Live Recovery Activity Feed ======= */}
              <div className="mb-8">
                <ActivityFeed
                  items={activityItems}
                  onSimulateClick={() => setSimulationOpen(true)}
                />
              </div>

              {/* ======= ANALYTICS ======= */}
              {hasAnalytics && analytics && (
                <>
                  {/* ======= SECTION 4: Recovery Intelligence ======= */}
                  <AnalyticsSection
                    icon={PieChartIcon}
                    title="Recovery Intelligence"
                  >
                    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                      {/* Status Distribution */}
                      <div className="rounded-xl border border-border bg-bg-card p-5">
                        <h3 className="mb-4 text-xs font-semibold uppercase tracking-wider text-text-muted">
                          Status Distribution
                        </h3>
                        {statusChartData.length > 0 ? (
                          <div className="flex flex-col items-center gap-4 sm:flex-row">
                            <div className="h-52 w-52 flex-shrink-0">
                              <ResponsiveContainer width="100%" height="100%">
                                <PieChart>
                                  <Pie
                                    data={statusChartData}
                                    dataKey="value"
                                    nameKey="name"
                                    cx="50%"
                                    cy="50%"
                                    innerRadius={50}
                                    outerRadius={80}
                                    paddingAngle={3}
                                    strokeWidth={0}
                                  >
                                    {statusChartData.map((entry, index) => (
                                      <Cell
                                        key={index}
                                        fill={getStatusColor(entry.status)}
                                      />
                                    ))}
                                  </Pie>
                                  <Tooltip content={<CustomTooltip />} />
                                </PieChart>
                              </ResponsiveContainer>
                            </div>
                            <div className="flex flex-col gap-2">
                              {statusChartData.map((item, i) => (
                                <div
                                  key={i}
                                  className="flex items-center gap-2"
                                >
                                  <span
                                    className="h-2.5 w-2.5 rounded-full"
                                    style={{
                                      backgroundColor: getStatusColor(
                                        item.status,
                                      ),
                                    }}
                                  />
                                  <span className="text-xs text-text-secondary">
                                    {item.name}
                                  </span>
                                  <span className="ml-auto font-mono text-xs font-semibold text-text-primary">
                                    {item.value}
                                  </span>
                                </div>
                              ))}
                            </div>
                          </div>
                        ) : (
                          <ChartEmptyState message="No status data" />
                        )}
                      </div>

                      {/* Strategy Distribution */}
                      <div className="rounded-xl border border-border bg-bg-card p-5">
                        <h3 className="mb-4 text-xs font-semibold uppercase tracking-wider text-text-muted">
                          Strategy Distribution
                        </h3>
                        {strategyChartData.length > 0 ? (
                          <div className="h-52">
                            <ResponsiveContainer width="100%" height="100%">
                              <BarChart
                                data={strategyChartData}
                                layout="vertical"
                                margin={{ left: 10, right: 20, top: 5, bottom: 5 }}
                              >
                                <CartesianGrid
                                  strokeDasharray="3 3"
                                  stroke="#2a2b38"
                                  horizontal={false}
                                />
                                <XAxis
                                  type="number"
                                  allowDecimals={false}
                                  tick={{ fill: '#5c5e72', fontSize: 11 }}
                                  axisLine={{ stroke: '#2a2b38' }}
                                />
                                <YAxis
                                  dataKey="name"
                                  type="category"
                                  tick={{ fill: '#8b8d9e', fontSize: 11 }}
                                  axisLine={false}
                                  tickLine={false}
                                  width={130}
                                />
                                <Tooltip content={<CustomTooltip />} />
                                <Bar
                                  dataKey="value"
                                  radius={[0, 4, 4, 0]}
                                  maxBarSize={28}
                                >
                                  {strategyChartData.map((entry, index) => (
                                    <Cell
                                      key={index}
                                      fill={getStrategyColor(entry.strategy)}
                                    />
                                  ))}
                                </Bar>
                              </BarChart>
                            </ResponsiveContainer>
                          </div>
                        ) : (
                          <ChartEmptyState message="No strategy data" />
                        )}
                      </div>
                    </div>
                  </AnalyticsSection>

                  {/* ======= SECTION 5: Recovery Performance ======= */}
                  <AnalyticsSection
                    icon={TrendingUp}
                    title="Recovery Performance"
                  >
                    <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
                      {/* Success Rate Gauge */}
                      <div className="flex items-center justify-center rounded-xl border border-border bg-bg-card p-6">
                        <SuccessRateGauge
                          rate={analytics.performance.success_rate}
                        />
                      </div>

                      {/* Performance Metrics */}
                      <div className="col-span-1 grid grid-cols-2 gap-3 lg:col-span-2">
                        <MetricCard
                          label="Total Cases"
                          value={analytics.performance.total_cases.toLocaleString()}
                          color="text-accent-blue"
                        />
                        <MetricCard
                          label="Successful"
                          value={analytics.performance.successful_cases.toLocaleString()}
                          color="text-green-400"
                          subtitle="Resolved successfully"
                        />
                        <MetricCard
                          label="Failed"
                          value={analytics.performance.failed_cases.toLocaleString()}
                          color="text-red-400"
                          subtitle="Resolved failed"
                        />
                        <MetricCard
                          label="Pending"
                          value={analytics.performance.pending_cases.toLocaleString()}
                          color="text-amber-400"
                          subtitle="In progress"
                        />
                      </div>
                    </div>
                  </AnalyticsSection>

                  {/* ======= SECTION 6: Simulated Financial Impact ======= */}
                  <AnalyticsSection
                    icon={DollarSign}
                    title="Simulated Financial Impact"
                  >
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                      <MetricCard
                        label="Total Failed Value"
                        value={formatCurrency(
                          analytics.financial.total_failed_amount_paise,
                        )}
                        color="text-text-primary"
                        subtitle="All cases"
                      />
                      <MetricCard
                        label="Simulated Recovered"
                        value={formatCurrency(
                          analytics.financial.simulated_recovered_amount_paise,
                        )}
                        color="text-green-400"
                        subtitle="Simulation mode"
                      />
                      <MetricCard
                        label="Pending Recovery"
                        value={formatCurrency(
                          analytics.financial.pending_recovery_amount_paise,
                        )}
                        color="text-amber-400"
                        subtitle="In pipeline"
                      />
                      <MetricCard
                        label="Human Review"
                        value={formatCurrency(
                          analytics.financial.human_review_amount_paise,
                        )}
                        color="text-purple-400"
                        subtitle="Awaiting decision"
                      />
                    </div>
                  </AnalyticsSection>

                  {/* ======= SECTION 7: Human Review Intelligence ======= */}
                  <AnalyticsSection
                    icon={ShieldCheck}
                    title="Human Review Intelligence"
                  >
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                      <div className="rounded-xl border border-purple-500/20 bg-purple-500/5 p-5">
                        <div className="flex items-center gap-2">
                          <Clock className="h-4 w-4 text-purple-400" />
                          <span className="text-[11px] font-medium uppercase tracking-wider text-purple-400/70">
                            Awaiting Review
                          </span>
                        </div>
                        <p className="mt-2 text-3xl font-bold text-purple-400">
                          {analytics.human_review.awaiting_review}
                        </p>
                      </div>
                      <div className="rounded-xl border border-green-500/20 bg-green-500/5 p-5">
                        <div className="flex items-center gap-2">
                          <CheckCircle2 className="h-4 w-4 text-green-400" />
                          <span className="text-[11px] font-medium uppercase tracking-wider text-green-400/70">
                            Approved
                          </span>
                        </div>
                        <p className="mt-2 text-3xl font-bold text-green-400">
                          {analytics.human_review.approved}
                        </p>
                      </div>
                      <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-5">
                        <div className="flex items-center gap-2">
                          <XCircle className="h-4 w-4 text-red-400" />
                          <span className="text-[11px] font-medium uppercase tracking-wider text-red-400/70">
                            Rejected
                          </span>
                        </div>
                        <p className="mt-2 text-3xl font-bold text-red-400">
                          {analytics.human_review.rejected}
                        </p>
                      </div>
                    </div>
                  </AnalyticsSection>

                  {/* ======= SECTION 8: Activity Timeline ======= */}
                  <AnalyticsSection
                    icon={CalendarDays}
                    title="Last 30 Days Recovery Activity"
                  >
                    <div className="rounded-xl border border-border bg-bg-card p-5">
                      {activityChartData.length > 0 ? (
                        <div className="h-52">
                          <ResponsiveContainer width="100%" height="100%">
                            <AreaChart
                              data={activityChartData}
                              margin={{ left: 0, right: 10, top: 10, bottom: 0 }}
                            >
                              <defs>
                                <linearGradient
                                  id="activityGradient"
                                  x1="0"
                                  y1="0"
                                  x2="0"
                                  y2="1"
                                >
                                  <stop
                                    offset="0%"
                                    stopColor={CHART_FILL_GRADIENT}
                                    stopOpacity={0.3}
                                  />
                                  <stop
                                    offset="100%"
                                    stopColor={CHART_FILL_GRADIENT}
                                    stopOpacity={0.02}
                                  />
                                </linearGradient>
                              </defs>
                              <CartesianGrid
                                strokeDasharray="3 3"
                                stroke="#2a2b38"
                                vertical={false}
                              />
                              <XAxis
                                dataKey="date"
                                tick={{ fill: '#5c5e72', fontSize: 11 }}
                                axisLine={{ stroke: '#2a2b38' }}
                                tickLine={false}
                              />
                              <YAxis
                                allowDecimals={false}
                                tick={{ fill: '#5c5e72', fontSize: 11 }}
                                axisLine={false}
                                tickLine={false}
                                width={30}
                              />
                              <Tooltip content={<CustomTooltip />} />
                              <Area
                                type="monotone"
                                dataKey="count"
                                stroke={CHART_FILL_GRADIENT}
                                strokeWidth={2}
                                fill="url(#activityGradient)"
                              />
                            </AreaChart>
                          </ResponsiveContainer>
                        </div>
                      ) : (
                        <ChartEmptyState message="No activity in the last 30 days" />
                      )}
                    </div>
                  </AnalyticsSection>
                </>
              )}

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

      {/* Simulation Modal */}
      <SimulationModal
        isOpen={simulationOpen}
        onClose={() => setSimulationOpen(false)}
        onSuccess={handleSimulationSuccess}
      />

      {/* Real Razorpay Test Checkout Modal */}
      <RazorpayCheckoutModal
        isOpen={razorpayTestOpen}
        onClose={() => setRazorpayTestOpen(false)}
        onSuccess={handleSimulationSuccess}
      />
    </div>
  );
}
