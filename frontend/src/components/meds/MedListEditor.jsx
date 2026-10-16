import { useMemo, useRef, useState } from 'react'
import {
  ArrowDownIcon,
  ArrowUpIcon,
  ExclamationTriangleIcon,
  PencilSquareIcon,
  PlusIcon,
  TrashIcon,
} from '@heroicons/react/20/solid'
import ConfirmDialog from '../ui/ConfirmDialog.jsx'
import MedItemBody from './MedItemBody.jsx'
import MedItemSheet from './MedItemSheet.jsx'
import AvoidSheet from './AvoidSheet.jsx'
import SaveSheet from './SaveSheet.jsx'
import { AvoidBlock, SlotHeading } from './MedListView.jsx'
import { errorMessage } from '../../lib/bitacora.js'
import { useDoctors } from '../../lib/doctors.js'
import {
  emptyItem,
  moveWithinSlot,
  newKey,
  rowErrors,
  sessionDirty,
  toSavePayload,
  upsertItem,
  useSaveMeds,
} from '../../lib/meds.js'

const iconButton =
  'flex size-11 items-center justify-center rounded-xl text-slate-700 ring-1 ring-slate-300 active:bg-slate-100 disabled:opacity-30'
const textButton =
  'flex min-h-11 items-center gap-1.5 rounded-xl px-3 text-sm font-medium ring-1 active:bg-slate-100'

