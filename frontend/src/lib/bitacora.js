/**
 * Bitácora vocabulary and the filter <-> URL mapping (docs/DESIGN.md §5.2, §6.1).
 *
 * The API speaks Spanish enum values (`consulta_control`, `whatsapp`…); this is
 * the one place that turns them into labels. Filters live in the URL search
 * params so a filtered list can be shared and survives the back button.
 */

/** entry_type values, in the order the pickers show them. */
export const ENTRY_TYPES = [
  { value: 'consulta_control', label: 'Control' },
  { value: 'consulta_enfermedad', label: 'Por enfermedad' },
  { value: 'visita_domiciliaria', label: 'Visita a domicilio' },
  { value: 'teleconsulta', label: 'Teleconsulta' },
  { value: 'terapia', label: 'Terapia' },
  { value: 'vacuna', label: 'Vacuna' },
  { value: 'procedimiento', label: 'Procedimiento' },
  { value: 'hospitalizacion', label: 'Hospitalización' },
  { value: 'examen', label: 'Examen' },
  { value: 'observacion_familiar', label: 'Observación familiar' },
  { value: 'otro', label: 'Otro' },
]

export const CHANNELS = [
  { value: 'presencial', label: 'Presencial' },
  { value: 'domicilio', label: 'A domicilio' },
  { value: 'whatsapp', label: 'WhatsApp' },
  { value: 'telefono', label: 'Teléfono' },
  { value: 'correo', label: 'Correo' },
]

const TYPE_LABELS = Object.fromEntries(ENTRY_TYPES.map((t) => [t.value, t.label]))
const CHANNEL_LABELS = Object.fromEntries(CHANNELS.map((c) => [c.value, c.label]))

export function entryTypeLabel(value) {
  return TYPE_LABELS[value] ?? value
}

export function channelLabel(value) {
  return value ? (CHANNEL_LABELS[value] ?? value) : ''
}

/** Header line of an entry: the doctor's name, else the free-text source. */
export function entrySource(entry) {
  return entry.doctor?.name || entry.source_label || ''
}

// --- Filters in the URL -----------------------------------------------------

/** URL `orden` value <-> API `sort` value. */
const SORT_PARAM = { fecha: 'date', medico: 'doctor', tipo: 'type' }
const SORT_TO_PARAM = Object.fromEntries(Object.entries(SORT_PARAM).map(([k, v]) => [v, k]))
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

/** A real calendar date as YYYY-MM-DD (rejects 2026-02-31). */
function isValidDate(value) {
  if (!DATE_RE.test(value)) return false
  const [y, m, d] = value.split('-').map(Number)
  const date = new Date(Date.UTC(y, m - 1, d))
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d
}

/** A natural first direction per sort field: newest first, names A–Z. */
export const DEFAULT_ORDER = { date: 'desc', doctor: 'asc', type: 'asc' }

export const EMPTY_FILTERS = Object.freeze({
  doctorIds: [],
  types: [],
  dateFrom: '',
  dateTo: '',
  pendingOnly: false,
  q: '',
  sort: 'date',
  order: 'desc',
  page: 1,
})

function positiveInts(values) {
  const out = []
  for (const value of values) {
    const n = Number(value)
    if (Number.isInteger(n) && n > 0 && n <= 2 ** 31 - 1 && !out.includes(n)) out.push(n)
  }
  return out
}

/**
 * URLSearchParams -> filters. Unknown or malformed values are dropped, so a
 * hand-edited URL never reaches the API as a 422.
 *
 *   medico=3&medico=7  tipo=vacuna  desde=2026-01-01  hasta=2026-03-31
 *   revisar=1  q=texto  orden=fecha|medico|tipo  dir=asc|desc  pagina=2
 */
export function parseFilters(searchParams) {
  const sort = SORT_PARAM[searchParams.get('orden')] ?? 'date'
  const dir = searchParams.get('dir')
  const page = Number(searchParams.get('pagina'))
  const dateFrom = searchParams.get('desde') ?? ''
  const dateTo = searchParams.get('hasta') ?? ''
  return {
    doctorIds: positiveInts(searchParams.getAll('medico')),
    types: [...new Set(searchParams.getAll('tipo').filter((t) => t in TYPE_LABELS))],
    dateFrom: isValidDate(dateFrom) ? dateFrom : '',
    dateTo: isValidDate(dateTo) ? dateTo : '',
    pendingOnly: searchParams.get('revisar') === '1',
    q: (searchParams.get('q') ?? '').slice(0, 200),
    sort,
    order: dir === 'asc' || dir === 'desc' ? dir : DEFAULT_ORDER[sort],
    page: Number.isInteger(page) && page > 1 ? page : 1,
  }
}

