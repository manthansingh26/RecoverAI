import { api } from './client';
import type {
  RecoveryCaseListResponse,
  RecoveryCaseDetail,
  ExecutionLogsResponse,
  ReviewActionResponse,
  ExecutionResponse,
} from '../types';

export interface ListCasesParams {
  status?: string;
  strategy?: string;
  requires_human_approval?: boolean;
  approved_by_human?: boolean;
  page?: number;
  page_size?: number;
}

export function listRecoveryCases(
  params: ListCasesParams = {},
): Promise<RecoveryCaseListResponse> {
  const searchParams = new URLSearchParams();
  if (params.status) searchParams.set('status', params.status);
  if (params.strategy) searchParams.set('strategy', params.strategy);
  if (params.requires_human_approval !== undefined)
    searchParams.set(
      'requires_human_approval',
      String(params.requires_human_approval),
    );
  if (params.approved_by_human !== undefined)
    searchParams.set('approved_by_human', String(params.approved_by_human));
  if (params.page) searchParams.set('page', String(params.page));
  if (params.page_size) searchParams.set('page_size', String(params.page_size));

  const qs = searchParams.toString();
  return api.get<RecoveryCaseListResponse>(
    `/api/recovery-cases${qs ? `?${qs}` : ''}`,
  );
}

export function getRecoveryCase(
  recoveryCaseId: string,
): Promise<RecoveryCaseDetail> {
  return api.get<RecoveryCaseDetail>(
    `/api/recovery-cases/${recoveryCaseId}`,
  );
}

export function getExecutionLogs(
  recoveryCaseId: string,
  page: number = 1,
  pageSize: number = 20,
): Promise<ExecutionLogsResponse> {
  return api.get<ExecutionLogsResponse>(
    `/api/recovery-cases/${recoveryCaseId}/execution-logs?page=${page}&page_size=${pageSize}`,
  );
}

export function approveCase(
  recoveryCaseId: string,
): Promise<ReviewActionResponse> {
  return api.post<ReviewActionResponse>(
    `/api/recovery-cases/${recoveryCaseId}/approve`,
  );
}

export function rejectCase(
  recoveryCaseId: string,
): Promise<ReviewActionResponse> {
  return api.post<ReviewActionResponse>(
    `/api/recovery-cases/${recoveryCaseId}/reject`,
  );
}

export function executeCase(
  recoveryCaseId: string,
): Promise<ExecutionResponse> {
  return api.post<ExecutionResponse>(
    `/api/recovery-cases/${recoveryCaseId}/execute`,
  );
}
