import { NavLink, Outlet } from 'react-router'
import { useReadiness } from '../api/hooks'

function ApiStatus() {
  const { data, isPending, isError } = useReadiness()
  const online = !isError && data?.status === 'ok'
  const label = isPending ? 'Checking API…' : online ? 'API online' : 'API offline'
  const dot = isPending ? 'bg-slate-500' : online ? 'bg-emerald-500' : 'bg-red-500'
  return (
    <span className="flex items-center gap-2 text-xs text-slate-400" role="status">
      <span className={`h-2 w-2 rounded-full ${dot}`} />
      {label}
    </span>
  )
}

const navClass = ({ isActive }: { isActive: boolean }) =>
  `rounded px-3 py-1.5 text-sm ${isActive ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-white'}`

export function Layout() {
  return (
    <div className="min-h-screen">
      <header className="flex items-center justify-between border-b border-slate-800 px-6 py-3">
        <div className="flex items-center gap-6">
          <span className="font-semibold tracking-tight">ReleaseRadar</span>
          <nav className="flex gap-1">
            <NavLink to="/" end className={navClass}>
              Comparator
            </NavLink>
            <span className="cursor-not-allowed px-3 py-1.5 text-sm text-slate-600" title="Phase 2">
              Daily Radar
            </span>
          </nav>
        </div>
        <ApiStatus />
      </header>
      <main className="mx-auto max-w-6xl p-6">
        <Outlet />
      </main>
    </div>
  )
}
