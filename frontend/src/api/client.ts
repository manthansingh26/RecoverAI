/**
 * Configurable API client for RecoverAI backend.
 * Uses VITE_API_BASE_URL env var, defaults to empty string for proxy.
 *
 * Milestone 14A: every request sends the HttpOnly session cookie
 * (`credentials: 'include'`). Session tokens are NEVER stored in
 * localStorage/sessionStorage.
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '';

export interface ApiResponse<T> {
  data: T;
  ok: boolean;
}

export class ApiClientError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiClientError';
    this.status = status;
    this.detail = detail;
  }
}

let unauthorizedHandler: (() => void) | null = null;

/**
 * Register a handler invoked when any authenticated API call returns 401.
 * Used by the auth context to redirect to the login page when the session
 * expires or is invalid. The login endpoint itself is excluded (a 401 there
 * simply means bad credentials).
 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const res = await fetch(url, {
    // Send the HttpOnly session cookie with every request.
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
    ...options,
  });

  if (res.status === 401 && !path.startsWith('/api/auth/login')) {
    unauthorizedHandler?.();
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // ignore parse error
    }
    throw new ApiClientError(res.status, detail);
  }

  return res.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    }),
};
