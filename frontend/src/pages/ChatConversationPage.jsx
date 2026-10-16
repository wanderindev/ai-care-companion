import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ArrowDownIcon, ChevronLeftIcon } from '@heroicons/react/24/outline'
import { ApiError } from '../lib/api.js'
import {
  CONNECTION_LOST,
  GENERATING_POLL_MS,
  SESSIONS_KEY,
  groupMessages,
  sendErrorMessage,
  sessionKey,
  streamChatMessage,
  useChatSession,
  useCreateChatSession,
} from '../lib/chat.js'
import { useMe } from '../lib/auth.js'
import { SUGGESTIONS_KEY } from '../lib/suggestions.js'
import { useIsDesktop } from '../lib/useMediaQuery.js'
import { CardSkeletons, ErrorState } from '../components/bitacora/ListStates.jsx'
import { AnswerBlock, PromptBubble, StreamingAnswer } from '../components/chat/ChatMessages.jsx'
import Composer from '../components/chat/Composer.jsx'
import ExamplePrompts from '../components/chat/ExamplePrompts.jsx'
import SessionActions from '../components/chat/SessionActions.jsx'

const NEW = 'nueva'
/** Streamed text is rendered (markdown re-parsed) at most this often. */
const RENDER_EVERY_MS = 80
const NEAR_BOTTOM_PX = 120

function isNearBottom() {
  const doc = document.documentElement
  return window.innerHeight + window.scrollY >= doc.scrollHeight - NEAR_BOTTOM_PX
}

function scrollToBottom(behavior = 'auto') {
  window.scrollTo({ top: document.documentElement.scrollHeight, behavior })
}

/**
 * /chat/:sessionId (and /chat/nueva, which becomes /chat/<id> on the first
 * message without remounting, so the stream carries on).
 *
 * While this page streams a turn it shows the stored messages up to the
 * moment of sending, then the prompt and the live answer; when the stream
 * ends everything is re-rendered from the stored session (DESIGN §6.6).
 */
