import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import RequireAuth from './components/RequireAuth';
import DashboardPage from './pages/DashboardPage';
import RecoveryCasesPage from './pages/RecoveryCasesPage';
import RecoveryCaseDetailPage from './pages/RecoveryCaseDetailPage';
import LoginPage from './pages/LoginPage';
import { AuthProvider } from './auth/AuthContext';

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            element={
              <RequireAuth>
                <Layout />
              </RequireAuth>
            }
          >
            <Route path="/" element={<DashboardPage />} />
            <Route path="/recovery-cases" element={<RecoveryCasesPage />} />
            <Route
              path="/recovery-cases/:id"
              element={<RecoveryCaseDetailPage />}
            />
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
