/**
 * Medications (docs/DESIGN.md §5.4, §6.4): data hooks over
 * /patients/{id}/meds and the small display / working-copy helpers the
 * Medicamentos tab needs.
 *
 * The list is saved whole (`PUT`, full-list save): the editor keeps a working
 * copy in component state, asks for a `dry_run` diff + summary, and only the
 * confirmed save creates a version. A save invalidates `['meds', patientId]`.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'
import { fetchPdf } from './pdf.js'
import { panamaToday } from './appointments.js'

export const MEDS_KEY = ['meds']
export const medsKey = (patientId) => [...MEDS_KEY, patientId]

/** GET /patients/{id}/meds: every slot in printed-sheet order, EVITAR, version. */
export function useMeds(patientId) {
  return useQuery({
    queryKey: medsKey(patientId),
    queryFn: ({ signal }) => api.get(`/patients/${patientId}/meds`, { signal }),
    enabled: Boolean(patientId),
  })
}

/** GET /patients/{id}/meds/versions (newest first; carries the author's name). */
export function useMedVersions(patientId, { enabled = true } = {}) {
  return useQuery({
    queryKey: [...medsKey(patientId), 'versions'],
    queryFn: ({ signal }) => api.get(`/patients/${patientId}/meds/versions`, { signal }),
    enabled: Boolean(patientId) && enabled,
  })
}

/**
 * GET /patients/{id}/meds/versions/{n}: the version's snapshot, the diff vs
 * the previous version, and `previous_version_no` / `next_version_no`.
 */
export function useMedVersion(patientId, versionNo) {
  return useQuery({
    queryKey: [...medsKey(patientId), 'version', versionNo],
    queryFn: ({ signal }) =>
      api.get(`/patients/${patientId}/meds/versions/${versionNo}`, { signal }),
    enabled: Boolean(patientId) && Boolean(versionNo),
  })
}

/** Events per «Cargar más» page in Historial (the server allows up to 500). */
export const EVENTS_PAGE_SIZE = 100

/**
 * GET /patients/{id}/meds/events: the history reconstructed from the
 * bitácora (`med_events`), newest first, a page at a time (keyset cursor,
 * ticket #82). `data.pages[n] = {items, next_cursor}`; «Cargar más» is
 * `fetchNextPage()`. An invalidation refetches every loaded page.
 */
export function useMedEvents(patientId) {
  return useInfiniteQuery({
    queryKey: [...medsKey(patientId), 'events'],
    queryFn: ({ pageParam, signal }) => {
      const cursor = pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ''
      return api.get(`/patients/${patientId}/meds/events?limit=${EVENTS_PAGE_SIZE}${cursor}`, {
        signal,
      })
    },
    initialPageParam: null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: Boolean(patientId),
  })
}

/**
 * GET …/meds/events?review_status=pending_review: **every** pending event
 * (never paged), for «Por revisar (N)», its pending-only view and
 * «Confirmar todos».
 */
export function usePendingMedEvents(patientId) {
  return useQuery({
    queryKey: [...medsKey(patientId), 'events-pending'],
    queryFn: ({ signal }) =>
      api.get(`/patients/${patientId}/meds/events?review_status=pending_review`, { signal }),
    select: (data) => data.items,
    enabled: Boolean(patientId),
  })
}

function useEventWrite(patientId, mutationFn) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSettled: () => queryClient.invalidateQueries({ queryKey: medsKey(patientId) }),
  })
}

/**
 * Review of a derived event (ticket #28). PATCH …/meds/events/{id} with any of
 * `{event_date, kind, medication, detail, review_status}`; `review_status:
 * 'ok'` confirms it.
 */
export function useUpdateMedEvent(patientId) {
  return useEventWrite(patientId, ({ id, data }) =>
    api.patch(`/patients/${patientId}/meds/events/${id}`, data),
  )
}

/** DELETE …/meds/events/{id}: «Descartar» (the backfill never recreates it). */
export function useDeleteMedEvent(patientId) {
  return useEventWrite(patientId, (id) => api.delete(`/patients/${patientId}/meds/events/${id}`))
}

