import { useState, useEffect, useCallback, useRef } from 'react';

export type PollingStatus = 'live' | 'refreshing' | 'paused' | 'error' | 'idle';

export interface UsePollingOptions<T> {
  intervalMs?: number;
  enabled?: boolean;
  pauseOnHidden?: boolean;
  onSuccess?: (data: T) => void;
  onError?: (error: string) => void;
}

export interface UsePollingReturn<T> {
  data: T | null;
  loading: boolean;
  isRefreshing: boolean;
  error: string | null;
  lastUpdated: Date | null;
  pollingStatus: PollingStatus;
  refetch: () => Promise<void>;
}

/**
 * Custom hook for smart, lightweight background polling.
 *
 * Features:
 * - In-flight lock: never overlaps requests
 * - Request generation counter: prevents race conditions and stale overwrites
 * - Visibility-aware: pauses when tab is hidden, immediately refreshes on return
 * - Non-disruptive: background refreshes do not trigger full-screen loading states
 * - Error resilient: preserves existing data on temporary network glitches
 * - Clean cleanup: avoids memory leaks and duplicate timers
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  options: UsePollingOptions<T> = {},
): UsePollingReturn<T> {
  const {
    intervalMs = 15000,
    enabled = true,
    pauseOnHidden = true,
  } = options;

  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [isTabHidden, setIsTabHidden] = useState<boolean>(
    typeof document !== 'undefined' ? document.visibilityState === 'hidden' : false,
  );

  const mountedRef = useRef<boolean>(true);
  const isFetchingRef = useRef<boolean>(false);
  const generationRef = useRef<number>(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // Execute a single fetch with generation lock
  const executeFetch = useCallback(async (isInitial: boolean = false): Promise<void> => {
    if (!mountedRef.current) return;
    if (isFetchingRef.current) return; // Prevent overlapping requests

    isFetchingRef.current = true;
    const currentGeneration = ++generationRef.current;

    if (isInitial) {
      setLoading(true);
    } else {
      setIsRefreshing(true);
    }

    try {
      const result = await fetcherRef.current();

      // Check if unmounted or if a newer request was dispatched
      if (!mountedRef.current || generationRef.current !== currentGeneration) {
        return;
      }

      setData(result);
      setError(null);
      setLastUpdated(new Date());
    } catch (err) {
      if (!mountedRef.current || generationRef.current !== currentGeneration) {
        return;
      }
      const message = err instanceof Error ? err.message : 'Failed to fetch data';
      setError(message);
      // Keep existing data intact on error
    } finally {
      if (mountedRef.current && generationRef.current === currentGeneration) {
        setLoading(false);
        setIsRefreshing(false);
        isFetchingRef.current = false;
      }
    }
  }, []);

  // Manual refetch
  const refetch = useCallback(async (): Promise<void> => {
    await executeFetch(false);
  }, [executeFetch]);

  // Handle visibility changes
  useEffect(() => {
    if (!pauseOnHidden || typeof document === 'undefined') return;

    const handleVisibilityChange = () => {
      const hidden = document.visibilityState === 'hidden';
      setIsTabHidden(hidden);

      if (!hidden && enabled && mountedRef.current) {
        // Tab just became visible: refresh immediately
        executeFetch(false);
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [pauseOnHidden, enabled, executeFetch]);

  // Initial fetch when dependencies change
  useEffect(() => {
    mountedRef.current = true;
    executeFetch(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  // Set up polling interval
  useEffect(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }

    if (enabled && !isTabHidden && intervalMs > 0) {
      intervalRef.current = setInterval(() => {
        if (mountedRef.current && !isFetchingRef.current && !isTabHidden) {
          executeFetch(false);
        }
      }, intervalMs);
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [enabled, isTabHidden, intervalMs, executeFetch]);

  // Track unmount
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, []);

  // Derive status
  let pollingStatus: PollingStatus = 'idle';
  if (!enabled) {
    pollingStatus = 'idle';
  } else if (isTabHidden) {
    pollingStatus = 'paused';
  } else if (error) {
    pollingStatus = 'error';
  } else if (isRefreshing || loading) {
    pollingStatus = 'refreshing';
  } else {
    pollingStatus = 'live';
  }

  return {
    data,
    loading: loading && data === null, // Only true before first data load
    isRefreshing,
    error,
    lastUpdated,
    pollingStatus,
    refetch,
  };
}
