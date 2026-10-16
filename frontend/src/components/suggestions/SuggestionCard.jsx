import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { CheckIcon, MegaphoneIcon, PencilSquareIcon, XMarkIcon } from '@heroicons/react/20/solid'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import EntrySheet from '../bitacora/EntrySheet.jsx'
import AppointmentSheet from '../calendar/AppointmentSheet.jsx'
import DoctorSheet from '../doctors/DoctorSheet.jsx'
import MedItemSheet from '../meds/MedItemSheet.jsx'
import SaveSheet from '../meds/SaveSheet.jsx'
import ProposedRecord from './ProposedRecord.jsx'
import SourceMessages from './SourceMessages.jsx'
import { ApiError } from '../../lib/api.js'
import { errorMessage, formatDate } from '../../lib/bitacora.js'
import { useLogEntry } from '../../lib/logEntries.js'
import { SLOTS, useMeds } from '../../lib/meds.js'
import { patientColor } from '../../lib/patients.js'
import { panamaToday } from '../../lib/appointments.js'
import {
  KIND_LABELS,
  SOURCE_LABELS,
  previewSuggestion,
  useApproveSuggestion,
  useRejectSuggestion,
} from '../../lib/suggestions.js'

const MED_ITEM_FIELDS = [
  'name',
  'strength',
  'quantity',
  'dose_note',
  'slot',
  'time',
  'days',
  'schedule_note',
  'instructions',
  'prescriber_id',
  'reason',
  'start_date',
  'end_date',
]

const actionClass =
  'flex min-h-12 items-center justify-center gap-2 rounded-2xl px-4 font-semibold disabled:opacity-60'

/** Edit an entry change: the current entry with the proposed changes laid over it. */
function UpdateEntryEdit({ suggestion, approve, onClose, onDone }) {
  const { data: entry, error } = useLogEntry(suggestion.payload.entry_id)
  if (error) {
    return (
      <ConfirmDialog
        title="No se pudo abrir la entrada"
        message={errorMessage(error, 'No se pudo cargar la entrada.')}
        confirmLabel="Cerrar"
        onConfirm={onClose}
        onCancel={onClose}
      />
    )
  }
  if (!entry) return null
  return (
    <EntrySheet
      entry={entry}
      prefill={suggestion.payload.changes}
      title="Editar y aprobar"
      submitLabel="Aprobar"
      submit={(changes) => {
        if (!Object.keys(changes).length) {
          const detail = 'No queda ningún cambio para aplicar.'
          return Promise.reject(new ApiError(400, detail, { detail }))
        }
        return approve({ payload: { entry_id: entry.id, changes } })
      }}
      onClose={onClose}
      onSaved={onDone}
    />
  )
}

/** Only the item fields that differ from `base` (all set ones for a new item). */
function itemPatch(draft, base) {
  const patch = {}
  for (const key of MED_ITEM_FIELDS) {
    const value = draft[key] ?? null
    if (base ? JSON.stringify(value) !== JSON.stringify(base[key] ?? null) : value !== null) {
      patch[key] = value
    }
  }
  return patch
}

/**
 * A medication change: optionally edit the item (MedItemSheet), then the
 * dry-run diff of the full-list save (SaveSheet) and approve with the
 * previewed version, so a list saved in between is caught (409).
 */
