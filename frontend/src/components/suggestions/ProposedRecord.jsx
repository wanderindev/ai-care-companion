import { ExclamationTriangleIcon } from '@heroicons/react/20/solid'
import Markdown from '../bitacora/Markdown.jsx'
import MedItemBody from '../meds/MedItemBody.jsx'
import {
  appointmentLabel,
  channelLabel,
  entryTypeLabel,
  formatDate,
  formatTime,
} from '../../lib/bitacora.js'
import { SLOTS, formatDays, formatQuantity } from '../../lib/meds.js'
import { MED_ACTION_LABELS } from '../../lib/suggestions.js'

const slotLabel = (slot) => SLOTS.find((s) => s.slot === slot)?.label ?? slot

function Row({ label, children }) {
  if (children == null || children === '') return null
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-2 py-1">
      <dt className="text-sm text-slate-500">{label}</dt>
      <dd className="min-w-0 break-words text-slate-900">{children}</dd>
    </div>
  )
}

function Warning({ children }) {
  return (
    <p className="mt-2 flex gap-2 rounded-xl bg-amber-50 px-3 py-2 text-sm text-amber-950 ring-1 ring-amber-200">
      <ExclamationTriangleIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </p>
  )
}

const ENTRY_FIELD_LABELS = {
  patient_id: 'Paciente',
  entry_date: 'Fecha',
  entry_time: 'Hora',
  doctor_id: 'Médico',
  source_label: 'Fuente',
  entry_type: 'Tipo',
  channel: 'Canal',
  title: 'Título',
  indications: 'Indicaciones',
  family_notes: 'Notas de la familia',
  review_status: 'Revisión',
  next_appointment: 'Próxima cita',
  attachment_ids: 'Archivos',
}

function entryValue(key, value, ctx) {
  if (value == null || value === '') return '—'
  switch (key) {
    case 'patient_id':
      return ctx.patientName(value)
    case 'entry_date':
      return formatDate(value)
    case 'entry_time':
      return formatTime(value)
    case 'doctor_id':
      return ctx.doctorLabel ? ctx.doctorLabel(value) : ctx.doctorName(value)
    case 'entry_type':
      return entryTypeLabel(value)
    case 'channel':
      return channelLabel(value)
    case 'review_status':
      return value === 'ok' ? 'Revisada' : 'Por revisar'
    case 'next_appointment':
      return nextLabel(value)
    case 'attachment_ids':
      return `${value.length} archivo(s)`
    default:
      return String(value)
  }
}

function nextLabel(next) {
  if (!next?.date) return 'Sin próxima cita'
  return `${formatDate(next.date)}${next.time ? ` · ${formatTime(next.time)}` : ''}`
}

function EntryFields({ entry, ctx }) {
  return (
    <>
      <dl>
        <Row label="Fecha">
          {formatDate(entry.entry_date)}
          {entry.entry_time ? ` · ${formatTime(entry.entry_time)}` : ''}
        </Row>
        <Row label="Tipo">{entryTypeLabel(entry.entry_type)}</Row>
        <Row label="Médico">{entry.doctor_id ? ctx.doctorName(entry.doctor_id) : null}</Row>
        <Row label="Fuente">{entry.source_label}</Row>
        <Row label="Canal">{entry.channel ? channelLabel(entry.channel) : null}</Row>
        <Row label="Título">{entry.title}</Row>
        <Row label="Próxima cita">
          {entry.next_appointment ? nextLabel(entry.next_appointment) : null}
        </Row>
      </dl>
      {entry.indications && (
        <div className="mt-2">
          <p className="text-sm text-slate-500">Indicaciones</p>
          <Markdown>{entry.indications}</Markdown>
        </div>
      )}
      {entry.family_notes && (
        <div className="mt-2">
          <p className="text-sm text-slate-500">Notas de la familia</p>
          <Markdown>{entry.family_notes}</Markdown>
        </div>
      )}
    </>
  )
}

function EntryChanges({ payload, context, ctx }) {
  const current = context.entry
  if (!current) return <Warning>La entrada que esta sugerencia quería cambiar ya no existe.</Warning>
  return (
    <>
      <p className="text-slate-700">
        Entrada del {formatDate(current.entry_date)} · {entryTypeLabel(current.entry_type)}
        {current.title || current.source_label
          ? ` · ${current.title || current.source_label}`
          : ''}
      </p>
      <ul className="mt-2 space-y-2">
        {Object.entries(payload.changes).map(([key, value]) => (
          <li key={key} className="rounded-xl bg-slate-50 p-2">
            <p className="text-sm font-medium text-slate-600">{ENTRY_FIELD_LABELS[key] ?? key}</p>
            {key in current && (
              <p className="text-sm text-slate-500 line-through">
                {entryValue(key, current[key], ctx)}
              </p>
            )}
            <p className="break-words whitespace-pre-wrap text-slate-900">
              {entryValue(key, value, ctx)}
            </p>
          </li>
        ))}
      </ul>
    </>
  )
}

