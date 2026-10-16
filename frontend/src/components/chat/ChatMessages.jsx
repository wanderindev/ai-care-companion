import { memo } from 'react'
import {
  ArrowPathIcon,
  CheckIcon,
  ExclamationTriangleIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline'
import Markdown from '../bitacora/Markdown.jsx'
import AttachmentChip from './AttachmentChip.jsx'
import ProposalCard from './ProposalCard.jsx'

const TIME_FMT = new Intl.DateTimeFormat('es-PA', {
  day: 'numeric',
  month: 'short',
  hour: 'numeric',
  minute: '2-digit',
  timeZone: 'America/Panama',
})

/** A family member's message, labeled with who wrote it. */
export function PromptBubble({ text, author, createdAt, mine }) {
  return (
    <div className="flex flex-col items-end">
      <p className="mb-1 px-1 text-xs text-slate-500">
        <span className="font-medium text-slate-700">{mine ? 'Tú' : author || 'Alguien'}</span>
        {createdAt ? ` · ${TIME_FMT.format(new Date(createdAt))}` : ''}
      </p>
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tr-md bg-brand-700 px-4 py-2.5 text-white [overflow-wrap:anywhere]">
        {text}
      </div>
    </div>
  )
}

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block size-3.5 animate-spin rounded-full border-2 border-brand-100 border-t-brand-700"
    />
  )
}

/** Tools the answer used (done) — the status texts, never the data. */
function ToolList({ tools }) {
  if (!tools?.length) return null
  return (
    <ul className="mb-2 flex flex-wrap gap-1.5">
      {tools.map((status) => (
        <li
          key={status}
          className="flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-600"
        >
          <CheckIcon className="size-3.5 text-emerald-600" aria-hidden="true" />
          {status.replace(/…$/, '')}
        </li>
      ))}
    </ul>
  )
}

function AnswerShell({ children }) {
  return (
    <div className="flex gap-2">
      <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-brand-50 text-brand-700">
        <SparklesIcon className="size-4" aria-hidden="true" />
      </span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}

/** Markdown is memoized: a stored answer is not re-parsed on every stream tick. */
const AnswerText = memo(function AnswerText({ text }) {
  return <Markdown gfm className="text-slate-900">{text}</Markdown>
})

/** Changes the answer proposed: cards linking to «Pendientes». */
function Proposals({ items }) {
  if (!items?.length) return null
  return (
    <div className="mt-3 flex flex-col items-start gap-2">
      {items.map((s) => (
        <ProposalCard key={s.id} suggestion={s} />
      ))}
    </div>
  )
}

/** A stored answer (one or more assistant rows of a turn). */
export function AnswerBlock({ block, onRetry }) {
  const interrupted = block.status === 'interrupted'
  return (
    <AnswerShell>
      <ToolList tools={block.tools} />
      {block.text ? <AnswerText text={block.text} /> : null}
      {block.attachments.length > 0 && (
        <div className="mt-3 flex flex-col items-start gap-2">
          {block.attachments.map((a) => (
            <AttachmentChip key={a.id} attachment={a} />
          ))}
        </div>
      )}
      <Proposals items={block.suggestions} />
      {interrupted && (
        <div className="mt-3 rounded-xl bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <p className="flex items-center gap-1.5 font-medium">
            <ExclamationTriangleIcon className="size-4" aria-hidden="true" />
            Respuesta incompleta
          </p>
          {block.error && <p className="mt-0.5">{block.error}</p>}
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="mt-2 flex min-h-11 items-center gap-1.5 rounded-xl bg-white px-3 font-medium text-amber-900 ring-1 ring-amber-300 active:bg-amber-100"
            >
              <ArrowPathIcon className="size-4" aria-hidden="true" />
              Reintentar
            </button>
          )}
        </div>
      )}
    </AnswerShell>
  )
}

/** The answer being streamed right now (or generated elsewhere, without text). */
export function StreamingAnswer({ text, activity, error, files, suggestions, live = false }) {
  return (
    <AnswerShell>
      {text ? <Markdown gfm className="text-slate-900">{text}</Markdown> : null}
      {files?.length > 0 && (
        <div className="mt-3 flex flex-col items-start gap-2">
          {files.map((a) => (
            <AttachmentChip key={a.id} attachment={a} />
          ))}
        </div>
      )}
      <Proposals items={suggestions} />
      {live && text && !activity && (
        <span
          aria-label="Escribiendo…"
          role="status"
          className="mt-1 inline-block size-2.5 animate-pulse rounded-full bg-brand-600"
        />
      )}
      {activity && (
        <p
          role="status"
          className={`flex items-center gap-2 text-sm text-slate-500 ${text ? 'mt-2' : 'mt-1'}`}
        >
          <Spinner />
          {activity}
        </p>
      )}
      {error && (
        <p role="alert" className="mt-3 rounded-xl bg-amber-50 px-3 py-2 text-sm text-amber-900">
          {error}
        </p>
      )}
    </AnswerShell>
  )
}