/** Ids per «Confirmar todos» request (the server's limit). */
export const CONFIRM_CHUNK = 500

/**
 * POST …/meds/events/confirm `{ids}` → `{confirmed}`: «Confirmar todos». The
 * pending list is not capped, so a longer list goes in chunks of
 * `CONFIRM_CHUNK`, one after the other; resolves to the total confirmed.
 */
export function useConfirmMedEvents(patientId) {
  return useEventWrite(patientId, async (ids) => {
    let confirmed = 0
    for (let i = 0; i < ids.length; i += CONFIRM_CHUNK) {
      const result = await api.post(`/patients/${patientId}/meds/events/confirm`, {
        ids: ids.slice(i, i + CONFIRM_CHUNK),
      })
      confirmed += result.confirmed
    }
    return { confirmed }
  })
}

/**
 * PUT /patients/{id}/meds with `{items, avoid, expected_version,
 * change_summary?, effective_date?, list_note?, dry_run?}`. The list-level
 * fields («Desde», the note) left out keep the current values; `null` clears
 * them. A real save refreshes the patient's list and versions; a dry run
 * touches nothing.
 */
export function useSaveMeds(patientId) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body) => api.put(`/patients/${patientId}/meds`, body),
    onSuccess: (result) => {
      if (!result.saved) return
      queryClient.setQueryData(medsKey(patientId), result)
      queryClient.invalidateQueries({ queryKey: medsKey(patientId) })
    },
  })
}

// ── Printable PDF ───────────────────────────────────────────────────────────

/** Path under /api/v1 of the printable list (current, or version `versionNo`). */
export function medsPdfPath(patientId, versionNo) {
  const query = versionNo ? `?version=${versionNo}` : ''
  return `/patients/${patientId}/meds/print.pdf${query}`
}

/** GET …/meds/print.pdf as a `File` (see `fetchPdf`). */
export function fetchMedsPdf(patientId, versionNo, { signal } = {}) {
  return fetchPdf(medsPdfPath(patientId, versionNo), {
    signal,
    fallbackName: 'medicamentos.pdf',
  })
}

// ── Display ─────────────────────────────────────────────────────────────────

export const WEEKDAYS = [
  { value: 1, short: 'lun', long: 'lunes' },
  { value: 2, short: 'mar', long: 'martes' },
  { value: 3, short: 'mié', long: 'miércoles' },
  { value: 4, short: 'jue', long: 'jueves' },
  { value: 5, short: 'vie', long: 'viernes' },
  { value: 6, short: 'sáb', long: 'sábado' },
  { value: 7, short: 'dom', long: 'domingo' },
]

/** [1, 3, 5] → 'lun, mié, vie'; null / empty → '' (every day, nothing to say). */
export function formatDays(days) {
  if (!days?.length) return ''
  return days.map((d) => WEEKDAYS.find((w) => w.value === d)?.short ?? d).join(', ')
}

const FRACTIONS = { '1/2': '½', '1/4': '¼', '3/4': '¾', '1/3': '⅓', '2/3': '⅔' }

/** '1/2' → '½', '1 1/2' → '1½' (display only; the stored text is untouched). */
export function formatQuantity(text) {
  if (!text) return ''
  return text.replace(/(\d+\s+)?\b([123])\/([234])\b/g, (match, whole, num, den) => {
    const glyph = FRACTIONS[`${num}/${den}`]
    if (!glyph) return match
    return whole ? `${whole.trim()}${glyph}` : glyph
  })
}

/** The sheet's «Desde»: `effective_date`, else the Panamá day of `changed_at`. */
export function effectiveDate(list) {
  if (list?.effective_date) return list.effective_date
  return list?.changed_at ? panamaToday(new Date(list.changed_at)) : null
}

