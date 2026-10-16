import { Link } from 'react-router-dom'

export default function NotFoundPage() {
  return (
    <section className="py-12 text-center">
      <h1 className="text-2xl font-semibold">Página no encontrada</h1>
      <Link
        to="/bitacora"
        className="mt-6 inline-flex min-h-12 items-center rounded-full bg-brand-700 px-6 font-medium text-white"
      >
        Ir a la Bitácora
      </Link>
    </section>
  )
}
