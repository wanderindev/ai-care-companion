/**
 * «Subir resultado» (manual lab upload, docs/DESIGN.md §6.5): one PDF, or
 * photos of the pages of one report, read by the same extraction as the
 * WhatsApp PDFs. Files go up with `uploadAttachment` (photos downscaled to
 * 2560 px), then POST /lab-reports/extract queues one job.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from './api.js'
import { downscaleImage, isHeic } from './upload.js'

export const MAX_PHOTOS = 10
// «Subir PDF» from a lab link (#124): a portal may hold several PDFs, each
// read as its own report.
export const MAX_LINK_PDFS = 10
const PHOTO_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp'])

// The gallery input lists only formats Claude reads, so iOS hands over
// gallery photos as JPEG instead of the original HEIC.
export const PICKER_ACCEPT = 'application/pdf,image/jpeg,image/png,image/webp'

export function isPdf(file) {
  return file.type === 'application/pdf' || /\.pdf$/i.test(file.name ?? '')
}

/**
 * Picked files ready for the checks: photos downscaled, and a HEIC photo the
 * browser can decode turned into JPEG (`downscaleImage`). Never throws.
 */
export function prepareLabFiles(files) {
  return Promise.all(files.map((f) => (isPdf(f) ? f : downscaleImage(f))))
}

/**
 * '' when `files` (after `prepareLabFiles`) can be read as one report, else
 * a Spanish message. The server checks again from the bytes; this only
 * spares a useless upload. `multiplePdfs` (lab link mode): several PDFs are
 * fine, each one its own report; they still cannot be mixed with photos.
 */
export function checkLabFiles(files, { multiplePdfs = false } = {}) {
  if (!files.length) return 'Elige un PDF o al menos una foto.'
  const pdfs = files.filter(isPdf)
  if (pdfs.length && multiplePdfs) {
    if (pdfs.length !== files.length) return 'Sube solo PDF o solo fotos, sin mezclarlos.'
    if (pdfs.length > MAX_LINK_PDFS) return `Sube como máximo ${MAX_LINK_PDFS} PDF a la vez.`
    return ''
  }
  if (pdfs.length) return files.length === 1 ? '' : 'Sube un solo PDF, sin otras fotos ni archivos.'
  if (files.some(isHeic)) {
    return 'Las fotos HEIC no se pueden leer. Configura la cámara en «Más compatible» (JPG) o sube una captura de pantalla de la foto.'
  }
  if (files.some((f) => f.type && !PHOTO_TYPES.has(f.type))) {
    return 'Solo se aceptan un PDF o fotos (JPG, PNG o WebP).'
  }
  if (files.length > MAX_PHOTOS) return `Sube como máximo ${MAX_PHOTOS} fotos por informe.`
  return ''
}

/**
 * Resolves to `{ job_id }`. `labLinkId`: the lab link the PDF was downloaded
 * from («Subir PDF»), which is then marked resolved; a link with several
 * PDFs takes one request per PDF (one report each).
 */
export function startLabExtraction(attachmentIds, patientId, labLinkId = null) {
  return api.post('/lab-reports/extract', {
    attachment_ids: attachmentIds,
    patient_id: patientId ?? null,
    lab_link_id: labLinkId ?? null,
  })
}

/**
 * Where an upload job ended (`{ outcome, report_id }`), fetched once the job
 * is finished. `outcome`: 'lab_report' | 'not_lab_report' | 'failed' | null.
 */
export function useLabUploadOutcome(jobId, finished) {
  return useQuery({
    queryKey: ['lab-upload-outcome', jobId],
    queryFn: ({ signal }) => api.get(`/lab-reports/extract/${jobId}`, { signal }),
    enabled: jobId != null && finished,
    staleTime: Infinity,
  })
}

/** The Spanish message of a failed job (`"PermanentJobError: …"` → `…`). */
export function jobErrorMessage(job, fallback) {
  const match = /^(?:Permanent)?JobError: (.+)$/.exec(job?.error ?? '')
  return match ? match[1] : fallback
}