/** Filters -> URLSearchParams, leaving defaults out to keep URLs short. */
export function filtersToSearchParams(filters) {
  const f = { ...EMPTY_FILTERS, ...filters }
  const params = new URLSearchParams()
  f.doctorIds.forEach((id) => params.append('medico', String(id)))
  f.types.forEach((t) => params.append('tipo', t))
  if (f.dateFrom) params.set('desde', f.dateFrom)
  if (f.dateTo) params.set('hasta', f.dateTo)
  if (f.pendingOnly) params.set('revisar', '1')
  if (f.q.trim()) params.set('q', f.q.trim())
  if (f.sort !== 'date') params.set('orden', SORT_TO_PARAM[f.sort])
  if (f.order !== DEFAULT_ORDER[f.sort]) params.set('dir', f.order)
  if (f.page > 1) params.set('pagina', String(f.page))
  return params
}

/** True when anything narrows the list (sort and page don't count). */
export function hasActiveFilters(filters) {
  return Boolean(
    filters.doctorIds.length ||
      filters.types.length ||
      filters.dateFrom ||
      filters.dateTo ||
      filters.pendingOnly ||
      filters.q.trim(),
  )
}

/** Query string for GET /log-entries. */
export function toApiParams(filters, patientIds, { limit, offset }) {
  const params = new URLSearchParams()
  patientIds.forEach((id) => params.append('patient_id', String(id)))
  filters.doctorIds.forEach((id) => params.append('doctor_id', String(id)))
  filters.types.forEach((t) => params.append('entry_type', t))
  if (filters.pendingOnly) params.set('review_status', 'pending_review')
  if (filters.dateFrom) params.set('date_from', filters.dateFrom)
  if (filters.dateTo) params.set('date_to', filters.dateTo)
  if (filters.q.trim()) params.set('q', filters.q.trim())
  params.set('sort', filters.sort)
  params.set('order', filters.order)
  params.set('limit', String(limit))
  params.set('offset', String(offset))
  return params
}

/**
 * Body filter for `POST /log-entries/review` («Marcar todas como revisadas»):
 * the same filters `toApiParams` sends, so the action hits exactly what the
 * list shows (the endpoint itself only touches «Por revisar» entries).
 */
export function toReviewFilter(filters, patientIds) {
  const filter = {
    patient_id: [...patientIds],
    doctor_id: [...filters.doctorIds],
    entry_type: [...filters.types],
  }
  if (filters.dateFrom) filter.date_from = filters.dateFrom
  if (filters.dateTo) filter.date_to = filters.dateTo
  if (filters.q.trim()) filter.q = filters.q.trim()
  return filter
}

// --- Display helpers ----------------------------------------------------------

const MONTHS = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']

/** '2026-03-04' -> '4 mar 2026' (no Date parsing, so no timezone shift). */
export function formatDate(iso) {
  if (!iso || !DATE_RE.test(iso)) return iso ?? ''
  const [y, m, d] = iso.split('-').map(Number)
  return `${d} ${MONTHS[m - 1]} ${y}`
}

/** Label of the date-range filter chip. */
export function dateChipLabel(from, to) {
  if (from && to) return `${formatDate(from)} – ${formatDate(to)}`
  if (from) return `Desde ${formatDate(from)}`
  if (to) return `Hasta ${formatDate(to)}`
  return 'Fechas'
}

/** '2026-03-04' -> 'miércoles, 4 de marzo de 2026'. */
export function formatLongDate(iso) {
  if (!iso || !DATE_RE.test(iso)) return iso ?? ''
  const [y, m, d] = iso.split('-').map(Number)
  return new Intl.DateTimeFormat('es', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(y, m - 1, d)))
}

/** '14:30:00' -> '14:30'. */
export function formatTime(value) {
  return value ? value.slice(0, 5) : ''
}

