/**
 * Attachment uploads (POST /attachments, multipart) and short-lived file URLs.
 *
 * Camera photos are downscaled in the browser to at most 2560 px on the long
 * side before upload (docs/DESIGN.md §5.2). Re-encoding also drops EXIF
 * metadata (GPS position, device) from the stored copy. Files the browser
 * cannot decode (HEIC outside Safari), PDFs and audio are sent unchanged; the
 * server decides the type from the bytes and enforces the size limit.
 *
 * Uploads go through the shared api client, so they carry X-Requested-With
 * (the CSRF guard rejects writes without it).
 */
import { api } from './api.js'

export const MAX_IMAGE_SIDE = 2560
const RESIZABLE = new Set(['image/jpeg', 'image/png', 'image/webp'])
const JPEG_QUALITY = 0.85

/** HEIC/HEIF (iPhone photos). Phones may send no type: check the name too. */
export function isHeic(file) {
  return (
    file.type === 'image/heic' || file.type === 'image/heif' || /\.(heic|heif)$/i.test(file.name ?? '')
  )
}

function canvasToBlob(canvas, type, quality) {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality))
}

/**
 * Returns a File no larger than `maxSide` px on its long side, or the original
 * file when it is not a resizable image, already small enough, or cannot be
 * decoded here. A HEIC/HEIF photo the browser can decode (Safari) is always
 * re-encoded as JPEG, which every reader (and Claude) accepts; elsewhere it
 * is sent unchanged. Never throws.
 */
export async function downscaleImage(file, maxSide = MAX_IMAGE_SIDE) {
  const heic = isHeic(file)
  if ((!RESIZABLE.has(file.type) && !heic) || typeof createImageBitmap !== 'function') return file
  let bitmap
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
  } catch {
    return file
  }
  try {
    const { width, height } = bitmap
    const scale = Math.min(1, maxSide / Math.max(width, height))
    if (scale === 1 && !heic) return file

    const canvas = document.createElement('canvas')
    canvas.width = Math.round(width * scale)
    canvas.height = Math.round(height * scale)
    const ctx = canvas.getContext('2d')
    if (!ctx) return file
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height)

    // PNG keeps transparency (screenshots); photos become JPEG.
    const type = file.type === 'image/png' ? 'image/png' : 'image/jpeg'
    const blob = await canvasToBlob(canvas, type, JPEG_QUALITY)
    if (!blob) return file
    const ext = type === 'image/png' ? 'png' : 'jpg'
    const name = file.name ? file.name.replace(/\.[^.]*$/, '') + `.${ext}` : `foto.${ext}`
    return new File([blob], name, { type, lastModified: file.lastModified })
  } catch {
    return file
  } finally {
    bitmap.close?.()
  }
}

/**
 * Uploads one file and resolves to the new attachment
 * `{ id, filename, mime, size, sha256, owner_type, owner_id, created_at }`.
 * It is not linked to anything yet: send its `id` with the record that owns it.
 * Rejects with ApiError (413 too large, 415 type not allowed, 503 storage down).
 */
export async function uploadAttachment(file, { signal } = {}) {
  const prepared = await downscaleImage(file)
  const form = new FormData()
  form.append('file', prepared, prepared.name || file.name || 'archivo')
  return api.post('/attachments', form, { signal })
}

/**
 * A presigned URL for viewing (or, with `download`, saving) a file. It expires
 * after a few minutes: fetch it right before use, don't cache it.
 */
export function getAttachmentUrl(id, { download = false } = {}) {
  const query = download ? '?download=true' : ''
  return api.get(`/attachments/${id}/url${query}`)
}

/**
 * Opens (new tab) or downloads a file with a fresh URL. The tab is opened
 * synchronously inside the click so popup blockers allow it.
 */
export async function openAttachment(id, { download = false } = {}) {
  const tab = download ? null : window.open('', '_blank')
  try {
    const { url } = await getAttachmentUrl(id, { download })
    if (tab) {
      tab.opener = null
      tab.location.href = url
    } else {
      window.location.assign(url)
    }
  } catch (error) {
    tab?.close()
    throw error
  }
}
