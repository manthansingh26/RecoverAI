/**
 * RecoveryPipeline — visualizes the lifecycle stages of a recovery case.
 *
 * Derives completed/active/pending/blocked/failed states from actual
 * RecoveryCaseDetail data. Does not fabricate step completion.
 */

import {
  ArrowRight,
  Ban,
  CheckCircle2,
  Clock,
  FileSearch,
  Loader2,
  Shield,
  Sparkles,
  UserCheck,
  XCircle,
  Zap,
} from 'lucide-react';
import type { RecoveryCaseDetail } from '../types';

// --- Stage state ---
type StageState = 'completed' | 'active' | 'pending' | 'blocked' | 'failed';

interface PipelineStage {
  id: string;
  label: string;
  description: string;
  icon: React.ElementType;
  state: StageState;
}

// --- State color/style mappings ---
const STATE_STYLES: Record<
  StageState,
  { ring: string; bg: string; icon: string; text: string; line: string }
> = {
  completed: {
    ring: 'ring-green-500/40',
    bg: 'bg-green-500/10',
    icon: 'text-green-400',
    text: 'text-green-400',
    line: 'bg-green-500/40',
  },
  active: {
    ring: 'ring-accent-blue/60',
    bg: 'bg-accent-blue/10',
    icon: 'text-accent-blue',
    text: 'text-accent-blue',
    line: 'bg-accent-blue/40',
  },
  pending: {
    ring: 'ring-border',
    bg: 'bg-bg-hover',
    icon: 'text-text-muted',
    text: 'text-text-muted',
    line: 'bg-border',
  },
  blocked: {
    ring: 'ring-amber-500/40',
    bg: 'bg-amber-500/10',
    icon: 'text-amber-400',
    text: 'text-amber-400',
    line: 'bg-amber-500/30',
  },
  failed: {
    ring: 'ring-red-500/40',
    bg: 'bg-red-500/10',
    icon: 'text-red-400',
    text: 'text-red-400',
    line: 'bg-red-500/30',
  },
};

const STATE_LABELS: Record<StageState, string> = {
  completed: 'Completed',
  active: 'In Progress',
  pending: 'Pending',
  blocked: 'Blocked',
  failed: 'Failed',
};

// --- Derive pipeline stages from case data ---
function derivePipelineStages(caseData: RecoveryCaseDetail): PipelineStage[] {
  const status = caseData.status;
  const hasStrategy = !!caseData.recommended_strategy;
  const requiresHuman = caseData.requires_human_approval;
  const approved = caseData.approved_by_human;
  const hasExecutionLogs = caseData.recent_execution_logs.length > 0;

  // Determine how far the pipeline has progressed
  const isReceived = status === 'RECEIVED';
  const isDecisionPending = status === 'DECISION_PENDING';
  const isPendingExecution = status === 'PENDING_EXECUTION';
  const isRequiresHuman = status === 'REQUIRES_HUMAN';
  const isExecuting = status === 'EXECUTING';
  const isSuccess = status === 'RESOLVED_SUCCESS';
  const isFailed = status === 'RESOLVED_FAILED';
  const isResolved = isSuccess || isFailed;

  // Stage 1: Payment Failure Detected — always completed if case exists
  const stage1: PipelineStage = {
    id: 'payment-failure',
    label: 'Payment Failure',
    description: 'Payment failure event detected',
    icon: Zap,
    state: 'completed',
  };

  // Stage 2: Event Ingested — always completed if case exists
  const stage2: PipelineStage = {
    id: 'event-ingested',
    label: 'Event Ingested',
    description: 'Payment event normalized and stored',
    icon: FileSearch,
    state: 'completed',
  };

  // Stage 3: Failure Classified
  const classifiedDone = !isReceived;
  const stage3: PipelineStage = {
    id: 'failure-classified',
    label: 'Failure Classified',
    description: caseData.failure_category
      ? `Category: ${caseData.failure_category}`
      : 'Failure category determined',
    icon: Sparkles,
    state: classifiedDone ? 'completed' : isReceived ? 'active' : 'pending',
  };

  // Stage 4: Strategy Selected
  const strategyDone = classifiedDone && hasStrategy;
  const stage4: PipelineStage = {
    id: 'strategy-selected',
    label: 'Strategy Selected',
    description: caseData.recommended_strategy
      ? `Strategy: ${caseData.recommended_strategy.replace(/_/g, ' ')}`
      : 'Recovery strategy determined',
    icon: Shield,
    state: strategyDone
      ? 'completed'
      : isDecisionPending
        ? 'active'
        : classifiedDone && !hasStrategy
          ? 'active'
          : 'pending',
  };

  // Stage 5: Policy Validated — completed if strategy selected and case moved past decision
  const policyDone =
    strategyDone &&
    !isReceived &&
    !isDecisionPending;
  const stage5: PipelineStage = {
    id: 'policy-validated',
    label: 'Policy Validated',
    description: 'Recovery policy and safety checks passed',
    icon: CheckCircle2,
    state: policyDone
      ? 'completed'
      : strategyDone && isDecisionPending
        ? 'active'
        : 'pending',
  };

  // Stage 6: Human Review / Execution
  let stage6State: StageState = 'pending';
  let stage6Label = 'Review / Execution';
  let stage6Description = 'Human review or automated execution';
  let stage6Icon: React.ElementType = UserCheck;

  if (requiresHuman) {
    stage6Label = 'Human Review';
    stage6Icon = UserCheck;
    if (approved === true) {
      stage6State = 'completed';
      stage6Description = 'Approved by human operator';
    } else if (approved === false) {
      stage6State = 'failed';
      stage6Description = 'Rejected by human operator';
    } else if (isRequiresHuman) {
      stage6State = 'active';
      stage6Description = 'Awaiting human approval';
    } else if (policyDone) {
      stage6State = 'active';
      stage6Description = 'Awaiting human review';
    }
  } else {
    stage6Label = 'Execution';
    stage6Icon = Loader2;
    if (isExecuting) {
      stage6State = 'active';
      stage6Description = 'Recovery execution in progress';
    } else if (isPendingExecution) {
      stage6State = 'active';
      stage6Description = 'Pending execution';
    } else if (isResolved && hasExecutionLogs) {
      stage6State = 'completed';
      stage6Description = 'Execution completed';
    } else if (policyDone) {
      stage6State = 'pending';
      stage6Description = 'Awaiting execution';
    }
  }

  const stage6: PipelineStage = {
    id: 'review-execution',
    label: stage6Label,
    description: stage6Description,
    icon: stage6Icon,
    state: stage6State,
  };

  // Stage 7: Final State
  let stage7State: StageState = 'pending';
  let stage7Description = 'Final recovery outcome';
  let stage7Icon: React.ElementType = Clock;

  if (isSuccess) {
    stage7State = 'completed';
    stage7Description = 'Payment Recovered (Verified)';
    stage7Icon = CheckCircle2;
  } else if (isFailed) {

    stage7State = 'failed';
    stage7Description = approved === false
      ? 'Recovery stopped — rejected by human'
      : 'Recovery failed';
    stage7Icon = XCircle;
  } else if (
    caseData.recommended_strategy === 'STOP_RECOVERY' &&
    !isReceived &&
    !isDecisionPending
  ) {
    stage7State = 'blocked';
    stage7Description = 'Recovery not recommended';
    stage7Icon = Ban;
  }

  const stage7: PipelineStage = {
    id: 'final-state',
    label: 'Final State',
    description: stage7Description,
    icon: stage7Icon,
    state: stage7State,
  };

  return [stage1, stage2, stage3, stage4, stage5, stage6, stage7];
}

