/**
 * LEDD (Levodopa Equivalent Daily Dose, ticket #133): display helpers over the
 * `ledd` object the meds API returns (computed on the server by
 * `app/services/ledd.py`: total_mg, contributions, as_needed,
 * not_calculated, pending) and the admins' review of AI-classified names.
 * The app never acts on the number: it is a reference for the neurologist.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'
import { MEDS_KEY } from './meds.js'

export const LEDD_DISCLAIMER =
  'Aproximación de referencia (convención de investigación: Tomlinson 2010 / Jost 2023). ' +
  'No es una dosis ni una indicación: sirve para comentar los cambios con el neurólogo.'

/** Symbol next to an item that counts (never color alone: it has an aria-label). */
export const LEDD_MARK = '◆'
export const LEDD_MARK_AS_NEEDED = '◇'

/** Spanish number with at most one decimal: 412.5 → «412.5», 800 → «800» (es-PA, like labs.js). */
export function formatMg(value) {
  if (value == null) return '—'
  const rounded = Math.round(value * 10) / 10
  return rounded.toLocaleString('es-PA', {
    maximumFractionDigits: 1,
    useGrouping: false,
  })
}

/** A daily dose: up to 3 decimals (pramipexol 0.125 mg). */
export function formatDose(value) {
  if (value == null) return '—'
  return value.toLocaleString('es-PA', {
    maximumFractionDigits: 3,
    useGrouping: false,
  })
}

export function formatFactor(contribution) {
  const factor = formatDose(contribution.factor)
  if (contribution.kind === 'comt') return `levodopa × ${factor}`
  if (contribution.kind === 'fixed') return `${factor} mg fijos`
  return `× ${factor}`
}

/** «LEDD: 920 → 1040 mg/día (+13 %)», or '' when it did not change. */
export function leddChangeText(change) {
  if (!change) return ''
  const pct =
    change.pct == null
      ? ''
      : ` (${change.pct > 0 ? '+' : change.pct < 0 ? '−' : ''}${Math.abs(change.pct)} %)`
  return `LEDD: ${formatMg(change.before_mg)} → ${formatMg(change.after_mg)} mg/día${pct}`
}

/** item id → 'counted' | 'as_needed' (for the per-item mark). */
export function leddMarks(ledd) {
  const marks = new Map()
  if (!ledd) return marks
  for (const c of ledd.contributions ?? []) for (const id of c.item_ids) marks.set(id, 'counted')
  for (const a of ledd.as_needed ?? []) marks.set(a.item_id, 'as_needed')
  return marks
}

const SUBSTANCES_KEY = ['ledd', 'substances']

function useDecision(action) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, factor }) =>
      api.post(`/ledd/substances/${id}/${action}`, action === 'confirm' ? { factor } : undefined),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: MEDS_KEY })
      queryClient.invalidateQueries({ queryKey: SUBSTANCES_KEY })
    },
  })
}

/** «Confirmar» (counts with the proposed or edited factor). */
export const useConfirmLeddSubstance = () => useDecision('confirm')
/** «No cuenta» (adds 0). */
export const useRejectLeddSubstance = () => useDecision('reject')
