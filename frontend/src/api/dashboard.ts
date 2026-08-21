import { api } from './client';
import type { DashboardSummary } from '../types';

export function fetchDashboardSummary(): Promise<DashboardSummary> {
  return api.get<DashboardSummary>('/api/dashboard/summary');
}
