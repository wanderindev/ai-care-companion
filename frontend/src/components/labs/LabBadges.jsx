import {
  ArrowDownIcon,
  ArrowUpIcon,
  CheckIcon,
  ExclamationTriangleIcon,
  QuestionMarkCircleIcon,
} from '@heroicons/react/20/solid'
import { flagLabel, PATIENT_UNKNOWN } from '../../lib/labs.js'

// Each flag has its own text and icon; color only reinforces them. Text
// colors are the -800/-900 steps on -50 fills (contrast well above 4.5:1).
const FLAG_STYLES = {
  alto: { className: 'bg-red-50 text-red-800 ring-red-200', Icon: ArrowUpIcon },
  bajo: { className: 'bg-blue-50 text-blue-800 ring-blue-200', Icon: ArrowDownIcon },
  anormal: {
    className: 'bg-amber-50 text-amber-900 ring-amber-300',
    Icon: ExclamationTriangleIcon,
  },
  normal: { className: 'bg-emerald-50 text-emerald-800 ring-emerald-200', Icon: CheckIcon },
}

export function FlagBadge({ flag }) {
  const style = FLAG_STYLES[flag]
  if (!style) return <span className="text-sm text-slate-400">—</span>
  const { className, Icon } = style
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold whitespace-nowrap ring-1 ring-inset ${className}`}
    >
      <Icon className="size-3.5" aria-hidden="true" />
      {flagLabel(flag)}
    </span>
  )
}

export function UnassignedBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900 ring-1 ring-amber-300 ring-inset">
      <QuestionMarkCircleIcon className="size-3.5" aria-hidden="true" />
      {PATIENT_UNKNOWN}
    </span>
  )
}