// --- Component ---

interface RecoveryPipelineProps {
  caseData: RecoveryCaseDetail;
}

export default function RecoveryPipeline({ caseData }: RecoveryPipelineProps) {
  const stages = derivePipelineStages(caseData);

  return (
    <div className="mb-6 rounded-xl border border-border bg-bg-card p-5">
      <h3 className="mb-4 flex items-center gap-2 text-sm font-semibold text-text-primary">
        <ArrowRight className="h-4 w-4 text-accent-blue" />
        Recovery Pipeline
      </h3>

      {/* Desktop: horizontal pipeline */}
      <div className="hidden md:flex md:items-start md:gap-0">
        {stages.map((stage, index) => {
          const styles = STATE_STYLES[stage.state];
          return (
            <div key={stage.id} className="flex flex-1 items-start">
              <div className="flex flex-col items-center text-center">
                {/* Icon circle */}
                <div
                  className={`flex h-10 w-10 items-center justify-center rounded-full ring-2 ${styles.ring} ${styles.bg} transition-all`}
                  title={`${stage.label}: ${STATE_LABELS[stage.state]}`}
                >
                  <stage.icon className={`h-4.5 w-4.5 ${styles.icon}`} />
                </div>

                {/* Label */}
                <p
                  className={`mt-2 text-[11px] font-semibold leading-tight ${styles.text}`}
                >
                  {stage.label}
                </p>

                {/* Description */}
                <p className="mt-0.5 max-w-[100px] text-[10px] leading-tight text-text-muted">
                  {stage.description}
                </p>

                {/* State badge */}
                <span
                  className={`mt-1.5 rounded-full px-2 py-0.5 text-[9px] font-medium ${styles.bg} ${styles.text}`}
                >
                  {STATE_LABELS[stage.state]}
                </span>
              </div>

              {/* Connecting line */}
              {index < stages.length - 1 && (
                <div className="mt-5 flex flex-1 items-center px-1">
                  <div className={`h-0.5 w-full rounded ${styles.line}`} />
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Mobile: vertical pipeline */}
      <div className="flex flex-col md:hidden">
        {stages.map((stage, index) => {
          const styles = STATE_STYLES[stage.state];
          return (
            <div key={stage.id}>
              <div className="flex items-start gap-3">
                {/* Icon + vertical line */}
                <div className="flex flex-col items-center">
                  <div
                    className={`flex h-9 w-9 items-center justify-center rounded-full ring-2 ${styles.ring} ${styles.bg}`}
                    title={`${stage.label}: ${STATE_LABELS[stage.state]}`}
                  >
                    <stage.icon className={`h-4 w-4 ${styles.icon}`} />
                  </div>
                  {index < stages.length - 1 && (
                    <div className={`my-1 h-6 w-0.5 rounded ${styles.line}`} />
                  )}
                </div>

                {/* Text */}
                <div className="pt-1.5">
                  <div className="flex items-center gap-2">
                    <p
                      className={`text-xs font-semibold ${styles.text}`}
                    >
                      {stage.label}
                    </p>
                    <span
                      className={`rounded-full px-1.5 py-0.5 text-[9px] font-medium ${styles.bg} ${styles.text}`}
                    >
                      {STATE_LABELS[stage.state]}
                    </span>
                  </div>
                  <p className="mt-0.5 text-[11px] text-text-muted">
                    {stage.description}
                  </p>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
