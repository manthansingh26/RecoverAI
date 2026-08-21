import { NavLink, Outlet } from 'react-router-dom';
import {
  LayoutDashboard,
  FolderSearch,
  Shield,
  Activity,
  Menu,
  X,
} from 'lucide-react';
import { useState } from 'react';

const NAV_ITEMS = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/recovery-cases', icon: FolderSearch, label: 'Recovery Cases' },
];

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="flex h-screen overflow-hidden bg-bg-primary">
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/60 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={`
          fixed inset-y-0 left-0 z-40 w-64 flex-shrink-0
          border-r border-border bg-bg-secondary
          transform transition-transform duration-200 ease-in-out
          lg:relative lg:translate-x-0
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
        `}
      >
        <div className="flex h-full flex-col">
          {/* Brand */}
          <div className="flex h-16 items-center gap-3 border-b border-border px-5">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent-blue/15">
              <Shield className="h-5 w-5 text-accent-blue" />
            </div>
            <div>
              <h1 className="text-sm font-bold tracking-tight text-text-primary">
                RecoverAI
              </h1>
              <p className="text-[10px] font-medium uppercase tracking-widest text-text-muted">
                Recovery Ops
              </p>
            </div>
            <button
              className="ml-auto rounded p-1 text-text-muted hover:text-text-primary lg:hidden"
              onClick={() => setSidebarOpen(false)}
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          {/* Navigation */}
          <nav className="flex-1 space-y-1 px-3 py-4">
            {NAV_ITEMS.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === '/'}
                onClick={() => setSidebarOpen(false)}
                className={({ isActive }) =>
                  `flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors ${
                    isActive
                      ? 'bg-accent-blue/10 text-accent-blue'
                      : 'text-text-secondary hover:bg-bg-hover hover:text-text-primary'
                  }`
                }
              >
                <Icon className="h-4 w-4" />
                {label}
              </NavLink>
            ))}
          </nav>

          {/* Simulation Indicator */}
          <div className="border-t border-border px-4 py-4">
            <div className="flex items-center gap-2 rounded-lg bg-amber-500/10 px-3 py-2.5">
              <Activity className="h-4 w-4 text-amber-400" />
              <div>
                <p className="text-xs font-semibold text-amber-400">
                  Simulation Mode
                </p>
                <p className="text-[10px] text-amber-400/70">
                  No real financial actions
                </p>
              </div>
            </div>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Top bar */}
        <header className="flex h-16 items-center gap-4 border-b border-border bg-bg-secondary px-4 lg:px-6">
          <button
            className="rounded-lg p-2 text-text-muted hover:bg-bg-hover hover:text-text-primary lg:hidden"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu className="h-5 w-5" />
          </button>
          <div className="flex items-center gap-2 text-sm text-text-muted">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-500/10 px-2.5 py-1 text-[11px] font-medium text-amber-400">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
              SIMULATION
            </span>
            <span className="hidden text-text-muted sm:inline">•</span>
            <span className="hidden text-xs sm:inline">Dev Environment</span>
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto p-4 lg:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
