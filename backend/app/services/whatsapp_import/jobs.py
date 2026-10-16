"""Worker handlers (listed in ``app.worker.HANDLER_MODULES``).

- ``whatsapp_import``, payload ``{"import_id": <id>}``: the import itself,
  ``app.services.whatsapp_import.process.process_import``.
- ``whatsapp_media``, payload ``{"import_id": <id>}``: after the sync
  extension uploaded files to an import,
  ``app.services.whatsapp_import.media_upload.finish_media`` (runs the
  ``after_media`` hooks). With ``"deadline": true`` (queued ``MEDIA_WAIT``
  after an extension run's import finished): ``media_upload.finalize``, for
  a run the extension never completed (it queues the plain job if needed).
"""

from app.core.config import get_settings
from app.models.whatsapp import WhatsappImport
from app.services import jobs
from app.services.storage import StorageNotConfigured, get_storage
from app.services.whatsapp_import.media_upload import MEDIA_JOB_KIND, finalize, finish_media
from app.services.whatsapp_import.process import process_import
from app.services.whatsapp_import.upload import JOB_KIND


def _import_id(ctx: jobs.JobContext) -> int:
    import_id = ctx.payload.get("import_id")
    if not isinstance(import_id, int) or isinstance(import_id, bool):
        raise jobs.PermanentJobError("Tarea de importación inválida.")
    return import_id


@jobs.handler(JOB_KIND)
def run_whatsapp_import(ctx: jobs.JobContext) -> None:
    import_id = _import_id(ctx)
    try:
        storage = get_storage()
    except StorageNotConfigured:
        raise jobs.JobError("El almacenamiento de archivos no está disponible.") from None
    process_import(ctx.db, storage, get_settings(), import_id, ctx.progress)


@jobs.handler(MEDIA_JOB_KIND)
def run_whatsapp_media(ctx: jobs.JobContext) -> None:
    import_id = _import_id(ctx)
    if ctx.payload.get("deadline") is True:
        imp = ctx.db.get(WhatsappImport, import_id, with_for_update=True)
        if imp is not None:
            finalize(ctx.db, imp)
        ctx.db.commit()
    else:
        finish_media(ctx.db, import_id)
    ctx.progress(100, "Listo")
