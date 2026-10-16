import { ChatBubbleLeftRightIcon } from '@heroicons/react/24/outline'
import { EXAMPLE_PROMPTS } from '../../lib/chat.js'

/** Empty state: a few questions to start with; tapping one fills the box. */
export default function ExamplePrompts({ onPick, title = 'Pregunta lo que necesites' }) {
  return (
    <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-5 py-8 text-center">
      <ChatBubbleLeftRightIcon className="mx-auto size-10 text-slate-400" aria-hidden="true" />
      <p className="mt-3 font-medium text-slate-800">{title}</p>
      <p className="mt-1 text-sm text-slate-500">
        El asistente responde con lo que está registrado en la bitácora, el calendario, los
        medicamentos y los laboratorios. Por ejemplo:
      </p>
      <ul className="mt-4 space-y-2 text-left">
        {EXAMPLE_PROMPTS.map((prompt) => (
          <li key={prompt}>
            <button
              type="button"
              onClick={() => onPick(prompt)}
              className="min-h-12 w-full rounded-2xl bg-brand-50 px-4 py-2.5 text-left text-sm font-medium text-brand-800 active:bg-brand-100"
            >
              «{prompt}»
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
