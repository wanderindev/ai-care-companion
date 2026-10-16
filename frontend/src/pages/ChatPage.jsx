import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ChevronRightIcon, PlusIcon } from '@heroicons/react/24/outline'
import { relativeTime, useChatSessions } from '../lib/chat.js'
import { errorMessage } from '../lib/bitacora.js'
import { useNow } from '../lib/useNow.js'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'
import ExamplePrompts from '../components/chat/ExamplePrompts.jsx'
import SessionActions from '../components/chat/SessionActions.jsx'
import { useLongPress } from '../lib/useLongPress.js'

function SessionRow({ session, now }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const longPress = useLongPress(() => setMenuOpen(true))
  return (
    <li className="flex items-center gap-1 rounded-2xl border border-slate-200 bg-white pr-1">
      <Link
        to={`/chat/${session.id}`}
        {...longPress}
        className="flex min-h-16 min-w-0 flex-1 items-center gap-3 rounded-l-2xl px-4 py-3 select-none [-webkit-touch-callout:none] active:bg-slate-50"
      >
        <div className="min-w-0 flex-1">
          <p className={`truncate font-medium ${session.title ? 'text-slate-900' : 'text-slate-500'}`}>
            {session.title || 'Nueva conversación'}
          </p>
          <p className="mt-0.5 truncate text-sm text-slate-500">
            {relativeTime(session.last_message_at || session.created_at, now)}
            {session.created_by_name ? ` · iniciada por ${session.created_by_name}` : ''}
          </p>
        </div>
        {session.generating && (
          <span className="shrink-0 rounded-full bg-brand-50 px-2 py-0.5 text-xs font-medium text-brand-700">
            Respondiendo…
          </span>
        )}
        <ChevronRightIcon className="size-5 shrink-0 text-slate-400 lg:hidden" aria-hidden="true" />
      </Link>
      <SessionActions
        session={session}
        open={menuOpen}
        onOpenChange={setMenuOpen}
        label={`Opciones de «${session.title || 'Nueva conversación'}»`}
      />
    </li>
  )
}

const TABS = [
  { archived: false, label: 'Conversaciones' },
  { archived: true, label: 'Archivadas' },
]

/**
 * /chat: the family's shared conversations, most recent first;
 * `?archivadas=1` lists the archived ones («Archivadas»). Each row has a «⋮»
 * menu (also a long press on phones): archive / restore, delete.
 */
export default function ChatPage() {
  const navigate = useNavigate()
  const now = useNow()
  const [params, setParams] = useSearchParams()
  const archived = params.get('archivadas') === '1'
  const query = useChatSessions({ archived })
  const sessions = query.data?.pages.flatMap((p) => p.items) ?? []

  const newChat = (prompt) => navigate('/chat/nueva', prompt ? { state: { prompt } } : undefined)

  return (
    <section className="mx-auto max-w-3xl">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Chat</h1>
        <button
          type="button"
          onClick={() => newChat()}
          className="flex min-h-11 items-center gap-1.5 rounded-2xl bg-brand-700 px-4 font-semibold text-white active:bg-brand-800"
        >
          <PlusIcon className="size-5" aria-hidden="true" />
          Nueva conversación
        </button>
      </div>
      <p className="mt-1 text-sm text-slate-500">Las conversaciones son compartidas por toda la familia.</p>

      <div role="group" aria-label="Mostrar" className="mt-4 inline-flex rounded-2xl bg-slate-100 p-1">
        {TABS.map((tab) => (
          <button
            key={tab.label}
            type="button"
            aria-pressed={archived === tab.archived}
            onClick={() => setParams(tab.archived ? { archivadas: '1' } : {}, { replace: true })}
            className={`min-h-11 rounded-xl px-4 text-sm font-medium ${
              archived === tab.archived ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="mt-4">
        {query.isPending ? (
          <CardSkeletons count={4} />
        ) : query.isError ? (
          <ErrorState
            message={errorMessage(query.error, 'No se pudieron cargar las conversaciones.')}
            onRetry={() => query.refetch()}
          />
        ) : sessions.length === 0 && archived ? (
          <p className="rounded-2xl border border-dashed border-slate-300 px-4 py-6 text-center text-slate-500">
            No hay conversaciones archivadas.
          </p>
        ) : sessions.length === 0 ? (
          <ExamplePrompts title="Todavía no hay conversaciones" onPick={newChat} />
        ) : (
          <>
            <ul className="space-y-2">
              {sessions.map((s) => (
                <SessionRow key={s.id} session={s} now={now} />
              ))}
            </ul>
            {query.hasNextPage && (
              <button
                type="button"
                onClick={() => query.fetchNextPage()}
                disabled={query.isFetchingNextPage}
                className="mt-4 min-h-12 w-full rounded-2xl bg-white font-medium text-brand-700 ring-1 ring-slate-200 active:bg-slate-50 disabled:opacity-60"
              >
                {query.isFetchingNextPage ? 'Cargando…' : 'Cargar más'}
              </button>
            )}
          </>
        )}
      </div>
    </section>
  )
}