function ItemRow({ item, prescriberName, today, first, last, error, onMove, onEdit, onRemove }) {
  const label = item.name || 'medicamento'
  return (
    <li className={`py-3 ${error ? 'rounded-xl bg-red-50 px-2 ring-1 ring-red-300' : ''}`}>
      <MedItemBody item={item} prescriberName={prescriberName} today={today} />
      {error && (
        <p role="alert" className="mt-2 flex gap-1.5 text-sm font-medium text-red-800">
          <ExclamationTriangleIcon className="size-5 shrink-0" aria-hidden="true" />
          {error}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={first}
          onClick={() => onMove(-1)}
          aria-label={`Subir ${label}`}
          className={iconButton}
        >
          <ArrowUpIcon className="size-5" aria-hidden="true" />
        </button>
        <button
          type="button"
          disabled={last}
          onClick={() => onMove(1)}
          aria-label={`Bajar ${label}`}
          className={iconButton}
        >
          <ArrowDownIcon className="size-5" aria-hidden="true" />
        </button>
        <span className="flex-1" />
        <button
          type="button"
          onClick={onEdit}
          aria-label={`Editar ${label}`}
          className={`${textButton} text-brand-800 ring-brand-100`}
        >
          <PencilSquareIcon className="size-5" aria-hidden="true" />
          Editar
        </button>
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Quitar ${label}`}
          className={`${textButton} text-red-700 ring-red-200 active:bg-red-50`}
        >
          <TrashIcon className="size-5" aria-hidden="true" />
          Quitar
        </button>
      </div>
    </li>
  )
}

/**
 * Edit mode of a patient's list. The working copy lives in `session` (owned
 * by the page, so switching patient keeps it); nothing reaches the server
 * until «Guardar» → dry run → «Revisar cambios» → «Confirmar y guardar».
 * The list's «Desde» (default: today) and note are set in «Revisar cambios»
 * (#135), so «Guardar» opens it even with no item changed (to fix only the
 * note); each change there re-runs the dry run.
 * A 409 (someone saved meanwhile) is never merged or overwritten: the user
 * keeps their working copy on screen and can go back to the current list.
 */
export default function MedListEditor({
  patientId,
  patientName,
  session,
  onChange,
  onExit,
  onSaved,
  onConflict,
  today,
}) {
  const { base, working } = session
  const slots = base.slots
  const save = useSaveMeds(patientId)
  const dryRun = useSaveMeds(patientId) // previews, so «Guardando…» is only the real save
  const { data: doctors = [] } = useDoctors()
  const [sheet, setSheet] = useState(null) // {kind: 'item'|'avoid', row, isNew}
  const [confirm, setConfirm] = useState(null) // {kind: 'item'|'avoid', row} | 'discard'
  const [preview, setPreview] = useState(null) // dry-run result
  const [listFields, setListFields] = useState(null) // {effective_date, list_note} of `preview`
  const [refreshing, setRefreshing] = useState(false)
  const previewRun = useRef(0) // only the latest dry run updates the sheet
  const [problem, setProblem] = useState(null) // {message, rows} from the last save attempt
  const [conflict, setConflict] = useState(false)

  const dirty = sessionDirty(session)
  const doctorNames = useMemo(() => new Map(doctors.map((d) => [d.id, d.name])), [doctors])
  const setWorking = (update) => onChange({ ...session, working: update(working) })

  const bySlot = useMemo(() => {
    const groups = Object.fromEntries(slots.map((s) => [s.slot, []]))
    for (const item of working.items) groups[item.slot]?.push(item)
    return groups
  }, [slots, working.items])

  function body(isDryRun, changeSummary, fields) {
    return {
      ...toSavePayload(working),
      expected_version: base.version_no,
      dry_run: isDryRun,
      ...fields,
      ...(changeSummary ? { change_summary: changeSummary } : {}),
    }
  }

  function handleError(error) {
    previewRun.current += 1
    setPreview(null)
    setRefreshing(false)
    if (error.status === 409) {
      setConflict(true)
      setProblem({
        message:
          'Alguien guardó la lista mientras la editabas: tus cambios no se guardaron. Lee el aviso al principio de la lista.',
        rows: {},
      })
      return
    }
    const rows = rowErrors(error, working)
    setProblem({
      message: Object.keys(rows).length
        ? 'Hay filas con datos que no se pudieron guardar. Están marcadas en rojo.'
        : errorMessage(error, 'No se pudo guardar la lista. Inténtalo de nuevo.'),
      rows,
    })
  }

  async function review() {
    setProblem(null)
    const fields = { effective_date: today, list_note: base.list_note ?? null }
    const run = ++previewRun.current
    try {
      const result = await dryRun.mutateAsync(body(true, null, fields))
      if (run !== previewRun.current) return
      setListFields(fields)
      setPreview(result)
    } catch (error) {
      handleError(error)
    }
  }

  async function refreshPreview(fields) {
    const run = ++previewRun.current
    setRefreshing(true)
    try {
      const result = await dryRun.mutateAsync(body(true, null, fields))
      if (run !== previewRun.current) return
      setPreview(result)
      setRefreshing(false)
    } catch (error) {
      if (run === previewRun.current) handleError(error)
    }
  }

  async function confirmSave(changeSummary, fields) {
    try {
      const result = await save.mutateAsync(body(false, changeSummary, fields))
      setPreview(null)
      onSaved(result)
    } catch (error) {
      if (error.status === 409 || error.status === 422) handleError(error)
      // Other errors stay in the sheet (save.error).
    }
  }

  function applyRow(kind, row) {
    if (kind === 'item') setWorking((w) => ({ ...w, items: upsertItem(w.items, row) }))
    else
      setWorking((w) => ({
        ...w,
        avoid: w.avoid.some((r) => r.key === row.key)
          ? w.avoid.map((r) => (r.key === row.key ? row : r))
          : [...w.avoid, row],
      }))
    if (problem?.rows[row.key]) {
      setProblem((p) => ({ ...p, rows: { ...p.rows, [row.key]: undefined } }))
    }
    setSheet(null)
  }

  function removeRow({ kind, row }) {
    const field = kind === 'item' ? 'items' : 'avoid'
    setWorking((w) => ({ ...w, [field]: w[field].filter((r) => r.key !== row.key) }))
    setConfirm(null)
  }

  const removeMessage = (kind, row) => {
    if (kind === 'avoid') return `${row.substance} dejará de estar en la lista EVITAR al guardar.`
    if (!row.id) return 'Todavía no se había guardado: simplemente se quita de tus cambios.'
    return `Al guardar, ${row.name} se suspende desde hoy y deja de aparecer en la lista. Quedará en el historial.`
  }

  return (
    <section aria-label={`Editando la lista de ${patientName}`} className="space-y-5">
      <div className="rounded-2xl border border-brand-100 bg-brand-50 p-4">
        <p className="font-semibold text-brand-800">Editando la lista de {patientName}</p>
        <p className="mt-1 text-sm text-brand-800">
          Los cambios no se guardan hasta que pulses «Guardar» y los confirmes.
        </p>
      </div>

      {conflict && (
        <div role="alert" className="rounded-2xl border border-amber-300 bg-amber-50 p-4">
          <p className="font-semibold text-amber-950">
            Alguien guardó la lista mientras la editabas.
          </p>
          <p className="mt-1 text-sm text-amber-950">
            Tus cambios no se guardaron, para no borrar lo que guardó la otra persona. Míralos aquí,
            luego abre la lista actual y vuelve a hacerlos.
          </p>
          <button
            type="button"
            onClick={() => setConfirm('reload')}
            className="mt-3 min-h-12 rounded-2xl bg-white px-4 font-semibold text-amber-950 ring-1 ring-amber-400 active:bg-amber-100"
          >
            Ver la lista actual
          </button>
        </div>
      )}

      {slots.map((group) => {
        const items = bySlot[group.slot]
        const headingId = `edit-${patientId}-${group.slot}`
        return (
          <section key={group.slot} aria-labelledby={headingId}>
            <SlotHeading id={headingId}>{group.label}</SlotHeading>
            {items.length > 0 && (
              <ul className="divide-y divide-slate-200">
                {items.map((item, index) => (
                  <ItemRow
                    key={item.key}
                    item={item}
                    prescriberName={doctorNames.get(item.prescriber_id)}
                    today={today}
                    first={index === 0}
                    last={index === items.length - 1}
                    error={problem?.rows[item.key]}
                    onMove={(delta) =>
                      setWorking((w) => ({ ...w, items: moveWithinSlot(w.items, item.key, delta) }))
                    }
                    onEdit={() => setSheet({ kind: 'item', row: item, isNew: false })}
                    onRemove={() => setConfirm({ kind: 'item', row: item })}
                  />
                ))}
              </ul>
            )}
            <button
              type="button"
              onClick={() => setSheet({ kind: 'item', row: emptyItem(group.slot), isNew: true })}
              className="mt-2 flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl border border-dashed border-slate-300 text-slate-700 active:bg-slate-100"
            >
              <PlusIcon className="size-5" aria-hidden="true" />
              Agregar en {group.label}
            </button>
          </section>
        )
      })}

      <AvoidBlock avoid={working.avoid}>
        {working.avoid.length === 0 && (
          <p className="mt-2 text-red-950">Nada en la lista EVITAR.</p>
        )}
        <ul className="mt-2 divide-y divide-red-200">
          {working.avoid.map((row) => (
            <li
              key={row.key}
              className={`py-2 ${problem?.rows[row.key] ? 'rounded-xl bg-white px-2 ring-1 ring-red-500' : ''}`}
            >
              <p className="text-lg font-bold text-red-950">{row.substance}</p>
              {row.reason && <p className="text-red-900">{row.reason}</p>}
              {problem?.rows[row.key] && (
                <p role="alert" className="text-sm font-medium text-red-800">
                  {problem.rows[row.key]}
                </p>
              )}
              <div className="mt-2 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setSheet({ kind: 'avoid', row, isNew: false })}
                  aria-label={`Editar ${row.substance}`}
                  className={`${textButton} bg-white text-brand-800 ring-brand-100`}
                >
                  <PencilSquareIcon className="size-5" aria-hidden="true" />
                  Editar
                </button>
                <button
                  type="button"
                  onClick={() => setConfirm({ kind: 'avoid', row })}
                  aria-label={`Quitar ${row.substance} de EVITAR`}
                  className={`${textButton} bg-white text-red-700 ring-red-200 active:bg-red-50`}
                >
                  <TrashIcon className="size-5" aria-hidden="true" />
                  Quitar
                </button>
              </div>
            </li>
          ))}
        </ul>
        <button
          type="button"
          onClick={() =>
            setSheet({
              kind: 'avoid',
              row: { key: newKey(), id: null, substance: '', reason: null, source_entry_id: null },
              isNew: true,
            })
          }
          className="mt-2 flex min-h-12 w-full items-center justify-center gap-2 rounded-2xl border border-dashed border-red-300 bg-white text-red-900 active:bg-red-100"
        >
          <PlusIcon className="size-5" aria-hidden="true" />
          Agregar a EVITAR
        </button>
      </AvoidBlock>

      {/* Sticks above the bottom nav (min-h-16 + its 1px border). */}
      <div className="sticky bottom-[calc(4rem+1px+env(safe-area-inset-bottom))] z-10 -mx-4 border-t border-slate-200 bg-white/95 px-4 py-3 backdrop-blur lg:mx-0 lg:rounded-2xl lg:border lg:shadow-sm">
        {problem?.message && (
          <p role="alert" className="mb-2 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
            {problem.message}
          </p>
        )}
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => (dirty ? setConfirm('discard') : onExit())}
            className="min-h-12 flex-1 rounded-2xl font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100"
          >
            Cancelar edición
          </button>
          <button
            type="button"
            onClick={review}
            disabled={save.isPending || dryRun.isPending || conflict}
            className="min-h-12 flex-1 whitespace-nowrap rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800 disabled:opacity-50"
          >
            {dryRun.isPending && !preview ? 'Revisando…' : 'Guardar'}
          </button>
        </div>
      </div>

      {sheet?.kind === 'item' && (
        <MedItemSheet
          item={sheet.row}
          isNew={sheet.isNew}
          slots={slots}
          avoid={working.avoid}
          onSave={(row) => applyRow('item', row)}
          onClose={() => setSheet(null)}
        />
      )}
      {sheet?.kind === 'avoid' && (
        <AvoidSheet
          row={sheet.row}
          isNew={sheet.isNew}
          onSave={(row) => applyRow('avoid', row)}
          onClose={() => setSheet(null)}
        />
      )}
      {preview && (
        <SaveSheet
          preview={preview}
          slots={slots}
          listFields={listFields}
          busy={save.isPending}
          refreshing={refreshing}
          onRefresh={refreshPreview}
          error={save.isError && save.error.status !== 409 ? save.error : null}
          onConfirm={confirmSave}
          onClose={() => {
            save.reset()
            previewRun.current += 1
            setRefreshing(false)
            setPreview(null)
          }}
        />
      )}
      {confirm?.kind && (
        <ConfirmDialog
          title={
            confirm.kind === 'item'
              ? `¿Quitar ${confirm.row.name}?`
              : `¿Quitar ${confirm.row.substance} de EVITAR?`
          }
          message={removeMessage(confirm.kind, confirm.row)}
          confirmLabel="Quitar"
          cancelLabel="No quitar"
          danger
          onConfirm={() => removeRow(confirm)}
          onCancel={() => setConfirm(null)}
        />
      )}
      {confirm === 'discard' && (
        <ConfirmDialog
          title="¿Descartar los cambios?"
          message="Ninguno de los cambios que hiciste en la lista se guardará."
          confirmLabel="Descartar"
          cancelLabel="Seguir editando"
          danger
          onConfirm={onExit}
          onCancel={() => setConfirm(null)}
        />
      )}
      {confirm === 'reload' && (
        <ConfirmDialog
          title="¿Ver la lista actual?"
          message="Se cierra la edición y tus cambios se pierden. Anota antes lo que quieras volver a hacer."
          confirmLabel="Ver la lista actual"
          cancelLabel="Seguir viendo mis cambios"
          danger
          onConfirm={onConflict}
          onCancel={() => setConfirm(null)}
        />
      )}
    </section>
  )
}
