import { Outlet, ScrollRestoration } from 'react-router-dom'
import { PatientProvider } from '../../contexts/PatientProvider.jsx'
import AppHeader from './AppHeader.jsx'
import BottomNav from './BottomNav.jsx'

export default function AppLayout() {
  return (
    // Inside RequireAuth: the provider loads GET /patients for signed-in users only.
    <PatientProvider>
      <div className="flex min-h-dvh flex-col">
        <AppHeader />
        {/* Bottom padding keeps content clear of the fixed bottom nav + safe area. */}
        <main className="mx-auto w-full max-w-3xl flex-1 px-4 lg:max-w-6xl pt-4 pb-[calc(5rem+env(safe-area-inset-bottom))]">
          <Outlet />
        </main>
        <BottomNav />
        {/* New pages start at the top; back restores the list's scroll position. */}
        <ScrollRestoration />
      </div>
    </PatientProvider>
  )
}
