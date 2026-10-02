import { useState } from 'react'
import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/store/authStore'
import { Button } from '@/components/ui/button'
import {
  LayoutDashboard, Terminal, BrainCircuit, History, Settings,
  LogOut, Zap, Users, BarChart3, FlaskConical, Menu, X, FileText
} from 'lucide-react'
import { cn } from '@/utils/utils'
import TradingHaltBanner from '@/components/TradingHaltBanner'

const navItems = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/terminal', icon: Terminal, label: 'Terminal' },
  { to: '/ai-analyst', icon: BrainCircuit, label: 'AI Analyst' },
  { to: '/historical-lab', icon: BarChart3, label: 'Historical Lab' },
  { to: '/backtest', icon: FlaskConical, label: 'Backtesting' },
  { to: '/autopilot', icon: Zap, label: 'Autopilot' },
  { to: '/reports', icon: FileText, label: 'Reports' },
  { to: '/history', icon: History, label: 'History' },
  { to: '/settings', icon: Settings, label: 'Settings' },
  { to: '/users', icon: Users, label: 'Users', adminOnly: true },
]

export default function DashboardLayout() {
  const { user, logout } = useAuthStore()
  const navigate = useNavigate()
  const [mobileOpen, setMobileOpen] = useState(false)

  const handleLogout = () => {
    logout()
    navigate('/login')
  }

  const navContent = (
    <nav className="flex-1 overflow-y-auto p-2 md:p-4 space-y-1">
      {navItems.map((item) => {
        if (item.adminOnly && user?.role !== 'admin') return null
        return (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
            onClick={() => setMobileOpen(false)}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 px-3 md:px-4 py-2.5 md:py-3 rounded-lg transition-colors text-sm md:text-base",
                isActive
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground"
              )
            }
          >
            <item.icon className="w-4 h-4 md:w-5 md:h-5 shrink-0" />
            <span className="font-medium truncate">{item.label}</span>
          </NavLink>
        )
      })}
    </nav>
  )

  return (
    <div className="min-h-screen flex flex-col md:flex-row bg-background">
      {/* Mobile header */}
      <div className="md:hidden flex items-center justify-between p-3 border-b border-border bg-card sticky top-0 z-50">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center">
            <span className="text-sm">📈</span>
          </div>
          <span className="font-bold text-sm">Finance Engine</span>
        </div>
        <Button variant="ghost" size="sm" onClick={() => setMobileOpen(!mobileOpen)}>
          {mobileOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
        </Button>
      </div>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div className="md:hidden fixed inset-0 top-12 z-40 bg-background/95 backdrop-blur-sm flex flex-col">
          <div className="flex-1 flex flex-col p-4">
            {navContent}
            <div className="border-t border-border pt-4 mt-auto">
              <div className="flex items-center gap-3 mb-3 px-3">
                <div className="w-9 h-9 rounded-full bg-muted flex items-center justify-center shrink-0">
                  <span className="text-sm">👤</span>
                </div>
                <div className="min-w-0">
                  <p className="font-medium text-sm truncate">{user?.name || 'User'}</p>
                  <p className="text-xs text-muted-foreground truncate">@{user?.username}</p>
                </div>
              </div>
              <Button variant="ghost" className="w-full justify-start text-muted-foreground hover:text-destructive text-sm" onClick={handleLogout}>
                <LogOut className="w-4 h-4 mr-2 shrink-0" />
                Sign Out
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Desktop sidebar */}
      <aside className="hidden md:flex w-56 lg:w-64 border-r border-border bg-card flex-col shrink-0">
        <div className="p-4 lg:p-6 border-b border-border">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 lg:w-10 lg:h-10 rounded-xl bg-primary/10 border border-primary/20 flex items-center justify-center shrink-0">
              <span className="text-lg lg:text-xl">📈</span>
            </div>
            <div className="min-w-0">
              <h1 className="font-bold text-base lg:text-lg truncate">Finance Engine</h1>
              <p className="text-[10px] lg:text-xs text-muted-foreground">v2.0</p>
            </div>
          </div>
        </div>
        {navContent}
        <div className="p-3 lg:p-4 border-t border-border mt-auto">
          <div className="flex items-center gap-2 lg:gap-3 mb-3 px-2 lg:px-3">
            <div className="w-8 h-8 lg:w-9 lg:h-9 rounded-full bg-muted flex items-center justify-center shrink-0">
              <span className="text-xs lg:text-sm">👤</span>
            </div>
            <div className="min-w-0">
              <p className="font-medium text-xs lg:text-sm truncate">{user?.name || 'User'}</p>
              <p className="text-[10px] lg:text-xs text-muted-foreground truncate">@{user?.username}</p>
            </div>
          </div>
          <Button variant="ghost" className="w-full justify-start text-muted-foreground hover:text-destructive text-xs lg:text-sm" onClick={handleLogout}>
            <LogOut className="w-3.5 h-3.5 lg:w-4 lg:h-4 mr-2 shrink-0" />
            Sign Out
          </Button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto min-h-0">
        <TradingHaltBanner />
        <Outlet />
      </main>
    </div>
  )
}