function MedChangeFlow({ suggestion, edit, approve, onClose, onDone }) {
  const { data: list } = useMeds(suggestion.patient_id)
  const [payload, setPayload] = useState(edit ? null : suggestion.payload)
  const [saveError, setSaveError] = useState(null)
  const [busy, setBusy] = useState(false)
  // A dry run (nothing is saved), recomputed for an edited proposal.
  const previewQuery = useQuery({
    queryKey: ['suggestion-preview', suggestion.id, payload],
    queryFn: () => previewSuggestion(suggestion.id, edit ? payload : undefined),
    enabled: Boolean(payload),
    retry: false,
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
  })
  const preview = previewQuery.data
  const error = saveError ?? previewQuery.error

  const current = list?.slots
    .flatMap((group) => group.items)
    .find((item) => item.id === suggestion.payload.item_id)

  if (edit && !payload) {
    if (!list) return null
    const isNew = suggestion.payload.action === 'add'
    const base = isNew ? { key: 'nuevo', id: null } : current
    if (!base) {
      return (
        <ConfirmDialog
          title="No se puede editar"
          message={`«${suggestion.payload.medication}» ya no está en la lista actual.`}
          confirmLabel="Cerrar"
          onConfirm={onClose}
          onCancel={onClose}
        />
      )
    }
    return (
      <MedItemSheet
        item={{ ...base, ...suggestion.payload.item }}
        isNew={isNew}
        slots={SLOTS}
        avoid={list.avoid}
        onClose={onClose}
        onSave={(draft) => {
          const item = itemPatch(draft, isNew ? null : current)
          setPayload({ ...suggestion.payload, item })
        }}
      />
    )
  }

  if (error && !preview) {
    return (
      <ConfirmDialog
        title="No se puede aplicar"
        message={errorMessage(error, 'No se pudo calcular el cambio.')}
        confirmLabel="Cerrar"
        onConfirm={onClose}
        onCancel={onClose}
      />
    )
  }
  if (!preview) return null
  return (
    <SaveSheet
      preview={preview}
      slots={SLOTS}
      busy={busy}
      error={error}
      onClose={onClose}
      onConfirm={async (summary) => {
        setBusy(true)
        setSaveError(null)
        try {
          const body = { expected_version: preview.version_no }
          if (summary) body.change_summary = summary
          if (edit) body.payload = payload
          onDone(await approve(body))
        } catch (err) {
          setSaveError(err)
        } finally {
          setBusy(false)
        }
      }}
    />
  )
}

/**
 * One suggestion in «Pendientes»: what it proposes, why, the messages it
 * comes from, and «Aprobar» / «Editar y aprobar» / «Rechazar».
 */