// ── Checks (DESIGN §6.4, ticket #29) ────────────────────────────────────────
//
// Mirror of backend `app/services/meds_checks.py` (keep both in sync): an
// item matches an EVITAR row when one of the row's terms is a whole word or
// phrase of its name + strength, after `normalizeName`. Terms: the whole
// substance and its parts split on `/ , ; ( ) +`, each one's leading words
// before the first word with a digit («Aspirina 100 mg» → «aspirina»), terms
// under 3 letters dropped, plus the synonyms of each term's `SYNONYMS` group.
// The term's last word may take a plural «s» / «es». Trademark signs are
// spaces. A basic check by name, never a clinical one.

const MIN_TERM = 3
const PLURALS = ['', 's', 'es']
const SYNONYMS = [
  ['ibuprofeno', 'advil', 'motrin'],
  ['acido acetilsalicilico', 'aspirina'],
  ['acetaminofen', 'paracetamol', 'tylenol', 'panadol'],
  ['naproxeno', 'aleve'],
  ['diclofenaco', 'voltaren'],
  ['metamizol', 'dipirona'],
]

/**
 * No accents, lowercase, punctuation as spaces, single spaces. Trademark signs
 * go first: NFKD would turn «™» into "TM" glued to the name.
 */
export function normalizeName(value) {
  if (!value) return ''
  return value
    .replace(/[™℠®©]/gu, ' ')
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim()
}

const SYNONYM_INDEX = new Map()
for (const group of SYNONYMS) {
  const names = group.map(normalizeName)
  for (const name of names) SYNONYM_INDEX.set(name, names)
}

/** 'aspirina 100 mg' → 'aspirina' (words before the first one with a digit). */
function leadingWords(term) {
  const words = []
  for (const word of term.split(' ')) {
    if (/\p{Nd}/u.test(word)) break
    words.push(word)
  }
  return words.join(' ')
}

function avoidTerms(substance) {
  const parts = [substance ?? '', ...(substance ?? '').split(/[/,;()+]/)].map(normalizeName)
  const terms = new Set(
    [...parts, ...parts.map(leadingWords)].filter((t) => t.replaceAll(' ', '').length >= MIN_TERM),
  )
  for (const term of [...terms]) for (const s of SYNONYM_INDEX.get(term) ?? []) terms.add(s)
  return terms
}

function matchesTerms(text, terms) {
  const haystack = ` ${normalizeName(text)} `
  return [...terms].some((term) => PLURALS.some((end) => haystack.includes(` ${term}${end} `)))
}

/** The EVITAR rows `item` (name + strength) matches. */
export function avoidMatches(item, avoid = []) {
  const text = [item?.name, item?.strength].filter(Boolean).join(' ')
  if (!text.trim()) return []
  return avoid.filter((row) => matchesTerms(text, avoidTerms(row.substance)))
}

/** «… (reason)» when there is a reason. */
export function avoidWarningText(name, substance, reason) {
  const why = reason ? ` (${reason})` : ''
  return `Atención: ${name} coincide con «${substance}» en la lista EVITAR${why}.`
}

/** Warnings to show while adding or editing an item (never blocking). */
export function itemWarnings(item, avoid) {
  return avoidMatches(item, avoid).map((row) =>
    avoidWarningText(item.name?.trim() || 'Este medicamento', row.substance, row.reason),
  )
}

/** Longest «Nota de la lista» the API takes (`LIST_NOTE_MAX` in `app/schemas/meds.py`). */
export const LIST_NOTE_MAX = 20000

export const CHECKS_DISCLAIMER ='Revisión básica por nombre; no reemplaza al médico.'

/**
 * GET /patients/{id}/meds/checks: bitácora entries newer than the list's
 * «Desde» that mention medication changes → `{reference_date, total, entries}`.
 */
export function useMedChecks(patientId, { enabled = true } = {}) {
  return useQuery({
    queryKey: [...medsKey(patientId), 'checks'],
    queryFn: ({ signal }) => api.get(`/patients/${patientId}/meds/checks`, { signal }),
    enabled: Boolean(patientId) && enabled,
  })
}

