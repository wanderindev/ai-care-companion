import { useRef } from 'react'
import { CameraIcon, DocumentPlusIcon, PhotoIcon, XMarkIcon } from '@heroicons/react/24/outline'
import { ExclamationCircleIcon } from '@heroicons/react/20/solid'
import { uploadAttachment } from '../../lib/upload.js'
import { formatSize, uploadErrorMessage } from '../../lib/bitacora.js'
import { AttachmentIcon } from './Attachments.jsx'

let nextKey = 0

const pickerClass =
  'flex min-h-12 flex-1 flex-col items-center justify-center gap-0.5 rounded-2xl px-2 py-1 text-sm font-medium text-slate-700 ring-1 ring-slate-300 active:bg-slate-100'

/**
 * Attachments of the entry form. Files upload as soon as they are picked
 * (POST /attachments) and the entry links the successful ones on save. A
 * failed upload shows why and never blocks saving the entry without it.
 *
 * `items`: [{ key, status: 'uploading'|'done'|'error', attachment?, name, error? }]
 * `onChange(updater)`: functional state update from the parent.
 */
export default function AttachmentsField({ items, onChange }) {
  const cameraRef = useRef(null)
  const galleryRef = useRef(null)
  const fileRef = useRef(null)
  const fileButtonRef = useRef(null)

  function addFiles(fileList) {
    const files = [...(fileList ?? [])]
    if (!files.length) return
    const added = files.map((file) => ({
      key: `u${(nextKey += 1)}`,
      status: 'uploading',
      name: file.name || 'archivo',
      file,
    }))
    onChange((current) => [...current, ...added])
    for (const item of added) {
      uploadAttachment(item.file)
        .then((attachment) =>
          onChange((current) =>
            current.map((i) =>
              i.key === item.key
                ? { key: i.key, status: 'done', attachment, name: attachment.filename }
                : i,
            ),
          ),
        )
        .catch((error) =>
          onChange((current) =>
            current.map((i) =>
              i.key === item.key
                ? { key: i.key, status: 'error', name: i.name, error: uploadErrorMessage(error) }
                : i,
            ),
          ),
        )
    }
  }

  function onPicked(event) {
    addFiles(event.target.files)
    event.target.value = '' // the same file can be picked again
  }

  return (
    <fieldset>
      <legend className="text-sm font-medium text-slate-700">Adjuntos</legend>
      <div className="mt-1 flex gap-2">
        <button type="button" onClick={() => cameraRef.current?.click()} className={pickerClass}>
          <CameraIcon className="size-6" aria-hidden="true" />
          Cámara
        </button>
        <button type="button" onClick={() => galleryRef.current?.click()} className={pickerClass}>
          <PhotoIcon className="size-6" aria-hidden="true" />
          Galería
        </button>
        <button
          ref={fileButtonRef}
          type="button"
          onClick={() => fileRef.current?.click()}
          className={pickerClass}
        >
          <DocumentPlusIcon className="size-6" aria-hidden="true" />
          Archivo
        </button>
      </div>
      <input
        ref={cameraRef}
        type="file"
        accept="image/*"
        capture="environment"
        onChange={onPicked}
        className="hidden"
        aria-label="Tomar foto"
        tabIndex={-1}
      />
      <input
        ref={galleryRef}
        type="file"
        accept="image/*"
        multiple
        onChange={onPicked}
        className="hidden"
        aria-label="Elegir fotos"
        tabIndex={-1}
      />
      <input
        ref={fileRef}
        type="file"
        accept="image/*,.heic,.heif,application/pdf,audio/*"
        multiple
        onChange={onPicked}
        className="hidden"
        aria-label="Elegir archivos"
        tabIndex={-1}
      />
      <p className="mt-1 text-xs text-slate-500">Fotos, PDF o audios.</p>

      {items.length > 0 && (
        <ul className="mt-2 space-y-2" aria-live="polite">
          {items.map((item) => (
            <li
              key={item.key}
              className={`flex items-center gap-3 rounded-2xl border p-2 pl-3 ${
                item.status === 'error' ? 'border-red-200 bg-red-50' : 'border-slate-200 bg-white'
              }`}
            >
              {item.status === 'error' ? (
                <ExclamationCircleIcon
                  className="size-6 shrink-0 text-red-600"
                  aria-hidden="true"
                />
              ) : (
                <AttachmentIcon
                  mime={item.attachment?.mime ?? item.file?.type}
                  className="size-6 shrink-0 text-slate-400"
                />
              )}
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{item.name}</p>
                <p
                  className={`text-xs ${item.status === 'error' ? 'text-red-800' : 'text-slate-500'}`}
                >
                  {item.status === 'uploading' && 'Subiendo…'}
                  {item.status === 'done' && formatSize(item.attachment.size)}
                  {item.status === 'error' && `No se subió: ${item.error}`}
                </p>
              </div>
              {item.status !== 'uploading' && (
                <button
                  type="button"
                  aria-label={`Quitar ${item.name}`}
                  onClick={() => {
                    onChange((current) => current.filter((i) => i.key !== item.key))
                    // This button goes away with its row; keep focus in the sheet.
                    fileButtonRef.current?.focus()
                  }}
                  className="flex size-11 shrink-0 items-center justify-center rounded-xl text-slate-500 active:bg-slate-100"
                >
                  <XMarkIcon className="size-5" aria-hidden="true" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </fieldset>
  )
}
