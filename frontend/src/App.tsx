import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import DashboardPage from './pages/DashboardPage';
import RecoveryCasesPage from './pages/RecoveryCasesPage';
import RecoveryCaseDetailPage from './pages/RecoveryCaseDetailPage';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/recovery-cases" element={<RecoveryCasesPage />} />
          <Route
            path="/recovery-cases/:id"
            element={<RecoveryCaseDetailPage />}
          />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
