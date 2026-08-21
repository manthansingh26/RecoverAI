import { useState, useCallback, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Zap,
  ShieldCheck,
  UserCheck,
  Ban,
  Play,
  X,
  RotateCcw,
  CheckCircle2,
  AlertTriangle,
  Loader2,
  ArrowRight,
} from 'lucide-react';
import type { SimulationScenario, SimulationResult } from '../types';
import { simulatePaymentFailure } from '../api/simulation';
import { formatCurrency } from '../utils/format';
import { getStatusLabel, getStrategyLabel } from '../utils/status';

// ---------------------------------------------------------------------------
// Scenario Card Data
// ---------------------------------------------------------------------------

interface ScenarioDefinition {
  id: SimulationScenario;
  title: string;
  label: string;
  description: string;
  icon: typeof Zap;
  amountLabel: string;
  riskLevel: string;
  riskColor: string;
  accentColor: string;
  bgColor: string;
  borderColor: string;
  iconBg: string;
}

const SCENARIOS: ScenarioDefinition[] = [
  {
    id: 'LOW_VALUE_TRANSIENT',
    title: 'Low Value — Transient',
    label: 'Auto Recovery',
    description: 'Simulate a low-risk transient payment failure.',
    icon: Zap,
    amountLabel: '~₹500',
    riskLevel: 'Low Risk',
    riskColor: 'text-green-400',
    accentColor: 'text-green-400',
    bgColor: 'bg-green-500/8',
    borderColor: 'border-green-500/25',
    iconBg: 'bg-green-500/15',
  },
  {
    id: 'MEDIUM_VALUE_RECOVERABLE',
    title: 'Medium Value — Recoverable',
    label: 'Smart Recovery',
    description: 'Simulate a recoverable failure for strategy analysis.',
    icon: ShieldCheck,
    amountLabel: '~₹2,500',
    riskLevel: 'Medium Risk',
    riskColor: 'text-amber-400',
    accentColor: 'text-accent-blue',
    bgColor: 'bg-accent-blue/8',
    borderColor: 'border-accent-blue/25',
    iconBg: 'bg-accent-blue/15',
  },
  {
    id: 'HIGH_VALUE_HUMAN_REVIEW',
    title: 'High Value — Human Review',
    label: 'Human Review',
    description: 'Simulate a high-risk failure requiring manual approval.',
    icon: UserCheck,
    amountLabel: '≥₹75,000',
    riskLevel: 'High Risk',
    riskColor: 'text-purple-400',
    accentColor: 'text-purple-400',
    bgColor: 'bg-purple-500/8',
    borderColor: 'border-purple-500/25',
    iconBg: 'bg-purple-500/15',
  },
  {
    id: 'PERMANENT_FAILURE',
    title: 'Permanent Failure',
    label: 'Safe Rejection',
    description: 'Simulate a non-recoverable failure that should not be retried.',
    icon: Ban,
    amountLabel: '~₹1,500',
    riskLevel: 'Non-recoverable',
    riskColor: 'text-red-400',
    accentColor: 'text-red-400',
    bgColor: 'bg-red-500/8',
    borderColor: 'border-red-500/25',
    iconBg: 'bg-red-500/15',
  },
];

// ---------------------------------------------------------------------------
// Pipeline progress steps
// ---------------------------------------------------------------------------

const PIPELINE_STEPS = [
  'Creating payment failure event',
  'Validating & normalizing event',
  'Running recovery analysis',
  'Applying policy & strategy',
  'Processing recovery case',
  'Finalizing results',
];

// ---------------------------------------------------------------------------
// Component props
// ---------------------------------------------------------------------------

interface SimulationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

type Phase = 'select' | 'processing' | 'result' | 'error';

// ---------------------------------------------------------------------------
// SimulationModal
// ---------------------------------------------------------------------------

