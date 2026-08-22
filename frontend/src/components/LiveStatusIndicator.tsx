import { useEffect, useState } from 'react';
import { AlertCircle, CheckCircle2, PauseCircle, RefreshCw } from 'lucide-react';
import type { PollingStatus } from '../hooks/usePolling';

interface LiveStatusIndicatorProps {
  status: PollingStatus;
  lastUpdated?: Date | null;
  intervalSec?: number;
  className?: string;
}

export default function LiveStatusIndicator({
  status,
  lastUpdated,
  intervalSec = 15,
  className = '',
}: LiveStatusIndicatorProps) {
  const [currentTime, setCurrentTime] = useState<number>(() => Date.now());

  // Re-calculate relative time every 5 seconds
  useEffect(() => {
    const timer = setInterval(() => setCurrentTime(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);

  const getRelativeTime = (date: Date | null | undefined, nowMs: number): string => {
    if (!date) return '';
    const diffSec = Math.floor((nowMs - date.getTime()) / 1000);
    if (diffSec < 5) return 'just now';
    if (diffSec < 60) return `${diffSec}s ago`;
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    return 'earlier';
  };

  const timeString = getRelativeTime(lastUpdated, currentTime);

  return (
    <div
      className={`inline-flex items-center gap-2 rounded-lg border border-border bg-bg-card/80 px-3 py-1.5 text-xs backdrop-blur-sm ${className}`}
    >
      {status === 'live' && (
        <>
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-green-500" />
          </span>
          <span className="font-medium text-green-400">Live</span>
          <span className="text-text-muted">•</span>
          <span className="text-text-muted">Auto-refreshing ({intervalSec}s)</span>
          {timeString && (
            <>
              <span className="text-text-muted">•</span>
              <span className="text-text-muted">Updated {timeString}</span>
            </>
          )}
        </>
      )}

      {status === 'refreshing' && (
        <>
          <RefreshCw className="h-3 w-3 animate-spin text-accent-cyan" />
          <span className="font-medium text-accent-cyan">Syncing...</span>
          {timeString && (
            <>
              <span className="text-text-muted">•</span>
              <span className="text-text-muted">Updated {timeString}</span>
            </>
          )}
        </>
      )}

      {status === 'paused' && (
        <>
          <PauseCircle className="h-3 w-3 text-amber-400" />
          <span className="font-medium text-amber-400">Paused</span>
          <span className="text-text-muted">(Tab inactive)</span>
        </>
      )}

      {status === 'error' && (
        <>
          <AlertCircle className="h-3 w-3 text-amber-400" />
          <span className="font-medium text-amber-400">Connection issue</span>
          <span className="text-text-muted">•</span>
          <span className="text-text-muted">Retrying...</span>
        </>
      )}

      {status === 'idle' && (
        <>
          <CheckCircle2 className="h-3 w-3 text-text-muted" />
          <span className="text-text-muted">Static mode</span>
        </>
      )}
    </div>
  );
}
