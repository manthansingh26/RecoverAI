/**
 * Auth context — bootstraps the authenticated operator on load, exposes
 * login/logout, and notifies the router when the session expires (401).
 *
 * The session lives ONLY in the HttpOnly cookie; nothing is stored in
 * localStorage/sessionStorage.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { api, setUnauthorizedHandler } from '../api/client';
import type { Operator, OperatorRole } from '../types';

interface AuthContextValue {
  user: Operator | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<Operator>;
  logout: () => Promise<void>;
  /** True when the current operator holds one of the given roles. */
  hasRole: (roles: OperatorRole[]) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Operator | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  // Bootstrap auth state from the server-side session on first load.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const me = await api.get<Operator>('/api/auth/me');
        if (!cancelled) setUser(me);
      } catch {
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Any later 401 (session expired/invalidated) redirects to login.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      navigate('/login');
    });
    return () => setUnauthorizedHandler(null);
  }, [navigate]);

  const login = useCallback(async (email: string, password: string) => {
    const me = await api.post<Operator>('/api/auth/login', { email, password });
    setUser(me);
    return me;
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post('/api/auth/logout');
    } finally {
      // Clear local state regardless of network outcome.
      setUser(null);
    }
  }, []);

  const hasRole = useCallback(
    (roles: OperatorRole[]) => {
      if (!user) return false;
      return roles.includes(user.role);
    },
    [user],
  );

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, hasRole }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
