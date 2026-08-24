import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  Clock,
  CheckCircle2,
  XCircle,
  PlayCircle,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Shield,
  User,
  Zap,
  RotateCcw,
  FileText,
  CreditCard,
  Loader2,
} from 'lucide-react';
import {
  getRecoveryCase,
  approveCase,
  rejectCase,
  executeCase,
  createRecoveryCheckout,
} from '../api/recoveryCases';
import { loadRazorpayScript } from '../utils/loadRazorpay';
import { usePolling } from '../hooks/usePolling';
import StatusBadge from '../components/StatusBadge';

import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import Modal from '../components/Modal';
import RecoveryPipeline from '../components/RecoveryPipeline';
import LiveStatusIndicator from '../components/LiveStatusIndicator';
import { getStrategyLabel } from '../utils/status';
import {
  formatCurrency,
  formatDate,
  timeAgo,
} from '../utils/format';
import { useAuth } from '../auth/AuthContext';

export default function RecoveryCaseDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  // Role-aware: VIEWERs can read a case but cannot act on it.
  const { hasRole } = useAuth();
  const canOperate = hasRole(['OPERATOR', 'ADMIN']);

  const [activeStatus, setActiveStatus] = useState<string | null>(null);
  const isCaseActive = activeStatus === null || !['RESOLVED_SUCCESS', 'RESOLVED_FAILED'].includes(activeStatus);

  const {
    data: caseData,
    loading,
    error,
    lastUpdated,
    pollingStatus,
    refetch,
  } = usePolling(
    async () => {
      const data = await getRecoveryCase(id!);
      if (data) {
        setActiveStatus(data.status);
      }
      return data;
    },
    [id],
    {
      intervalMs: 10000,
      enabled: isCaseActive,
    },
  );

  const [approveModalOpen, setApproveModalOpen] = useState(false);
  const [rejectModalOpen, setRejectModalOpen] = useState(false);
  const [executeModalOpen, setExecuteModalOpen] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [checkoutLoading, setCheckoutLoading] = useState(false);
  const [actionResult, setActionResult] = useState<string | null>(null);
  const [expandedLog, setExpandedLog] = useState<string | null>(null);

  const handleLaunchRecoveryCheckout = async () => {
    if (!id || !caseData) return;
    setCheckoutLoading(true);
    setActionResult(null);

    try {
      const scriptLoaded = await loadRazorpayScript();
      if (!scriptLoaded || !window.Razorpay) {
        throw new Error('Failed to load Razorpay Checkout SDK. Please check your internet connection.');
      }

      // 1. Request recovery checkout order from backend
      const res = await createRecoveryCheckout(id);

      // 2. Launch Razorpay Checkout Modal
      const options = {
        key: res.key_id,
        amount: res.amount,
        currency: res.currency,
        name: 'RecoverAI',
        description: `Recovery Payment (Case ${id.slice(0, 8)}...)`,
        order_id: res.order_id,
        prefill: {
          name: 'Demo Customer',
          email: 'customer@recoverai.local',
          contact: '9999999999',
        },
        notes: {
          recovery_case_id: res.recovery_case_id,
        },
        theme: {
          color: '#10b981',
        },
        modal: {
          ondismiss: () => {
            setCheckoutLoading(false);
            refetch();
          },
        },
        handler: () => {
          setCheckoutLoading(false);
          setActionResult(
            '✓ Test payment submitted. Awaiting backend webhook confirmation (payment.captured)...',
          );
          refetch();
        },
      };

      const rzp = new window.Razorpay(options);
      rzp.on('payment.failed', () => {
        setCheckoutLoading(false);
        setActionResult('⚠ Payment failed in Razorpay Test modal.');
        refetch();
      });
      rzp.open();
    } catch (err) {
      setCheckoutLoading(false);
      setActionResult(
        `Error: ${err instanceof Error ? err.message : 'Failed to launch checkout'}`,
      );
    }
  };

  const handleApprove = async () => {
    setActionLoading(true);
    setActionResult(null);
    try {
      const res = await approveCase(id!);
      setActionResult(`✓ ${res.message}`);
      setApproveModalOpen(false);

      refetch();
    } catch (err) {
      setActionResult(
        `Error: ${err instanceof Error ? err.message : 'Unknown error'}`,
      );
    } finally {
      setActionLoading(false);
    }
  };

  const handleReject = async () => {
    setActionLoading(true);
    setActionResult(null);
    try {
      const res = await rejectCase(id!);
      setActionResult(`✓ ${res.message}`);
      setRejectModalOpen(false);
      refetch();
    } catch (err) {
      setActionResult(
        `Error: ${err instanceof Error ? err.message : 'Unknown error'}`,
      );
    } finally {
      setActionLoading(false);
    }
  };

  const handleExecute = async () => {
    setActionLoading(true);
    setActionResult(null);
    try {
      const res = await executeCase(id!);
      setActionResult(
        `✓ Execution ${res.status}: ${res.message}`,
      );
      setExecuteModalOpen(false);
      refetch();
    } catch (err: unknown) {
      if (
        err &&
        typeof err === 'object' &&
        'status' in err &&
        (err as { status: number }).status === 404
      ) {
        setActionResult(
          '⚠ Manual execution is not available in the current environment.',
        );
        setExecuteModalOpen(false);
      } else {
        setActionResult(
          `Error: ${err instanceof Error ? err.message : 'Unknown error'}`,
        );
      }
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) {
    return (
      <div>
        <button
          onClick={() => navigate('/recovery-cases')}
          className="mb-4 flex items-center gap-2 text-sm text-text-muted hover:text-text-primary"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Cases
        </button>
        <LoadingSpinner text="Loading case details..." />
      </div>
    );
  }

  if (error) {
    return (
      <div>
        <button
          onClick={() => navigate('/recovery-cases')}
          className="mb-4 flex items-center gap-2 text-sm text-text-muted hover:text-text-primary"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Cases
        </button>
        <ErrorMessage message={error} onRetry={refetch} />
      </div>
    );
  }

  if (!caseData) return null;

  const showHumanReview =
    caseData.requires_human_approval &&
    caseData.approved_by_human === null;

  return (
    <div className="max-w-4xl">
      {/* Back button */}
      <button
        onClick={() => navigate('/recovery-cases')}
        className="mb-4 flex items-center gap-2 text-sm text-text-muted hover:text-text-primary"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Cases
      </button>

      {/* Case header */}
      <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight text-text-primary">
              Recovery Case
            </h1>
            <StatusBadge status={caseData.status} size="md" />
          </div>
          <p className="mt-1 font-mono text-xs text-text-muted">
            {caseData.recovery_case_id}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <LiveStatusIndicator
            status={isCaseActive ? pollingStatus : 'idle'}
            lastUpdated={lastUpdated}
            intervalSec={10}
          />
          <div className="flex items-center gap-2 text-xs text-text-muted">
            <Clock className="h-3.5 w-3.5" />
            Created {timeAgo(caseData.created_at)}
          </div>
        </div>
      </div>

      {/* Recovery Pipeline Visualization */}
      <RecoveryPipeline caseData={caseData} />

      {/* Resolved Success Verified Banner */}
      {caseData.status === 'RESOLVED_SUCCESS' && (
        <div className="mb-6 rounded-xl border-2 border-emerald-500/30 bg-emerald-500/10 p-6">
          <div className="flex items-start gap-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-emerald-500/20 text-emerald-400">
              <CheckCircle2 className="h-6 w-6" />
            </div>
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <h2 className="text-lg font-bold text-emerald-400">
                  Payment Recovered — Verified
                </h2>
                <span className="rounded-full bg-emerald-500/20 border border-emerald-500/40 px-2 py-0.5 text-[10px] font-semibold text-emerald-300">
                  Razorpay Test Mode Verified
                </span>
              </div>
              <p className="mt-1 text-xs text-emerald-200/90 leading-relaxed">
                This payment failure has been successfully recovered via verified Razorpay Test Mode settlement. Future recovery execution has been disarmed.
              </p>
              {typeof caseData.decision_audit_trail?.recovery_completion === 'object' && caseData.decision_audit_trail?.recovery_completion !== null && (
                <div className="mt-3 flex flex-wrap items-center gap-4 rounded-lg bg-black/20 border border-emerald-500/20 px-3 py-2 text-xs font-mono text-emerald-300">
                  <span>Payment ID: <strong className="text-white">{String((caseData.decision_audit_trail.recovery_completion as Record<string, unknown>).payment_id || '—')}</strong></span>
                  <span>• Order ID: <strong className="text-white">{String((caseData.decision_audit_trail.recovery_completion as Record<string, unknown>).order_id || '—')}</strong></span>
                  <span>• Amount: <strong className="text-white">{formatCurrency(Number((caseData.decision_audit_trail.recovery_completion as Record<string, unknown>).amount_paise || caseData.payment_event?.amount_paise || 0))}</strong></span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Recovery Payment Checkout Action */}
      {caseData.status === 'PENDING_EXECUTION' && (
        <div className="mb-6 rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-5">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold text-emerald-400 flex items-center gap-2">
                  <CreditCard className="h-4 w-4" />
                  Customer Recovery Payment
                </h3>
                <span className="rounded-full bg-emerald-500/10 border border-emerald-500/30 px-2 py-0.5 text-[10px] font-semibold text-emerald-400">
                  Test Mode
                </span>
              </div>
              <p className="text-xs text-text-muted mt-1">
                Simulate customer completing the recovery checkout via Razorpay Checkout Modal.
              </p>
            </div>
            {canOperate ? (
              <button
                onClick={handleLaunchRecoveryCheckout}
                disabled={checkoutLoading}
                className="flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2.5 text-xs font-semibold text-white shadow-sm transition-all hover:bg-emerald-500 active:scale-[0.98] disabled:opacity-50"
              >
                {checkoutLoading ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    <span>Preparing Checkout...</span>
                  </>
                ) : (
                  <>
                    <CreditCard className="h-3.5 w-3.5" />
                    <span>Complete Recovery Checkout (Test Mode)</span>
                  </>
                )}
              </button>
            ) : (
              <span className="text-xs text-text-muted">
                Viewer role — checkout requires an operator.
              </span>
            )}
          </div>
        </div>
      )}


      {/* Action result message */}
      {actionResult && (
        <div
          className={`mb-4 rounded-lg border p-3 text-sm ${
            actionResult.startsWith('✓')
              ? 'border-green-500/30 bg-green-500/10 text-green-400'
              : actionResult.startsWith('⚠')
                ? 'border-amber-500/30 bg-amber-500/10 text-amber-400'
                : 'border-red-500/30 bg-red-500/10 text-red-400'
          }`}
        >
          {actionResult}
        </div>
      )}

      {/* Human Review Section */}
      {showHumanReview && (
        <div className="mb-6 rounded-xl border-2 border-purple-500/30 bg-purple-500/5 p-6">
          <div className="flex items-start gap-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-purple-500/15">
              <AlertTriangle className="h-5 w-5 text-purple-400" />
            </div>
            <div className="flex-1">
              <h2 className="text-lg font-semibold text-purple-400">
                Human Review Required
              </h2>
              <p className="mt-1 text-sm text-text-secondary">
                This case requires human approval before it can proceed with
                automatic recovery execution.
              </p>
              <div className="mt-4 flex flex-wrap gap-3">
                {canOperate ? (
                  <>
                    <button
                      onClick={() => setApproveModalOpen(true)}
                      disabled={actionLoading}
                      className="flex items-center gap-2 rounded-lg bg-green-500/15 px-4 py-2.5 text-sm font-semibold text-green-400 transition-colors hover:bg-green-500/25 disabled:opacity-50"
                    >
                      <CheckCircle2 className="h-4 w-4" />
                      Approve Recovery
                    </button>
                    <button
                      onClick={() => setRejectModalOpen(true)}
                      disabled={actionLoading}
                      className="flex items-center gap-2 rounded-lg bg-red-500/15 px-4 py-2.5 text-sm font-semibold text-red-400 transition-colors hover:bg-red-500/25 disabled:opacity-50"
                    >
                      <XCircle className="h-4 w-4" />
                      Reject Recovery
                    </button>
                  </>
                ) : (
                  <span className="text-xs text-text-muted">
                    Viewer role — approval requires an operator.
                  </span>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Approved indicator */}
      {caseData.requires_human_approval &&
        caseData.approved_by_human === true && (
          <div className="mb-6 rounded-xl border border-green-500/30 bg-green-500/5 p-4">
            <div className="flex items-center gap-3">
              <CheckCircle2 className="h-5 w-5 text-green-400" />
              <div>
                <p className="text-sm font-semibold text-green-400">
                  Human Approved
                </p>
                <p className="text-xs text-green-400/70">
                  This case has been approved by a human operator.
                </p>
              </div>
            </div>
          </div>
        )}

      {/* Rejected indicator */}
      {caseData.requires_human_approval &&
        caseData.approved_by_human === false && (
          <div className="mb-6 rounded-xl border border-red-500/30 bg-red-500/5 p-4">
            <div className="flex items-center gap-3">
              <XCircle className="h-5 w-5 text-red-400" />
              <div>
                <p className="text-sm font-semibold text-red-400">
                  Human Rejected
                </p>
                <p className="text-xs text-red-400/70">
                  This case has been rejected. Automatic recovery is permanently
                  blocked.
                </p>
              </div>
            </div>
          </div>
        )}

      {/* Case Overview */}
      <Section icon={FileText} title="Case Overview">
        <InfoRow
          label="Status"
          value={<StatusBadge status={caseData.status} size="md" />}
        />
        <InfoRow label="Failure Category" value={caseData.failure_category} />
        <InfoRow
          label="Recovery Probability"
          value={
            caseData.recovery_probability !== null
              ? `${(caseData.recovery_probability * 100).toFixed(1)}%`
              : '—'
          }
        />
        <InfoRow
          label="Priority Score"
          value={
            caseData.priority_score !== null
              ? caseData.priority_score.toFixed(2)
              : '—'
          }
        />
        <InfoRow
          label="Strategy"
          value={getStrategyLabel(caseData.recommended_strategy)}
        />
        <InfoRow
          label="Expected Value"
          value={
            caseData.expected_value_paise !== null
              ? formatCurrency(caseData.expected_value_paise)
              : '—'
          }
        />
      </Section>

      {/* Payment Event Information */}
      {caseData.payment_event && (
        <Section icon={Zap} title="Payment Event Information">
          <InfoRow
            label="Payment Event ID"
            value={
              <span className="font-mono text-xs">
                {caseData.payment_event.payment_event_id.slice(0, 16)}…
              </span>
            }
          />
          <InfoRow label="Event Type" value={caseData.payment_event.event_type} />
          <InfoRow
            label="Payment ID"
            value={caseData.payment_event.external_payment_id ?? '—'}
          />
          <InfoRow
            label="Order ID"
            value={caseData.payment_event.external_order_id ?? '—'}
          />
          <InfoRow
            label="Amount"
            value={formatCurrency(
              caseData.payment_event.amount_paise,
              caseData.payment_event.currency,
            )}
          />
          <InfoRow
            label="Error Code"
            value={caseData.payment_event.error_code ?? '—'}
          />
          <InfoRow
            label="Error Reason"
            value={caseData.payment_event.error_reason ?? '—'}
          />
          <InfoRow
            label="Error Description"
            value={caseData.payment_event.error_description ?? '—'}
          />
          <InfoRow
            label="Event Created"
            value={formatDate(caseData.payment_event.created_at)}
          />
        </Section>
      )}

      {/* Retry Information */}
      <Section icon={RotateCcw} title="Retry Information">
        <InfoRow label="Retry Count" value={String(caseData.retry_count)} />
        <InfoRow
          label="Next Run"
          value={
            caseData.next_run_at ? formatDate(caseData.next_run_at) : '—'
          }
        />
        <InfoRow
          label="Requires Human Approval"
          value={caseData.requires_human_approval ? 'Yes' : 'No'}
        />
        <InfoRow
          label="Approved by Human"
          value={
            caseData.approved_by_human === true
              ? 'Yes'
              : caseData.approved_by_human === false
                ? 'No'
                : 'Not yet decided'
          }
        />
      </Section>

      {/* Decision Audit Trail */}
      {Object.keys(caseData.decision_audit_trail).length > 0 && (
        <Section icon={User} title="Decision Audit Trail">
          <div className="rounded-lg bg-bg-primary p-4">
            <pre className="overflow-x-auto font-mono text-xs text-text-secondary">
              {JSON.stringify(caseData.decision_audit_trail, null, 2)}
            </pre>
          </div>
        </Section>
      )}

      {/* Safe Simulation Execution */}
      <Section icon={PlayCircle} title="Safe Simulation Execution">
        <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-400" />
            <div>
              <p className="text-sm font-semibold text-amber-400">
                Development/Test Only
              </p>
              <p className="mt-1 text-xs text-amber-400/70">
                All backend safety and eligibility checks remain active. No real
                financial action will occur in simulation mode. This endpoint is
                only available in development/test environments.
              </p>
              {canOperate ? (
                <button
                  onClick={() => setExecuteModalOpen(true)}
                  disabled={actionLoading}
                  className="mt-3 flex items-center gap-2 rounded-lg bg-amber-500/15 px-4 py-2.5 text-sm font-semibold text-amber-400 transition-colors hover:bg-amber-500/25 disabled:opacity-50"
                >
                  <PlayCircle className="h-4 w-4" />
                  Run Simulation
                </button>
              ) : (
                <p className="mt-3 text-xs text-text-muted">
                  Viewer role — running a simulation requires an operator.
                </p>
              )}
            </div>
          </div>
        </div>
      </Section>

      {/* Execution History */}
      <Section icon={Shield} title="Execution History">
        {caseData.recent_execution_logs.length === 0 ? (
          <p className="py-4 text-center text-sm text-text-muted">
            No execution logs yet.
          </p>
        ) : (
          <div className="space-y-3">
            {caseData.recent_execution_logs.map((log) => (
              <div
                key={log.execution_log_id}
                className="rounded-lg border border-border bg-bg-primary"
              >
                <button
                  onClick={() =>
                    setExpandedLog(
                      expandedLog === log.execution_log_id
                        ? null
                        : log.execution_log_id,
                    )
                  }
                  className="flex w-full items-center justify-between px-4 py-3 text-left"
                >
                  <div className="flex items-center gap-3">
                    <StatusBadge status={log.status} />
                    <span className="text-sm text-text-secondary">
                      {log.action}
                    </span>
                    <span className="hidden text-xs text-text-muted sm:inline">
                      ({log.execution_mode})
                    </span>
                    {log.error_message && (
                      <span className="text-xs text-red-400">
                        — {log.error_message}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-text-muted">
                      {log.executed_at ? timeAgo(log.executed_at) : '—'}
                    </span>
                    {expandedLog === log.execution_log_id ? (
                      <ChevronUp className="h-4 w-4 text-text-muted" />
                    ) : (
                      <ChevronDown className="h-4 w-4 text-text-muted" />
                    )}
                  </div>
                </button>
                {expandedLog === log.execution_log_id && (
                  <div className="border-t border-border px-4 py-3">
                    <div className="grid gap-4 md:grid-cols-2">
                      <div>
                        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-text-muted">
                          Request Data
                        </h4>
                        <pre className="max-h-40 overflow-auto rounded bg-bg-secondary p-3 font-mono text-[11px] text-text-secondary">
                          {JSON.stringify(log.request_data, null, 2)}
                        </pre>
                      </div>
                      <div>
                        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-text-muted">
                          Response Data
                        </h4>
                        <pre className="max-h-40 overflow-auto rounded bg-bg-secondary p-3 font-mono text-[11px] text-text-secondary">
                          {JSON.stringify(log.response_data, null, 2)}
                        </pre>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* Approval Modal */}
      <Modal
        isOpen={approveModalOpen}
        onClose={() => setApproveModalOpen(false)}
        title="Approve Recovery"
        actions={
          <>
            <button
              onClick={() => setApproveModalOpen(false)}
              className="rounded-lg border border-border bg-bg-card px-4 py-2 text-sm font-medium text-text-secondary transition-colors hover:text-text-primary"
            >
              Cancel
            </button>
            <button
              onClick={handleApprove}
              disabled={actionLoading}
              className="flex items-center gap-2 rounded-lg bg-green-500/20 px-4 py-2 text-sm font-semibold text-green-400 transition-colors hover:bg-green-500/30 disabled:opacity-50"
            >
              {actionLoading ? 'Approving...' : 'Confirm Approval'}
            </button>
          </>
        }
      >
        <p>
          You are about to <strong className="text-green-400">approve</strong>{' '}
          the recovery workflow for this case.
        </p>
        <ul className="mt-3 list-inside list-disc space-y-1 text-text-muted">
          <li>This approves the recovery workflow to proceed.</li>
          <li>
            It does <strong>not</strong> directly perform a real financial action.
          </li>
          <li>
            Execution remains subject to backend eligibility and safety checks.
          </li>
        </ul>
      </Modal>

      {/* Rejection Modal */}
      <Modal
        isOpen={rejectModalOpen}
        onClose={() => setRejectModalOpen(false)}
        title="Reject Recovery"
        actions={
          <>
            <button
              onClick={() => setRejectModalOpen(false)}
              className="rounded-lg border border-border bg-bg-card px-4 py-2 text-sm font-medium text-text-secondary transition-colors hover:text-text-primary"
            >
              Cancel
            </button>
            <button
              onClick={handleReject}
              disabled={actionLoading}
              className="flex items-center gap-2 rounded-lg bg-red-500/20 px-4 py-2 text-sm font-semibold text-red-400 transition-colors hover:bg-red-500/30 disabled:opacity-50"
            >
              {actionLoading ? 'Rejecting...' : 'Confirm Rejection'}
            </button>
          </>
        }
      >
        <p>
          You are about to{' '}
          <strong className="text-red-400">permanently reject</strong> the
          recovery for this case.
        </p>
        <ul className="mt-3 list-inside list-disc space-y-1 text-text-muted">
          <li>
            This permanently prevents automatic recovery for this case.
          </li>
          <li>
            The case will transition to <code className="rounded bg-bg-primary px-1 py-0.5 text-xs">RESOLVED_FAILED</code>.
          </li>
          <li>Re-approval after rejection may be blocked.</li>
        </ul>
      </Modal>

      {/* Execution Modal */}
      <Modal
        isOpen={executeModalOpen}
        onClose={() => setExecuteModalOpen(false)}
        title="Confirm Simulation Execution"
        actions={
          <>
            <button
              onClick={() => setExecuteModalOpen(false)}
              className="rounded-lg border border-border bg-bg-card px-4 py-2 text-sm font-medium text-text-secondary transition-colors hover:text-text-primary"
            >
              Cancel
            </button>
            <button
              onClick={handleExecute}
              disabled={actionLoading}
              className="flex items-center gap-2 rounded-lg bg-amber-500/20 px-4 py-2 text-sm font-semibold text-amber-400 transition-colors hover:bg-amber-500/30 disabled:opacity-50"
            >
              {actionLoading ? 'Executing...' : 'Run Simulation'}
            </button>
          </>
        }
      >
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3">
          <p className="text-sm font-semibold text-amber-400">
            ⚠ Simulation Mode
          </p>
          <p className="mt-1 text-xs text-amber-400/70">
            This will trigger a simulated execution. No real financial action
            will occur. All backend safety and eligibility checks remain active.
          </p>
        </div>
      </Modal>
    </div>
  );
}

// --- Helper Components ---

function Section({
  icon: Icon,
  title,
  children,
}: {
  icon: React.ElementType;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-6 rounded-xl border border-border bg-bg-card">
      <div className="flex items-center gap-2 border-b border-border px-5 py-3">
        <Icon className="h-4 w-4 text-text-muted" />
        <h3 className="text-sm font-semibold text-text-primary">{title}</h3>
      </div>
      <div className="p-5">{children}</div>
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 py-2 sm:flex-row sm:items-center sm:justify-between">
      <span className="text-xs font-medium text-text-muted">{label}</span>
      <span className="text-sm text-text-secondary">{value}</span>
    </div>
  );
}