/** POST …/meds/checks/{entryId}/dismiss: «Ya está reflejado» (for everyone). */
export function useDismissMedCheck(patientId) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (entryId) => api.post(`/patients/${patientId}/meds/checks/${entryId}/dismiss`),
    onSuccess: (result) => queryClient.setQueryData([...medsKey(patientId), 'checks'], result),
  })
}

// ── History ─────────────────────────────────────────────────────────────────

/** Slots in printed-sheet order with their labels (backend `meds_diff.SLOT_LABELS`). */
export const SLOTS = [
  { slot: 'al_despertar', label: 'Al despertar' },
  { slot: 'desayuno', label: 'Desayuno' },
  { slot: 'manana', label: 'Mañana' },
  { slot: 'almuerzo', label: 'Almuerzo' },
  { slot: 'tarde', label: 'Tarde' },
  { slot: 'cena', label: 'Cena' },
  { slot: 'noche', label: 'Noche' },
  { slot: 'madrugada', label: 'Madrugada' },
  { slot: 'segun_necesidad', label: 'Según necesidad' },
  { slot: 'otros', label: 'Otros medicamentos' },
]

/** `med_events.kind` → Spanish label. */
export const EVENT_KIND_LABELS = {
  inicio: 'Inicio',
  suspension: 'Suspensión',
  cambio_dosis: 'Cambio de dosis',
  sustitucion: 'Sustitución',
}

/**
 * A version (`GET …/versions/{n}`) in the shape `MedListView` reads: the
 * snapshot's flat items grouped by slot, its EVITAR rows, list note and
 * «Desde». The snapshot keeps the prescriber's name, not the doctor row.
 */
export function versionToList(version, patientId) {
  const snapshot = version.snapshot ?? {}
  const items = (snapshot.items ?? []).map((item) => ({
    ...item,
    prescriber: item.prescriber_name
      ? { id: item.prescriber_id, name: item.prescriber_name }
      : null,
  }))
  return {
    patient_id: patientId,
    version_no: version.version_no,
    change_summary: version.change_summary,
    changed_at: version.changed_at,
    effective_date: version.effective_date ?? snapshot.effective_date ?? null,
    list_note: snapshot.list_note ?? null,
    slots: SLOTS.map(({ slot, label }) => ({
      slot,
      label,
      items: items.filter((item) => item.slot === slot),
    })),
    avoid: snapshot.avoid ?? [],
  }
}

/**
 * The Historial timeline: versions and bitácora events merged, newest first,
 * grouped by month. A version sits on its «Desde» day (`effective_date`, else
 * the Panamá day it was saved); on the same day it goes above the events,
 * since the list it records already includes them.
 * → `[{ key: 'YYYY-MM', items: [{ type: 'version' | 'event', date, data }] }]`
 */
export function buildTimeline(versions = [], events = []) {
  const items = [
    ...versions.map((v) => ({ type: 'version', date: effectiveDate(v) ?? '', data: v })),
    ...events.map((e) => ({ type: 'event', date: e.event_date ?? '', data: e })),
  ]
  items.sort((a, b) => {
    if (a.date !== b.date) return a.date < b.date ? 1 : -1
    if (a.type !== b.type) return a.type === 'version' ? -1 : 1
    return a.type === 'version' ? b.data.version_no - a.data.version_no : b.data.id - a.data.id
  })
  const groups = []
  for (const item of items) {
    const key = item.date.slice(0, 7)
    const last = groups[groups.length - 1]
    if (last?.key === key) last.items.push(item)
    else groups.push({ key, items: [item] })
  }
  return groups
}

const MONTH_NAMES = [
  'Enero',
  'Febrero',
  'Marzo',
  'Abril',
  'Mayo',
  'Junio',
  'Julio',
  'Agosto',
  'Septiembre',
  'Octubre',
  'Noviembre',
  'Diciembre',
]

