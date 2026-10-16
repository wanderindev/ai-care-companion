import logo from '../../assets/brand/logo.svg'

/** Centered card used by the sign-in and link-verification screens. */
export default function AuthScreen({ children }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <img src={logo} alt="n16chat" className="mx-auto h-14 w-auto" />
          <p className="mt-3 text-sm text-slate-500">Salud de Papá y Mamá</p>
        </div>
        <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">{children}</div>
      </div>
    </div>
  )
}
