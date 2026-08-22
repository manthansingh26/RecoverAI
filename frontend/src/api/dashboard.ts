import { api } from './client';
import type { DashboardSummary, DashboardAnalytics } from '../types';

export function fetchDashboardSummary(): Promise<DashboardSummary> {
  return api.get<DashboardSummary>('/api/dashboard/summary');
}

export function fetchDashboardAnalytics(): Promise<DashboardAnalytics> {
  return api.get<DashboardAnalytics>('/api/dashboard/analytics');
}