/** Today in the device's timezone, as YYYY-MM-DD. */
export function todayISO() {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1).replace('.', ',')} MB`
}

/** How the browser can show a file: inline image, audio player, or a chip. */
export function attachmentKind(mime) {
  if (mime === 'image/heic' || mime === 'image/heif') return 'file-image' // most browsers can't
  if (mime?.startsWith('image/')) return 'image'
  if (mime?.startsWith('audio/')) return 'audio'
  if (mime === 'application/pdf') return 'pdf'
  return 'file'
}

/** Markdown -> a plain one-paragraph preview for cards. */
export function plainPreview(markdown) {
  if (!markdown) return ''
  return markdown
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[#>*_`~|]+/g, ' ')
    .replace(/^\s*[-+]\s+/gm, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/** Accent/case-insensitive comparison key (for client-side doctor search). */
export function normalizeText(text) {
  return (text ?? '')
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .toLowerCase()
    .trim()
}

/** A Spanish message for an attachment upload failure. */
export function uploadErrorMessage(error) {
  switch (error?.status) {
    case 413:
      return error.message?.startsWith('El archivo')
        ? error.message
        : 'El archivo es demasiado grande.'
    case 415:
      return 'Tipo de archivo no permitido. Sube una foto, un PDF o un audio.'
    case 503:
      return 'El almacenamiento de archivos no está disponible ahora. Puedes guardar la entrada sin este archivo.'
    case 400:
      return 'El archivo está vacío.'
    default:
      return error?.status
        ? 'No se pudo subir el archivo. Inténtalo de nuevo.'
        : 'No se pudo subir el archivo. Revisa tu conexión.'
  }
}

/** A Spanish message for any failed API call. */
export function errorMessage(error, fallback = 'Algo salió mal. Inténtalo de nuevo.') {
  if (!error) return ''
  if (!error.status) return 'Sin conexión con el servidor. Revisa tu conexión e inténtalo de nuevo.'
  // Backend details are Spanish strings; validation (422) bodies are lists.
  if (typeof error.body?.detail === 'string') return error.body.detail
  return fallback
}

// ── "Próxima cita" (the appointment an entry created) ──────────────────────

export const APPOINTMENT_STATUS_LABELS = {
  programada: 'Programada',
  realizada: 'Realizada',
  cancelada: 'Cancelada',
}

/**
 * `next_appointment.starts_at` comes on the Panamá clock
 * ('2026-04-14T14:00:00-05:00'), so the date and time are read off the string
 * (no Date parsing, no device-timezone shift).
 */
export function appointmentDate(appt) {
  return appt?.starts_at?.slice(0, 10) ?? ''
}

export function appointmentTime(appt) {
  return appt && !appt.all_day ? appt.starts_at.slice(11, 16) : ''
}

/** '14 abr 2026 · 14:00' (date only for all-day appointments). */
export function appointmentLabel(appt) {
  const time = appointmentTime(appt)
  return `${formatDate(appointmentDate(appt))}${time ? ` · ${time}` : ''}`
}

/**
 * Form state of the "Próxima cita" from an entry's `next_appointment`. Only a
 * `programada` one is editable here; a cancelled one starts empty (filling the
 * field again brings it back), a `realizada` one is shown read-only.
 */
export function initialNextAppointment(appt) {
  const editable = appt?.status === 'programada'
  return {
    enabled: editable,
    date: editable ? appointmentDate(appt) : '',
    time: editable ? appointmentTime(appt) : '',
    // Until the user picks one, the appointment's doctor follows the entry's.
    doctorTouched: editable,
    doctorId: editable ? appt.doctor_id : null,
    location: editable ? (appt.location ?? '') : '',
    notes: editable ? (appt.notes ?? '') : '',
  }
}

/** API body (`next_appointment`) for the form state, or null when off. */
export function nextAppointmentPayload(next, entryDoctorId) {
  if (!next.enabled) return null
  return {
    date: next.date,
    time: next.time || null,
    doctor_id: next.doctorTouched ? next.doctorId : entryDoctorId,
    location: next.location.trim() || null,
    notes: next.notes.trim() || null,
  }
}

export function validateNextAppointment(next, entryDate) {
  if (!next.enabled) return null
  if (!next.date) return 'Indica la fecha de la próxima cita.'
  if (entryDate && next.date < entryDate) {
    return 'La próxima cita no puede ser anterior a la fecha de la entrada.'
  }
  return null
}