export default function SuggestionCard({ suggestion, ctx }) {
  const approveMutation = useApproveSuggestion()
  const reject = useRejectSuggestion()
  const [mode, setMode] = useState(null) // 'edit' | 'med' | 'medEdit' | 'reject'
  const [conflictDoctor, setConflictDoctor] = useState(null)
  const { kind, payload, context } = suggestion
  const patient = ctx.patients.find((p) => p.id === suggestion.patient_id)
  const color = patientColor(patient?.slug)
  const busy = approveMutation.isPending || reject.isPending
  const approve = (body) => approveMutation.mutateAsync({ id: suggestion.id, body })
  const close = () => setMode(null)
  const editable = kind !== 'lab_report' && !(kind === 'med_change' && payload.action === 'stop')
  const blocked =
    (kind === 'update_entry' && !context.entry) ||
    (kind === 'med_change' && payload.action !== 'add' && !context.item)
  const duplicates = kind === 'doctor' ? (context.duplicates ?? []) : []

  async function approveAsIs() {
    if (kind === 'med_change') {
      setMode('med')
      return
    }
    setConflictDoctor(null)
    try {
      await approve({})
    } catch (err) {
      if (err.status === 409 && err.body?.doctor) setConflictDoctor(err.body.doctor)
    }
  }

  async function link(doctorId) {
    try {
      await approve({ doctor_id: doctorId })
    } catch {
      // Shown from approveMutation.error.
    }
  }

  const linkTargets = conflictDoctor
    ? [conflictDoctor, ...duplicates.filter((d) => d.id !== conflictDoctor.id)]
    : duplicates

  return (
    <li className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-semibold text-slate-900">{KIND_LABELS[kind]}</h2>
        {patient && (
          <span className={`rounded-full px-2 py-0.5 text-sm font-medium ring-1 ${color.chip}`}>
            {patient.display_name}
          </span>
        )}
        {suggestion.explicit_request && (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-sm font-medium text-amber-900 ring-1 ring-amber-300">
            <MegaphoneIcon className="size-4" aria-hidden="true" />
            Pedido explícito
          </span>
        )}
        <span className="ml-auto text-sm text-slate-500">
          {SOURCE_LABELS[suggestion.source] ?? 'WhatsApp'}
          {suggestion.created_by_name ? ` (${suggestion.created_by_name})` : ''} ·{' '}
          {formatDate(suggestion.created_at.slice(0, 10))}
        </span>
      </div>

      <div className="mt-3">
        <ProposedRecord suggestion={suggestion} ctx={ctx} />
      </div>

      {suggestion.rationale && (
        <p className="mt-3 rounded-xl bg-brand-50 px-3 py-2 text-sm text-brand-900">
          <span className="font-semibold">Por qué: </span>
          {/* The badge already says «Pedido explícito»: drop the stored prefix. */}
          {(suggestion.explicit_request
            ? suggestion.rationale.replace(/^Pedido explícito[:.]\s*/, '')
            : suggestion.rationale) || 'La familia lo pidió en un mensaje «Bot».'}
        </p>
      )}

      {suggestion.chat_session_id && (
        <Link
          to={`/chat/${suggestion.chat_session_id}`}
          className="mt-3 flex min-h-11 items-center font-medium text-brand-700"
        >
          Ver la conversación del chat
        </Link>
      )}

      {suggestion.source_messages.length > 0 && (
        <details className="mt-3 group">
          <summary className="flex min-h-11 cursor-pointer items-center font-medium text-brand-700">
            Mensajes de origen ({suggestion.source_messages.length})
          </summary>
          <div className="mt-2">
            <SourceMessages messages={suggestion.source_messages} />
          </div>
        </details>
      )}

      {approveMutation.isError && mode === null && (
        <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
          {errorMessage(approveMutation.error, 'No se pudo aprobar. Inténtalo de nuevo.')}
        </p>
      )}

      {linkTargets.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {linkTargets.map((doctor) => (
            <button
              key={doctor.id}
              type="button"
              disabled={busy}
              onClick={() => link(doctor.id)}
              className={`${actionClass} text-brand-800 ring-1 ring-brand-300 active:bg-brand-50`}
            >
              Vincular a {doctor.name}
            </button>
          ))}
        </div>
      )}

      <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-3">
        {kind !== 'lab_report' && (
          <button
            type="button"
            disabled={busy || blocked}
            onClick={approveAsIs}
            className={`${actionClass} bg-brand-700 text-white active:bg-brand-800`}
          >
            <CheckIcon className="size-5" aria-hidden="true" />
            {approveMutation.isPending && mode === null ? 'Aprobando…' : 'Aprobar'}
          </button>
        )}
        {editable && (
          <button
            type="button"
            disabled={busy || blocked}
            onClick={() => setMode(kind === 'med_change' ? 'medEdit' : 'edit')}
            className={`${actionClass} text-brand-800 ring-1 ring-brand-300 active:bg-brand-50`}
          >
            <PencilSquareIcon className="size-5" aria-hidden="true" />
            Editar y aprobar
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={() => setMode('reject')}
          className={`${actionClass} text-slate-700 ring-1 ring-slate-300 active:bg-slate-100`}
        >
          <XMarkIcon className="size-5" aria-hidden="true" />
          Rechazar
        </button>
      </div>

      {mode === 'reject' && (
        <ConfirmDialog
          title="¿Rechazar la sugerencia?"
          message="No se guardará nada. La sugerencia sale de Pendientes."
          confirmLabel="Rechazar"
          danger
          busy={reject.isPending}
          error={reject.isError ? errorMessage(reject.error, 'No se pudo rechazar.') : ''}
          onCancel={close}
          onConfirm={async () => {
            try {
              await reject.mutateAsync({ id: suggestion.id })
            } catch {
              // Shown in the dialog.
            }
          }}
        />
      )}
      {mode === 'edit' && kind === 'create_entry' && (
        <EntrySheet
          prefill={payload}
          title="Editar y aprobar"
          submitLabel="Aprobar"
          submit={(data) => approve({ payload: data })}
          onClose={close}
          onSaved={close}
        />
      )}
      {mode === 'edit' && kind === 'update_entry' && (
        <UpdateEntryEdit suggestion={suggestion} approve={approve} onClose={close} onDone={close} />
      )}
      {mode === 'edit' && kind === 'create_appointment' && (
        <AppointmentSheet
          prefill={payload}
          today={panamaToday()}
          title="Editar y aprobar"
          submitLabel="Aprobar"
          submit={(data) => approve({ payload: data })}
          onClose={close}
          onSaved={close}
        />
      )}
      {mode === 'edit' && kind === 'doctor' && (
        <DoctorSheet
          prefill={payload}
          title="Editar y aprobar"
          submitLabel="Aprobar"
          submit={(data) => approve({ payload: data })}
          onClose={close}
          onSaved={close}
        />
      )}
      {(mode === 'med' || mode === 'medEdit') && (
        <MedChangeFlow
          suggestion={suggestion}
          edit={mode === 'medEdit'}
          approve={approve}
          onClose={close}
          onDone={close}
        />
      )}
    </li>
  )
}