function AppointmentFields({ appt, ctx }) {
  // `starts_at` may come without an offset (Panamá time): read it off the string.
  const label = appointmentLabel({ starts_at: appt.starts_at, all_day: appt.all_day })
  return (
    <dl>
      <Row label="Fecha">{label}</Row>
      <Row label="Médico">{appt.doctor_id ? ctx.doctorName(appt.doctor_id) : null}</Row>
      <Row label="Lugar">{appt.location}</Row>
      <Row label="Motivo">{appt.reason}</Row>
      <Row label="Notas">{appt.notes}</Row>
    </dl>
  )
}

function itemLine(item) {
  return [
    formatQuantity(item.quantity),
    item.name,
    item.strength,
    item.slot && slotLabel(item.slot),
    item.time && formatTime(item.time),
    formatDays(item.days),
  ]
    .filter(Boolean)
    .join(' · ')
}

const MED_FIELD_LABELS = {
  name: 'Nombre',
  strength: 'Concentración',
  quantity: 'Cantidad',
  dose_note: 'Nota de dosis',
  slot: 'Horario',
  time: 'Hora',
  days: 'Días',
  schedule_note: 'Calendario',
  instructions: 'Instrucciones',
  prescriber_id: 'Recetado por',
  reason: 'Motivo',
  start_date: 'Desde',
  end_date: 'Hasta',
}

function medValue(key, value, ctx) {
  if (value == null || value === '') return '—'
  if (key === 'slot') return slotLabel(value)
  if (key === 'days') return formatDays(value) || 'Todos los días'
  if (key === 'quantity') return formatQuantity(value)
  if (key === 'time') return formatTime(value)
  if (key === 'prescriber_id') return ctx.doctorName(value)
  if (key === 'start_date' || key === 'end_date') return formatDate(value)
  return String(value)
}

function MedChange({ payload, context, ctx }) {
  const action = MED_ACTION_LABELS[payload.action]
  const current = context.item
  return (
    <>
      <p className="font-semibold text-slate-900">
        {action}: {payload.medication}
      </p>
      {payload.action === 'add' && (
        <div className="mt-2 rounded-xl bg-slate-50 p-2">
          <MedItemBody item={payload.item} prescriberName={null} />
          <p className="mt-1 text-sm text-slate-600">Horario: {slotLabel(payload.item.slot)}</p>
        </div>
      )}
      {payload.action !== 'add' && current && (
        <p className="mt-1 text-sm text-slate-600">Ahora: {itemLine(current)}</p>
      )}
      {payload.action === 'change' && (
        <ul className="mt-2 space-y-1">
          {Object.entries(payload.item).map(([key, value]) => (
            <li key={key} className="text-slate-900">
              <span className="text-sm text-slate-500">{MED_FIELD_LABELS[key] ?? key}: </span>
              {current && key in current && (
                <span className="text-slate-500 line-through">
                  {medValue(key, current[key], ctx)}
                </span>
              )}{' '}
              {medValue(key, value, ctx)}
            </li>
          ))}
        </ul>
      )}
      {payload.action !== 'add' && !current && (
        <Warning>«{payload.medication}» ya no está en la lista actual de medicamentos.</Warning>
      )}
      {context.stale && (
        <Warning>
          La lista cambió desde que se hizo esta sugerencia. Al aprobar, el cambio se aplica a la
          lista actual; revisa la vista previa.
        </Warning>
      )}
    </>
  )
}

function DoctorFields({ doctor, context }) {
  return (
    <>
      <dl>
        <Row label="Nombre">{doctor.name}</Row>
        <Row label="Especialidad">{doctor.specialty}</Row>
        <Row label="Teléfono">{doctor.phone}</Row>
        <Row label="Correo">{doctor.email}</Row>
        <Row label="Clínica">{doctor.clinic}</Row>
        <Row label="Dirección">{doctor.address}</Row>
        <Row label="También">{doctor.aliases?.length ? doctor.aliases.join(', ') : null}</Row>
      </dl>
      {context.duplicates?.length > 0 && (
        <Warning>
          Ya existe un médico parecido:{' '}
          {context.duplicates.map((d) => d.name + (d.specialty ? ` (${d.specialty})` : '')).join(', ')}
          . Puedes vincular la sugerencia a ese médico en lugar de crear otro.
        </Warning>
      )}
    </>
  )
}

/**
 * The proposed record, readable (no JSON): entry fields, the changes to an
 * entry (before → after; a doctor-only change from the doctor inference,
 * #125, shows the entry and the proposed doctor with their specialty),
 * appointment date/doctor, a medication change, a doctor. `ctx` resolves
 * names: `doctorName(id)`, optional `doctorLabel(id)` (name · specialty),
 * `patientName(id)`.
 */
export default function ProposedRecord({ suggestion, ctx }) {
  const { kind, payload, context } = suggestion
  if (kind === 'create_entry') return <EntryFields entry={payload} ctx={ctx} />
  if (kind === 'update_entry') return <EntryChanges payload={payload} context={context} ctx={ctx} />
  if (kind === 'create_appointment') return <AppointmentFields appt={payload} ctx={ctx} />
  if (kind === 'med_change') return <MedChange payload={payload} context={context} ctx={ctx} />
  if (kind === 'doctor') return <DoctorFields doctor={payload} context={context} />
  return (
    <p className="text-slate-700">
      Los informes de laboratorio se registran solos en Laboratorios; esta sugerencia solo se puede
      rechazar.
    </p>
  )
}
