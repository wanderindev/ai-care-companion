import { useId, useState } from 'react'
import Sheet from '../ui/Sheet.jsx'
import { errorMessage } from '../../lib/bitacora.js'
import { EVENT_KIND_LABELS, useUpdateMedEvent } from '../../lib/meds.js'

const inputClass =
  'mt-1 min-h-12 w-full rounded-2xl border border-slate-300 bg-white px-3 text-base focus:border-brand-600 focus:ring-2 focus:ring-brand-100 focus:outline-none'

/**
 * Correct a change reconstructed from the bitácora (`med_events`): kind,
 * medication, detail and date. Saving a «Por revisar» event also confirms it
 * (editing it is reviewing it).
 */
export default function MedEventSheet({ event, patientId, onClose }) {
  const update = useUpdateMedEvent(patientId)
  const formId = useId()
  const pending = event.review_status === 'pending_review'
  const [kind, setKind] = useState(event.kind)
  const [medication, setMedication] = useState(event.medication)
  const [detail, setDetail] = useState(event.detail ?? '')
  const [eventDate, setEventDate] = useState(event.event_date)

  const data = {}
  if (kind !== event.kind) data.kind = kind
  if (medication.trim() !== event.medication) data.medication = medication.trim()
  if ((detail.trim() || null) !== event.detail) data.detail = detail.trim() || null
  if (eventDate !== event.event_date) data.event_date = eventDate
  if (pending) data.review_status = 'ok'
  const valid = medication.trim() !== '' && Boolean(eventDate)

  async function handleSubmit(e) {
    e.preventDefault()
    if (!valid) return
    if (Object.keys(data).length === 0) return onClose()
    try {
      await update.mutateAsync({ id: event.id, data })
      onClose()
    } catch {
      // the error shows in the sheet
    }
  }

  return (
    <Sheet
      title="Corregir cambio"
      onClose={onClose}
      footer={
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onClose}
            className="min-h-12 flex-1 rounded-2xl font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100"
          >
            Cancelar
          </button>
          <button
            type="submit"
            form={formId}
            disabled={update.isPending || !valid}
            className="min-h-12 flex-[2] rounded-2xl bg-brand-700 font-semibold text-white active:bg-brand-800 disabled:opacity-60"
          >
            {update.isPending ? 'Guardando…' : pending ? 'Guardar y confirmar' : 'Guardar'}
          </button>
        </div>
      }
    >
      <form id={formId} onSubmit={handleSubmit} className="space-y-5">
        <div>
          <label htmlFor={`${formId}-kind`} className="text-sm font-medium text-slate-700">
            Tipo de cambio
          </label>
          <select
            id={`${formId}-kind`}
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            className={inputClass}
          >
            {Object.entries(EVENT_KIND_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor={`${formId}-medication`} className="text-sm font-medium text-slate-700">
            Medicamento
          </label>
          <input
            id={`${formId}-medication`}
            type="text"
            required
            maxLength={200}
            value={medication}
            onChange={(e) => setMedication(e.target.value)}
            className={inputClass}
          />
        </div>

        <div>
          <label htmlFor={`${formId}-detail`} className="text-sm font-medium text-slate-700">
            Detalle <span className="font-normal text-slate-500">(dosis, horario, motivo)</span>
          </label>
          <textarea
            id={`${formId}-detail`}
            rows={3}
            maxLength={2000}
            value={detail}
            onChange={(e) => setDetail(e.target.value)}
            className={`${inputClass} py-2`}
          />
        </div>

        <div>
          <label htmlFor={`${formId}-date`} className="text-sm font-medium text-slate-700">
            Fecha
          </label>
          <input
            id={`${formId}-date`}
            type="date"
            required
            value={eventDate}
            onChange={(e) => setEventDate(e.target.value)}
            className={inputClass}
          />
        </div>

        {update.isError && (
          <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">
            {errorMessage(update.error, 'No se pudo guardar el cambio.')}
          </p>
        )}
      </form>
    </Sheet>
  )
}
