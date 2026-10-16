import PdfButton from '../ui/PdfButton.jsx'
import { fetchMedsPdf } from '../../lib/meds.js'

/**
 * «Imprimir / Descargar PDF» for one patient's list (or one version of it):
 * the shared two-tap `PdfButton` (fetch with the cookie, then open/share).
 */
export default function PrintButton({
  patientId,
  versionNo,
  label = 'Imprimir / Descargar PDF',
  ariaLabel,
}) {
  return (
    <PdfButton
      fetchFile={({ signal }) => fetchMedsPdf(patientId, versionNo, { signal })}
      label={label}
      ariaLabel={ariaLabel}
      shareTitle="Lista de medicamentos"
      resetKey={`${patientId}:${versionNo ?? ''}`}
    />
  )
}