export default function SimulationModal({
  isOpen,
  onClose,
  onSuccess,
}: SimulationModalProps) {
  const navigate = useNavigate();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [phase, setPhase] = useState<Phase>('select');
  const [selectedScenario, setSelectedScenario] =
    useState<SimulationScenario | null>(null);
  const [pipelineStep, setPipelineStep] = useState(0);
  const [result, setResult] = useState<SimulationResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Dialog open/close
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (isOpen) {
      dialog.showModal();
    } else {
      dialog.close();
    }
  }, [isOpen]);

  // Reset state when opening
  useEffect(() => {
    if (isOpen) {
      setPhase('select');
      setSelectedScenario(null);
      setPipelineStep(0);
      setResult(null);
      setError(null);
    }
  }, [isOpen]);

  // Pipeline step animation during processing
  useEffect(() => {
    if (phase !== 'processing') return;
    const interval = setInterval(() => {
      setPipelineStep((prev) => {
        if (prev >= PIPELINE_STEPS.length - 1) return prev;
        return prev + 1;
      });
    }, 600);
    return () => clearInterval(interval);
  }, [phase]);

  const handleRunSimulation = useCallback(async () => {
    if (!selectedScenario) return;

    setPhase('processing');
    setPipelineStep(0);
    setError(null);

    try {
      const simResult = await simulatePaymentFailure(selectedScenario);
      setResult(simResult);
      setPhase('result');
      onSuccess();
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'An unexpected error occurred';
      setError(message);
      setPhase('error');
    }
  }, [selectedScenario, onSuccess]);

  const handleViewCase = useCallback(() => {
    if (result?.recovery_case_id) {
      onClose();
      navigate(`/recovery-cases/${result.recovery_case_id}`);
    }
  }, [result, navigate, onClose]);

  const handleRunAnother = useCallback(() => {
    setPhase('select');
    setSelectedScenario(null);
    setPipelineStep(0);
    setResult(null);
    setError(null);
  }, []);

  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  return (
    <dialog
      ref={dialogRef}
      onClose={handleClose}
      className="rounded-2xl border border-border bg-bg-secondary p-0 shadow-2xl backdrop:bg-black/70 backdrop:backdrop-blur-sm w-full max-w-2xl"
    >
      <div className="p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-1">
          <h2 className="text-lg font-semibold text-text-primary">
            Simulate Payment Failure
          </h2>
          <button
            onClick={handleClose}
            className="rounded-lg p-1.5 text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {phase === 'select' && (
          <p className="text-xs text-text-muted mb-6">
            Create a realistic failed payment event and run it through the
            RecoverAI recovery pipeline.
          </p>
        )}

        {/* Phase: Scenario Selection */}
        {phase === 'select' && (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-6">
              {SCENARIOS.map((s) => {
                const Icon = s.icon;
                const isSelected = selectedScenario === s.id;
                return (
                  <button
                    key={s.id}
                    onClick={() => setSelectedScenario(s.id)}
                    className={`group relative text-left rounded-xl border p-4 transition-all duration-200 ${
                      isSelected
                        ? `${s.borderColor} ${s.bgColor} ring-1 ring-current/20`
                        : 'border-border bg-bg-card hover:border-border-light hover:bg-bg-hover'
                    }`}
                  >
                    <div className="flex items-start gap-3">
                      <div
                        className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${
                          isSelected ? s.iconBg : 'bg-bg-hover group-hover:bg-bg-card'
                        } transition-colors`}
                      >
                        <Icon
                          className={`h-4.5 w-4.5 ${
                            isSelected ? s.accentColor : 'text-text-muted'
                          } transition-colors`}
                        />
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 mb-0.5">
                          <span
                            className={`text-sm font-semibold ${
                              isSelected ? 'text-text-primary' : 'text-text-secondary'
                            }`}
                          >
                            {s.title}
                          </span>
                        </div>
                        <span
                          className={`inline-block text-[10px] font-medium uppercase tracking-wider px-1.5 py-0.5 rounded-md mb-1.5 ${
                            isSelected
                              ? `${s.accentColor} ${s.bgColor}`
                              : 'text-text-muted bg-bg-hover'
                          }`}
                        >
                          {s.label}
                        </span>
                        <p className="text-xs text-text-muted leading-relaxed">
                          {s.description}
                        </p>
                        <div className="mt-2 flex items-center gap-3 text-[11px]">
                          <span className="text-text-muted">
                            {s.amountLabel}
                          </span>
                          <span className="text-border">·</span>
                          <span className={s.riskColor}>{s.riskLevel}</span>
                        </div>
                      </div>
                    </div>
                    {isSelected && (
                      <div
                        className={`absolute top-3 right-3 flex h-5 w-5 items-center justify-center rounded-full ${s.iconBg}`}
                      >
                        <CheckCircle2 className={`h-3.5 w-3.5 ${s.accentColor}`} />
                      </div>
                    )}
                  </button>
                );
              })}
            </div>

            {/* Actions */}
            <div className="flex items-center justify-end gap-3">
              <button
                onClick={handleClose}
                className="rounded-lg border border-border bg-bg-card px-4 py-2 text-sm font-medium text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
              >
                Cancel
              </button>
              <button
                onClick={handleRunSimulation}
                disabled={!selectedScenario}
                className="flex items-center gap-2 rounded-lg bg-accent-blue px-4 py-2 text-sm font-semibold text-white transition-all hover:bg-accent-blue/90 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <Play className="h-3.5 w-3.5" />
                Run Simulation
              </button>
            </div>
          </>
        )}

        {/* Phase: Processing */}
        {phase === 'processing' && (
          <div className="py-6">
            <div className="flex flex-col items-center mb-8">
              <div className="flex h-14 w-14 items-center justify-center rounded-full bg-accent-blue/15 mb-4">
                <Loader2 className="h-7 w-7 text-accent-blue animate-spin" />
              </div>
              <h3 className="text-sm font-semibold text-text-primary mb-1">
                Processing Simulation
              </h3>
              <p className="text-xs text-text-muted">
                Running through the RecoverAI pipeline...
              </p>
            </div>

            <div className="space-y-2.5 max-w-sm mx-auto">
              {PIPELINE_STEPS.map((step, i) => (
                <div key={step} className="flex items-center gap-3">
                  <div className="flex h-6 w-6 shrink-0 items-center justify-center">
                    {i < pipelineStep ? (
                      <CheckCircle2 className="h-4 w-4 text-green-400" />
                    ) : i === pipelineStep ? (
                      <Loader2 className="h-4 w-4 text-accent-blue animate-spin" />
                    ) : (
                      <div className="h-2 w-2 rounded-full bg-border" />
                    )}
                  </div>
                  <span
                    className={`text-sm ${
                      i <= pipelineStep
                        ? 'text-text-primary'
                        : 'text-text-muted'
                    }`}
                  >
                    {step}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Phase: Result */}
        {phase === 'result' && result && (
          <div className="pt-2">
            <ResultDisplay
              result={result}
              onViewCase={handleViewCase}
              onRunAnother={handleRunAnother}
              onClose={handleClose}
            />
          </div>
        )}

        {/* Phase: Error */}
        {phase === 'error' && (
          <div className="py-6">
            <div className="flex flex-col items-center mb-6">
              <div className="flex h-14 w-14 items-center justify-center rounded-full bg-red-500/15 mb-4">
                <AlertTriangle className="h-7 w-7 text-red-400" />
              </div>
              <h3 className="text-sm font-semibold text-red-400 mb-1">
                Simulation Failed
              </h3>
              <p className="text-xs text-text-muted text-center max-w-sm">
                {error}
              </p>
            </div>
            <div className="flex items-center justify-center gap-3">
              <button
                onClick={handleRunAnother}
                className="flex items-center gap-2 rounded-lg border border-border bg-bg-card px-4 py-2 text-sm font-medium text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                Try Again
              </button>
              <button
                onClick={handleClose}
                className="rounded-lg border border-border bg-bg-card px-4 py-2 text-sm font-medium text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
              >
                Close
              </button>
            </div>
          </div>
        )}
      </div>
    </dialog>
  );
}

// ---------------------------------------------------------------------------
// Result Display Sub-component
// ---------------------------------------------------------------------------

function ResultDisplay({
  result,
  onViewCase,
  onRunAnother,
  onClose,
}: {
  result: SimulationResult;
  onViewCase: () => void;
  onRunAnother: () => void;
  onClose: () => void;
}) {
  const isHumanReview = result.requires_human_approval;
  const isSuccess = result.status === 'RESOLVED_SUCCESS';
  const isFailed = result.status === 'RESOLVED_FAILED';
  const isPending = result.status === 'PENDING_EXECUTION';

  // Determine result icon and color
  let iconBg = 'bg-green-500/15';
  let iconColor = 'text-green-400';
  let StatusIcon = CheckCircle2;
  let heading = 'Recovery case created successfully';

  if (isHumanReview) {
    iconBg = 'bg-purple-500/15';
    iconColor = 'text-purple-400';
    StatusIcon = UserCheck;
    heading = 'Human review required';
  } else if (isFailed) {
    iconBg = 'bg-red-500/15';
    iconColor = 'text-red-400';
    StatusIcon = Ban;
    heading = 'Recovery safely stopped';
  } else if (isPending) {
    iconBg = 'bg-amber-500/15';
    iconColor = 'text-amber-400';
    StatusIcon = Loader2;
    heading = 'Recovery case pending execution';
  }

  return (
    <>
      {/* Result header */}
      <div className="flex flex-col items-center mb-5">
        <div
          className={`flex h-12 w-12 items-center justify-center rounded-full ${iconBg} mb-3`}
        >
          <StatusIcon className={`h-6 w-6 ${iconColor}`} />
        </div>
        <h3 className="text-sm font-semibold text-text-primary">{heading}</h3>
        <p className="text-xs text-text-muted text-center mt-1 max-w-md leading-relaxed">
          {result.message}
        </p>
      </div>

      {/* Result details grid */}
      <div className="rounded-xl border border-border bg-bg-card p-4 mb-5">
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 text-xs">
          <DetailRow label="Scenario" value={getScenarioLabel(result.scenario)} />
          <DetailRow
            label="Amount"
            value={formatCurrency(result.amount_paise, result.currency)}
          />
          <DetailRow
            label="Classification"
            value={result.failure_category ? getStatusLabel(result.failure_category) : '—'}
          />
          <DetailRow
            label="Strategy"
            value={getStrategyLabel(result.recommended_strategy ?? null)}
          />
          <DetailRow
            label="Status"
            value={result.status ? getStatusLabel(result.status) : '—'}
            valueColor={
              isSuccess
                ? 'text-green-400'
                : isHumanReview
                  ? 'text-purple-400'
                  : isFailed
                    ? 'text-red-400'
                    : 'text-amber-400'
            }
          />
          <DetailRow
            label="Human Review"
            value={isHumanReview ? 'Required' : 'Not required'}
            valueColor={isHumanReview ? 'text-purple-400' : 'text-text-secondary'}
          />
          {result.recovery_probability !== null && (
            <DetailRow
              label="Recovery Probability"
              value={`${(result.recovery_probability * 100).toFixed(0)}%`}
            />
          )}
          {result.execution_result && (
            <DetailRow
              label="Execution"
              value={`${result.execution_result.status} (${result.execution_result.execution_mode})`}
              valueColor={
                result.execution_result.status === 'SUCCESS'
                  ? 'text-green-400'
                  : 'text-text-secondary'
              }
            />
          )}
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center justify-between">
        <button
          onClick={onRunAnother}
          className="flex items-center gap-2 rounded-lg border border-border bg-bg-card px-3.5 py-2 text-xs font-medium text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
        >
          <RotateCcw className="h-3 w-3" />
          Run Another
        </button>
        <div className="flex items-center gap-2.5">
          <button
            onClick={onClose}
            className="rounded-lg border border-border bg-bg-card px-3.5 py-2 text-xs font-medium text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
          >
            Close
          </button>
          {result.recovery_case_id && (
            <button
              onClick={onViewCase}
              className="flex items-center gap-2 rounded-lg bg-accent-blue px-3.5 py-2 text-xs font-semibold text-white transition-all hover:bg-accent-blue/90"
            >
              View Recovery Case
              <ArrowRight className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Detail row helper
// ---------------------------------------------------------------------------

function DetailRow({
  label,
  value,
  valueColor,
}: {
  label: string;
  value: string;
  valueColor?: string;
}) {
  return (
    <div>
      <span className="text-text-muted">{label}</span>
      <p className={`font-medium mt-0.5 ${valueColor ?? 'text-text-secondary'}`}>
        {value}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Scenario label helper
// ---------------------------------------------------------------------------

function getScenarioLabel(scenario: string): string {
  const labels: Record<string, string> = {
    LOW_VALUE_TRANSIENT: 'Low Value — Transient',
    MEDIUM_VALUE_RECOVERABLE: 'Medium Value — Recoverable',
    HIGH_VALUE_HUMAN_REVIEW: 'High Value — Human Review',
    PERMANENT_FAILURE: 'Permanent Failure',
  };
  return labels[scenario] ?? scenario;
}
