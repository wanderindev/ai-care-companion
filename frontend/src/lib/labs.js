/**
 * Labs vocabulary, filters <-> URL, and value helpers (docs/DESIGN.md §5.5, §6.5).
 *
 * The one place that turns lab enum values (flag, status) into Spanish labels.
 * Filters live in the URL search params, like the bitácora's.
 */

// --- Labels -----------------------------------------------------------------

export const FLAG_LABELS = { alto: 'Alto', bajo: 'Bajo', normal: 'Normal', anormal: 'Anormal' }
export const OUT_OF_RANGE = new Set(['alto', 'bajo', 'anormal'])

export function flagLabel(flag) {
  return FLAG_LABELS[flag] ?? ''
}

export const STATUS_LABELS = { ok: 'Revisado', needs_review: 'Por revisar', failed: 'Con error' }

const SOURCE_LABELS = { whatsapp: 'WhatsApp', upload: 'Subido a mano', link: 'Portal del laboratorio' }

export function sourceLabel(source) {
  return SOURCE_LABELS[source] ?? source
}

// --- Filters in the URL -----------------------------------------------------

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

function isValidDate(value) {
  if (!DATE_RE.test(value)) return false
  const [y, m, d] = value.split('-').map(Number)
  const date = new Date(Date.UTC(y, m - 1, d))
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d
}

export const EMPTY_LAB_FILTERS = Object.freeze({
  q: '',
  outOfRange: false,
  pendingOnly: false,
  dateFrom: '',
  dateTo: '',
})

/**
 * URLSearchParams -> filters (malformed values dropped):
 *   q=glucosa  fuera=1  revisar=1  desde=2025-01-01  hasta=2025-12-31
 */
export function parseLabFilters(searchParams) {
  const dateFrom = searchParams.get('desde') ?? ''
  const dateTo = searchParams.get('hasta') ?? ''
  return {
    q: (searchParams.get('q') ?? '').slice(0, 200),
    outOfRange: searchParams.get('fuera') === '1',
    pendingOnly: searchParams.get('revisar') === '1',
    dateFrom: isValidDate(dateFrom) ? dateFrom : '',
    dateTo: isValidDate(dateTo) ? dateTo : '',
  }
}

export function labFiltersToSearchParams(filters) {
  const f = { ...EMPTY_LAB_FILTERS, ...filters }
  const params = new URLSearchParams()
  if (f.q.trim()) params.set('q', f.q.trim())
  if (f.outOfRange) params.set('fuera', '1')
  if (f.pendingOnly) params.set('revisar', '1')
  if (f.dateFrom) params.set('desde', f.dateFrom)
  if (f.dateTo) params.set('hasta', f.dateTo)
  return params
}

export function hasActiveLabFilters(f) {
  return Boolean(f.q.trim() || f.outOfRange || f.pendingOnly || f.dateFrom || f.dateTo)
}

/**
 * Query string for GET /lab-reports. Reports whose patient was not
 * identified are always included (`include_unassigned`), whatever the
 * patient switcher says: they must stay visible until someone assigns them.
 */
export function toLabApiParams(filters, patientIds, { limit, offset }) {
  const params = new URLSearchParams()
  patientIds.forEach((id) => params.append('patient_id', String(id)))
  params.set('include_unassigned', 'true')
  if (filters.q.trim()) params.set('q', filters.q.trim())
  if (filters.outOfRange) params.set('out_of_range', 'true')
  if (filters.pendingOnly) params.set('status', 'needs_review')
  if (filters.dateFrom) params.set('date_from', filters.dateFrom)
  if (filters.dateTo) params.set('date_to', filters.dateTo)
  params.set('limit', String(limit))
  params.set('offset', String(offset))
  return params
}

// --- Values -------------------------------------------------------------------

const numberFormat = new Intl.NumberFormat('es-PA', { maximumFractionDigits: 3 })

export function formatNumber(value) {
  return value === null || value === undefined ? '' : numberFormat.format(value)
}

/** The value as the report printed it (falls back to the parsed number). */
export function resultValue(result) {
  if (result.value_text) return result.value_text
  if (result.value_num === null || result.value_num === undefined) return '—'
  return `${result.comparator ?? ''}${formatNumber(result.value_num)}`
}

/** "70 – 100", "< 100", "> 40", the printed text, or ''. */
export function referenceText(result) {
  const { ref_low: low, ref_high: high } = result
  if (low !== null && low !== undefined && high !== null && high !== undefined) {
    return `${formatNumber(low)} – ${formatNumber(high)}`
  }
  if (high !== null && high !== undefined) return `< ${formatNumber(high)}`
  if (low !== null && low !== undefined) return `> ${formatNumber(low)}`
  return result.ref_text ?? ''
}

/** Unit grouping key: case, spaces and "µ"/"u" spelling differences ignored. */
export function unitKey(unit) {
  return (unit ?? '').trim().toLowerCase().replace(/\s+/g, '').replace(/µ|μ/g, 'u')
}

/**
 * Splits series points by unit (in order of first appearance), so values in
 * different units are never drawn on one axis.
 */
export function groupByUnit(points) {
  const groups = new Map()
  for (const point of points) {
    const key = unitKey(point.unit)
    if (!groups.has(key)) groups.set(key, { key, unit: point.unit?.trim() || '', points: [] })
    groups.get(key).points.push(point)
  }
  return [...groups.values()]
}

/** Results grouped by panel, keeping report order; results without a panel first. */
export function groupByPanel(results) {
  const groups = []
  const byPanel = new Map()
  for (const result of results) {
    const key = result.panel ?? ''
    if (!byPanel.has(key)) {
      const group = { panel: result.panel, results: [] }
      byPanel.set(key, group)
      groups.push(group)
    }
    byPanel.get(key).results.push(result)
  }
  return groups
}

/** Reports grouped under their report date (the list is already date-sorted). */
export function groupByDate(reports) {
  const groups = []
  for (const report of reports) {
    const last = groups[groups.length - 1]
    if (last && last.date === report.report_date) last.reports.push(report)
    else groups.push({ date: report.report_date, reports: [report] })
  }
  return groups
}

/** Badge text for a report whose patient was not identified. */
export const PATIENT_UNKNOWN = 'Paciente sin identificar'