export default function ChatConversationPage() {
  const { sessionId: param } = useParams()
  const sessionId = param === NEW ? null : param
  const location = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const isDesktop = useIsDesktop()
  const me = useMe().data

  const [draft, setDraft] = useState(() => location.state?.prompt ?? '')
  const [sendError, setSendError] = useState('')
  const [stream, setStream] = useState(null)
  const [composerHeight, setComposerHeight] = useState(112)
  const [showJump, setShowJump] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const textRef = useRef('')
  const breakRef = useRef(false)
  const flushTimer = useRef(null)
  const abortRef = useRef(null)
  const stickRef = useRef(true)
  // Set right before /chat/nueva becomes /chat/<id>: that change keeps the stream.
  const continuingRef = useRef(false)

  // Another conversation (from one real id to another): start clean. The
  // stream of the previous one is aborted by the effect below.
  const [shownParam, setShownParam] = useState(param)
  if (shownParam !== param) {
    setShownParam(param)
    if (shownParam !== NEW) {
      setStream(null)
      setDraft('')
      setSendError('')
    }
  }

  // While this page streams a turn the session is not polled (the stream is
  // the live view); after a dropped stream it is, like any other turn.
  const query = useChatSession(sessionId, { poll: !stream || stream.phase === 'lost' })
  const createSession = useCreateChatSession()
  const session = query.data
  const stored = session?.messages ?? []

  // A dropped stream ends once a re-read shows the turn finished.
  const settled =
    stream?.phase === 'lost' && query.dataUpdatedAt > stream.lostAt && !session?.generating
  const active = stream && !settled

  // Leaving the page or switching conversations stops listening (the server
  // finishes the turn anyway); /chat/nueva → /chat/<id> keeps the stream.
  useEffect(
    () => () => {
      if (continuingRef.current) {
        continuingRef.current = false
        return
      }
      abortRef.current?.abort()
    },
    [sessionId],
  )
  useEffect(() => () => abortRef.current?.abort(), [])
  useEffect(() => () => clearTimeout(flushTimer.current), [])

  // After a dropped connection: re-read until the server says it is done.
  useEffect(() => {
    if (!active || stream.phase !== 'lost' || sessionId == null) return
    const timer = setInterval(
      () => queryClient.invalidateQueries({ queryKey: sessionKey(sessionId) }),
      GENERATING_POLL_MS,
    )
    return () => clearInterval(timer)
  }, [active, stream?.phase, sessionId, queryClient])

  // Follow new content unless the reader scrolled up.
  useEffect(() => {
    function onScroll() {
      const near = isNearBottom()
      stickRef.current = near
      setShowJump(!near)
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useLayoutEffect(() => {
    if (stickRef.current) scrollToBottom()
  }, [stream?.text, stream?.activity, stream?.error, stored.length, composerHeight])

  const flush = useCallback(() => {
    clearTimeout(flushTimer.current)
    flushTimer.current = null
    const text = textRef.current
    setStream((s) => (s && s.text !== text ? { ...s, text } : s))
  }, [])

  function onEvent({ event, data }, id) {
    if (event === 'delta') {
      if (breakRef.current && textRef.current) textRef.current += '\n\n'
      breakRef.current = false
      textRef.current += data.text ?? ''
      setStream((s) => (s && s.activity ? { ...s, activity: null } : s))
      if (!flushTimer.current) flushTimer.current = setTimeout(flush, RENDER_EVERY_MS)
    } else if (event === 'tool') {
      flush()
      breakRef.current = true
      setStream((s) => s && { ...s, activity: data.status || 'Consultando los datos…' })
    } else if (event === 'file') {
      // A generated PDF / Excel: a chip right away (the stored session lists it too).
      setStream((s) => s && { ...s, files: [...(s.files ?? []), data] })
    } else if (event === 'suggestion') {
      // A proposal waiting in «Pendientes»: a card right away, and a fresh badge count.
      setStream((s) => s && { ...s, suggestions: [...(s.suggestions ?? []), data] })
      queryClient.invalidateQueries({ queryKey: SUGGESTIONS_KEY })
    } else if (event === 'error') {
      // There may be two (e.g. `deadline` then `lock_lost`): the last one counts.
      setStream((s) => s && { ...s, error: data.detail || 'No se pudo completar la respuesta.' })
    } else if (event === 'title') {
      queryClient.setQueryData(sessionKey(id), (old) => old && { ...old, title: data.title })
      queryClient.invalidateQueries({ queryKey: SESSIONS_KEY })
    }
    // `message` / `done`: the stored session is re-read once the stream ends.
  }

  async function send(text) {
    if (active || !text.trim()) return
    setSendError('')
    textRef.current = ''
    breakRef.current = false
    stickRef.current = true
    const baseline = stored.reduce((max, m) => Math.max(max, m.id), 0)
    setStream({ prompt: text, baseline, text: '', activity: 'Pensando…', error: '', phase: 'streaming' })
    setDraft('')

    let id = sessionId
    let started = false
    // Created first, so leaving during the session create also cancels.
    const controller = new AbortController()
    abortRef.current = controller
    try {
      if (id == null) {
        id = String((await createSession.mutateAsync()).id)
        // Left (or switched) meanwhile: the empty session stays, nothing is sent.
        if (controller.signal.aborted) return
        continuingRef.current = true
        navigate(`/chat/${id}`, { replace: true, preventScrollReset: true })
      }
      await streamChatMessage(id, text, {
        signal: controller.signal,
        onOpen: () => {
          started = true
        },
        onEvent: (e) => onEvent(e, id),
      })
      flush()
      setStream((s) => s && { ...s, activity: null, phase: 'settling' })
      await queryClient.invalidateQueries({ queryKey: sessionKey(id) })
      queryClient.invalidateQueries({ queryKey: SESSIONS_KEY })
      if (!controller.signal.aborted) setStream(null)
    } catch (error) {
      if (error?.name === 'AbortError' || controller.signal.aborted) return
      flush()
      if (!started) {
        // Not accepted (no 200): give the text back to edit or resend.
        setStream(null)
        setDraft(text)
        setSendError(sendErrorMessage(error))
        if (id != null) queryClient.invalidateQueries({ queryKey: sessionKey(id) })
        if (error instanceof ApiError && error.status === 409) {
          queryClient.invalidateQueries({ queryKey: SESSIONS_KEY })
        }
        return
      }
      // The server finishes the turn anyway: show it once it is stored.
      setStream(
        (s) =>
          s && { ...s, activity: null, error: CONNECTION_LOST, phase: 'lost', lostAt: Date.now() },
      )
      setTimeout(() => queryClient.invalidateQueries({ queryKey: sessionKey(id) }), 2000)
      queryClient.invalidateQueries({ queryKey: SESSIONS_KEY })
    }
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  if (sessionId != null && query.isError) {
    const gone = query.error?.status === 404
    return (
      <section className="mx-auto max-w-3xl">
        <BackLink />
        <div className="mt-4">
          <ErrorState
            message={gone ? 'Esta conversación ya no existe.' : 'No se pudo cargar la conversación.'}
            onRetry={gone ? undefined : () => query.refetch()}
          />
        </div>
      </section>
    )
  }

  const visible = active ? stored.filter((m) => m.id <= stream.baseline) : stored
  const blocks = groupMessages(visible)
  const lastBlock = blocks[blocks.length - 1]
  const generatingElsewhere = !active && session?.generating
  const busy = Boolean(active) || Boolean(generatingElsewhere)

  return (
    <section className="mx-auto max-w-3xl">
      <BackLink />
      <header className="mt-2 flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold [overflow-wrap:anywhere]">
            {session?.title || 'Nueva conversación'}
          </h1>
          {session?.created_by_name && (
            <p className="text-sm text-slate-500">Iniciada por {session.created_by_name}</p>
          )}
        </div>
        {session && sessionId != null && !active && (
          <SessionActions
            session={session}
            open={menuOpen}
            onOpenChange={setMenuOpen}
            onDeleted={() => navigate(session.archived_at ? '/chat?archivadas=1' : '/chat', { replace: true })}
          />
        )}
      </header>
      {session?.archived_at && (
        <p className="mt-3 rounded-2xl bg-amber-50 px-4 py-3 text-sm text-amber-900">
          Conversación archivada: no aparece en la lista principal. Puedes restaurarla desde el menú «⋮».
        </p>
      )}

      <div className="mt-4 space-y-5">
        {sessionId != null && query.isPending ? (
          <CardSkeletons count={2} />
        ) : blocks.length === 0 && !active ? (
          <ExamplePrompts onPick={(p) => setDraft(p)} />
        ) : null}

        {blocks.map((block, i) => {
          if (block.type === 'prompt') {
            const m = block.message
            return (
              <PromptBubble
                key={block.key}
                text={m.text}
                author={m.author_name}
                createdAt={m.created_at}
                mine={me != null && m.author_user_id === me.id}
              />
            )
          }
          const prev = blocks[i - 1]
          const canRetry =
            block === lastBlock && !busy && block.status === 'interrupted' && prev?.type === 'prompt'
          return (
            <AnswerBlock
              key={block.key}
              block={block}
              onRetry={canRetry ? () => send(prev.message.text) : undefined}
            />
          )
        })}

        {active && (
          <>
            <PromptBubble text={stream.prompt} mine />
            <StreamingAnswer
              text={stream.text}
              activity={stream.activity}
              error={stream.error}
              files={stream.files}
              suggestions={stream.suggestions}
              live={stream.phase === 'streaming'}
            />
          </>
        )}
        {generatingElsewhere && <StreamingAnswer activity="Generando una respuesta…" />}
      </div>

      <div aria-hidden="true" style={{ height: composerHeight }} />

      {showJump && (
        <button
          type="button"
          onClick={() => {
            stickRef.current = true
            scrollToBottom('smooth')
          }}
          style={{ bottom: `calc(${composerHeight + 8}px + 4rem + env(safe-area-inset-bottom))` }}
          className="fixed left-1/2 z-30 flex min-h-10 -translate-x-1/2 items-center gap-1 rounded-full bg-white px-4 text-sm font-medium text-brand-700 shadow-md ring-1 ring-slate-200 active:bg-slate-50"
        >
          <ArrowDownIcon className="size-4" aria-hidden="true" />
          Ir al final
        </button>
      )}

      <Composer
        value={draft}
        onChange={(v) => {
          setDraft(v)
          if (sendError) setSendError('')
        }}
        onSend={send}
        disabled={busy}
        autoFocus={isDesktop && sessionId == null}
        onHeight={setComposerHeight}
        hint={
          sendError ? (
            <span role="alert" className="text-red-700">
              {sendError}
            </span>
          ) : busy ? (
            'Espera a que termine la respuesta para enviar otro mensaje.'
          ) : null
        }
      />
    </section>
  )
}

function BackLink() {
  return (
    <Link
      to="/chat"
      className="-ml-2 inline-flex min-h-11 items-center gap-1 rounded-xl px-2 text-sm font-medium text-brand-700 active:bg-brand-50"
    >
      <ChevronLeftIcon className="size-4" aria-hidden="true" />
      Conversaciones
    </Link>
  )
}
