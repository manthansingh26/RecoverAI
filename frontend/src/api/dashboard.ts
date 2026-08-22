import { api } from './client';
import type {
  ActivityFeed,
  DashboardAnalytics,
  DashboardSummary,
} from '../types';

export function fetchDashboardSummary(): Promise<DashboardSummary> {
  return api.get<DashboardSummary>('/api/dashboard/summary');
}

export function fetchDashboardAnalytics(): Promise<DashboardAnalytics> {
  return api.get<DashboardAnalytics>('/api/dashboard/analytics');
}

export function fetchDashboardActivity(limit: number = 20): Promise<ActivityFeed> {
  return api.get<ActivityFeed>(`/api/dashboard/activity?limit=${limit}`);
}