/** '2026-03' → 'Marzo 2026'. */
export function monthLabel(key) {
  const [y, m] = (key ?? '').split('-').map(Number)
  return y && m ? `${MONTH_NAMES[m - 1]} ${y}` : 'Sin fecha'
}

// ── Working copy ────────────────────────────────────────────────────────────

/** Item fields sent on save (`sort_order` is the position within the slot). */
const ITEM_FIELDS = [
  'id',
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
  'source_entry_id',
]
const AVOID_FIELDS = ['id', 'substance', 'reason', 'source_entry_id']

let nextKey = 1
/** Client-side key for rows of the working copy (new rows have no id yet). */
export function newKey() {
  return `new-${nextKey++}`
}

export function emptyItem(slot) {
  return {
    key: newKey(),
    id: null,
    name: '',
    strength: null,
    quantity: null,
    dose_note: null,
    slot,
    time: null,
    days: null,
    schedule_note: null,
    instructions: null,
    prescriber_id: null,
    prescriber: null,
    reason: null,
    start_date: null,
    end_date: null,
    source_entry_id: null,
  }
}

/** The editor's starting point: items flat in printed-sheet order, EVITAR rows. */
export function toWorkingCopy(list) {
  return {
    items: list.slots.flatMap((group) => group.items.map((item) => ({ ...item, key: item.id }))),
    avoid: list.avoid.map((row) => ({ ...row, key: row.id })),
  }
}

function pick(row, fields) {
  const out = {}
  for (const field of fields) {
    const value = row[field]
    if (field === 'id' && value == null) continue
    out[field] = value === '' ? null : (value ?? null)
  }
  return out
}

/** The `items` / `avoid` of the PUT body, in the working copy's order. */
export function toSavePayload(working) {
  return {
    items: working.items.map((item) => pick(item, ITEM_FIELDS)),
    avoid: working.avoid.map((row) => pick(row, AVOID_FIELDS)),
  }
}

/** True when the diff of a dry run or a version has anything in it. */
export function diffHasChanges(diff) {
  return Object.values(diff ?? {}).some((value) => Array.isArray(value) && value.length > 0)
}

/** An edit session: the list it started from, its payload, and the working copy. */
export function startSession(list) {
  const working = toWorkingCopy(list)
  return { base: list, baseline: JSON.stringify(toSavePayload(working)), working }
}

export function sessionDirty(session) {
  return Boolean(session) && JSON.stringify(toSavePayload(session.working)) !== session.baseline
}

/** Moves the item `key` one place up (-1) or down (+1) among its slot's items. */
export function moveWithinSlot(items, key, delta) {
  const index = items.findIndex((item) => item.key === key)
  if (index < 0) return items
  const slot = items[index].slot
  let target = index + delta
  while (target >= 0 && target < items.length && items[target].slot !== slot) target += delta
  if (target < 0 || target >= items.length) return items
  const next = [...items]
  ;[next[index], next[target]] = [next[target], next[index]]
  return next
}

/**
 * Puts a saved item back in the working copy: in place when it keeps its
 * slot, else at the end of its new slot (a new item goes to the end too).
 */
export function upsertItem(items, item) {
  const index = items.findIndex((i) => i.key === item.key)
  if (index >= 0 && items[index].slot === item.slot) {
    return items.map((i) => (i.key === item.key ? item : i))
  }
  return [...items.filter((i) => i.key !== item.key), item]
}

/**
 * 422 bodies point at the offending row (`loc: ['body', 'items', 3, 'name']`):
 * map them to the working copy's keys so the editor can flag that row.
 */
export function rowErrors(error, working) {
  const out = {}
  const detail = error?.status === 422 && Array.isArray(error.body?.detail) ? error.body.detail : []
  for (const problem of detail) {
    const [, collection, index] = problem.loc ?? []
    const rows =
      collection === 'items' ? working.items : collection === 'avoid' ? working.avoid : null
    const row = rows?.[index]
    if (row) out[row.key] = 'Revisa los datos de esta fila: el servidor no los aceptó.'
  }
  return out
}
