import cloud from '../../assets/brand/cloud.svg'
import logo from '../../assets/brand/logo.svg'
import PatientSwitcher from './PatientSwitcher.jsx'
import PendingBadge from './PendingBadge.jsx'
import UserMenu from './UserMenu.jsx'

export default function AppHeader() {
  return (
    <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 pt-[env(safe-area-inset-top)] backdrop-blur">
      <div className="mx-auto flex max-w-3xl items-center lg:max-w-6xl justify-between gap-2 px-4 py-2">
        {/* A phone has room for the cloud mark only (the switcher and menu take
            ~300 px of 360); the landscape logo from sm up. Both shrink, keeping
            their proportions, before the switcher and menu do. */}
        <img
          src={cloud}
          alt="n16chat"
          className="h-9 w-auto min-w-0 shrink object-contain object-left sm:hidden"
        />
        <img
          src={logo}
          alt="n16chat"
          className="hidden h-8 w-auto min-w-0 shrink object-contain object-left sm:block"
        />
        <div className="flex shrink-0 items-center gap-1">
          <PatientSwitcher />
          <PendingBadge />
          <UserMenu />
        </div>
      </div>
    </header>
  )
}
