import { MinusCircleIcon, PencilSquareIcon, PlusCircleIcon } from '@heroicons/react/20/solid'
import { formatQuantity } from '../../lib/meds.js'

// `word` for a pending save (confirmation sheet), `past` for a saved version.
const KINDS = {
  added: { icon: PlusCircleIcon, word: 'Se agrega', past: 'Agregado', tone: 'text-emerald-800' },
  removed: { icon: MinusCircleIcon, word: 'Se quita', past: 'Suspendido', tone: 'text-red-800' },
  changed: { icon: PencilSquareIcon, word: 'Cambia', past: 'Cambió', tone: 'text-slate-900' },
}

function itemLine(item, slotLabels) {
  const head = [formatQuantity(item.quantity), item.name, item.strength].filter(Boolean).join(' ')
  const slot = slotLabels[item.slot]
  return slot ? `${head} (${slot})` : head
}

function Row({ kind, title, past, pastWord, children }) {
  const { icon: Icon, tone } = KINDS[kind]
  const word = past ? (pastWord ?? KINDS[kind].past) : KINDS[kind].word
  return (
    <li className="flex gap-2 py-2">
      <Icon className={`mt-0.5 size-5 shrink-0 ${tone}`} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className={tone}>
          <span className="font-semibold">{word}:</span> {title}
        </p>
        {children}
      </div>
    </li>
  )
}

// Bitácora links are bookkeeping (the summary text leaves them out too).
const HIDDEN_FIELDS = new Set(['source_entry_id'])

/**
 * Before / after text of a changed field. Quantities are shown as fractions
 * ('1/2' → '½'), unless that makes both sides look the same (a '1/2' → '½'
 * edit): then the raw texts are shown, so the change stays visible.
 */
function fieldTexts(f) {
  if (f.field !== 'quantity') return [f.before_text, f.after_text]
  const before = formatQuantity(f.before_text)
  const after = formatQuantity(f.after_text)
  return before === after ? [f.before_text, f.after_text] : [before, after]
}

function Fields({ fields }) {
  return (
    <ul className="mt-1 space-y-0.5 text-sm text-slate-700">
      {fields
        .filter((f) => !HIDDEN_FIELDS.has(f.field))
        .map((f) => {
          const [before, after] = fieldTexts(f)
          return (
            <li key={f.field}>
              <span className="font-medium">{f.label}:</span>{' '}
              <span className="line-through decoration-slate-400">{before}</span>
              {' → '}
              <span className="font-medium text-slate-900">{after}</span>
            </li>
          )
        })}
    </ul>
  )
}

/**
 * A readable list diff (the `diff` of `PUT … dry_run` or of a version):
 * added / removed / changed items with their changed fields, reordered
 * slots, and the same for EVITAR. Words and icons carry the meaning, not
 * color alone. `past` words it for a saved version («Agregado»,
 * «Suspendido», «Cambió») instead of a pending save.
 */
export default function MedDiff({ diff, slots, past = false }) {
  const slotLabels = Object.fromEntries((slots ?? []).map((s) => [s.slot, s.label]))
  const rows = [
    ...(diff.added ?? []).map((item, i) => (
      <Row key={`a${i}`} kind="added" past={past} title={itemLine(item, slotLabels)} />
    )),
    ...(diff.removed ?? []).map((item) => (
      <Row key={`r${item.id}`} kind="removed" past={past} title={itemLine(item, slotLabels)} />
    )),
    ...(diff.changed ?? []).map((change) => (
      <Row key={`c${change.id}`} kind="changed" past={past} title={change.name}>
        <Fields fields={change.fields} />
      </Row>
    )),
    ...(diff.reordered_slots ?? []).map((slot) => (
      <Row
        key={`o${slot}`}
        kind="changed"
        past={past}
        title={`orden de ${slotLabels[slot] ?? slot}`}
      />
    )),
    ...(diff.avoid_added ?? []).map((row, i) => (
      <Row key={`aa${i}`} kind="added" past={past} title={`EVITAR ${row.substance}`} />
    )),
    ...(diff.avoid_removed ?? []).map((row) => (
      <Row
        key={`ar${row.id}`}
        kind="removed"
        past={past}
        pastWord="Quitado"
        title={`EVITAR ${row.substance}`}
      />
    )),
    ...(diff.avoid_changed ?? []).map((change) => (
      <Row key={`ac${change.id}`} kind="changed" past={past} title={`EVITAR ${change.substance}`}>
        <Fields fields={change.fields} />
      </Row>
    )),
    // List-level fields (#135): «Desde» shows both dates; the note only that
    // it was added / removed / modified (it can be a long table).
    ...(diff.list_changed ?? []).map((f) => {
      if (f.field === 'list_note') {
        const kind = !f.before ? 'added' : !f.after ? 'removed' : 'changed'
        const pastWord = { added: 'Agregada', removed: 'Quitada', changed: 'Modificada' }[kind]
        return (
          <Row
            key="list_note"
            kind={kind}
            past={past}
            pastWord={pastWord}
            title="nota de la lista"
          />
        )
      }
      return (
        <Row key={f.field} kind="changed" past={past} title={`fecha ${f.label}`}>
          <Fields fields={[f]} />
        </Row>
      )
    }),
  ]
  if (!rows.length) return <p className="text-slate-600">Sin cambios.</p>
  return <ul className="divide-y divide-slate-100">{rows}</ul>
}
