/**
 * Status badge color mapping for the RecoverAI dashboard.
 */

type BadgeStyle = {
  bg: string;
  text: string;
  border: string;
};

const STATUS_STYLES: Record<string, BadgeStyle> = {
  RECEIVED: {
    bg: 'bg-blue-500/15',
    text: 'text-blue-400',
    border: 'border-blue-500/30',
  },
  PENDING_EXECUTION: {
    bg: 'bg-amber-500/15',
    text: 'text-amber-400',
    border: 'border-amber-500/30',
  },
  REQUIRES_HUMAN_REVIEW: {
    bg: 'bg-purple-500/15',
    text: 'text-purple-400',
    border: 'border-purple-500/30',
  },
  EXECUTING: {
    bg: 'bg-cyan-500/15',
    text: 'text-cyan-400',
    border: 'border-cyan-500/30',
  },
  RESOLVED_SUCCESS: {
    bg: 'bg-green-500/15',
    text: 'text-green-400',
    border: 'border-green-500/30',
  },
  RESOLVED_FAILED: {
    bg: 'bg-red-500/15',
    text: 'text-red-400',
    border: 'border-red-500/30',
  },
  BLOCKED: {
    bg: 'bg-gray-500/15',
    text: 'text-gray-400',
    border: 'border-gray-500/30',
  },
};

const DEFAULT_STYLE: BadgeStyle = {
  bg: 'bg-gray-500/15',
  text: 'text-gray-400',
  border: 'border-gray-500/30',
};

export function getStatusStyle(status: string): BadgeStyle {
  return STATUS_STYLES[status] ?? DEFAULT_STYLE;
}

export function getStatusLabel(status: string): string {
  return status
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function getStrategyLabel(strategy: string | null): string {
  if (!strategy) return '—';
  return strategy
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
